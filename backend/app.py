"""Bootstrap local da API e do build opcional do frontend."""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import timezone, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv("backend/.env")
load_dotenv(Path(__file__).parent / ".env")

from api import api_router, public_router
from api.auth import register_auth_error_handler
from config import settings
from core.observability import bootstrap_observability, instrument_fastapi_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Observabilidade (OBS-01) providers OTel + handler de logs para o Loki,
# adicionados ao lado do basicConfig (D-01); no-op quando OBS_ENABLED=false
# ou sem endpoints.
_observability_enabled = bootstrap_observability()

logger = logging.getLogger("App")

# Slot alinhado de 10 minutos: o mesmo tamanho usado pelo claim
# (category_scan:{monitor_id}:{slot_start_utc}, Plan 07-05) e pela CronTrigger
# abaixo, para que misfire_grace_time nunca ultrapasse um slot inteiro.
_CATEGORY_MONITOR_SLOT_SECONDS = 600


async def _cancel_and_await_tasks(tasks: dict) -> None:
    """Cancel and await every still-pending task, logging cleanup failures.

    Used for `PriceMonitorService.tasks`, which are not registered with
    `JobManager` (each monitor owns a long-lived recurring loop, not a single
    claimed attempt) — `job_manager.shutdown()` alone would not reach them.
    """
    pending = [task for task in tasks.values() if not task.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=5.0
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                logger.exception(
                    "Falha ao encerrar task de monitor no shutdown.", exc_info=result
                )
    except asyncio.TimeoutError:
        logger.warning(
            "Timeout (5s) aguardando encerramento de tasks de monitor no shutdown; prosseguindo."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ordena boot/reconcile/scheduler/shutdown para os tres tipos de job.

    Ordem (D-01/D-05/D-06/D-07): hidrata os storages sem criar nenhuma task ->
    `job_manager.startup_reconcile()` retoma busca/cross e scan de categoria
    orfaos -> `start_loaded_monitor_loops()` reclama ciclos de preco orfaos e
    so entao inicia o loop normal de cada monitor ACTIVE -> o scheduler so
    comeca a agendar depois que tudo isso terminou.
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    from core.job_manager import job_manager
    from core.db import db
    from tenacity import retry, wait_exponential, stop_after_attempt
    from services.category_monitor_service import (
        category_monitor_job,
        reconcile_orphaned_category_scans,
    )
    from services.price_monitor_service import monitor_service
    from services.search_history_service import search_history_service
    from services.rabbitmq_service import rabbitmq_service
    from services.category_worker import process_category_scan_message

    @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(15))
    async def _connect_db():
        logger.info("Attempting to connect to PostgreSQL via Prisma...")
        await db.connect()
        logger.info("Prisma connected to PostgreSQL.")

    await _connect_db()

    # Carrega marcas a partir do banco de dados PostgreSQL (Prisma)
    from services.brand_service import brand_service
    await brand_service.load_from_db()

    # 1) Hidratacao pura — nenhuma task criada ainda (D-01/D-02).
    await monitor_service.load_monitors_async()

    # 2) Reconciliacao de orfaos para busca/cross e scan de categoria, uma
    # unica vez por lifespan, antes que qualquer novo tick seja permitido.
    job_manager.register_reconcile_source(
        "search_history",
        lambda: job_manager.reconcile(search_history_service._repository),
    )
    job_manager.register_reconcile_source(
        "category_scan", reconcile_orphaned_category_scans,
    )
    await job_manager.startup_reconcile()

    # 3) So agora o ciclo de preco orfao de cada monitor e reclamado sob seu
    # active_cycle_id original, e o loop normal comeca para os demais ACTIVE
    # (D-02/D-05/D-06) — nunca antes do passo 2 acima.
    monitor_service.start_loaded_monitor_loops()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        category_monitor_job,
        CronTrigger(minute="*/10", timezone=timezone.utc),
        id="category_monitor_job",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_CATEGORY_MONITOR_SLOT_SECONDS // 2,
    )
    
    from services.category_discovery_cache import pre_warm_all_brands_cache
    scheduler.add_job(
        pre_warm_all_brands_cache,
        CronTrigger(hour="6", minute="0", timezone=timezone(timedelta(hours=-3))), # 6 AM BRT
        id="pre_warm_all_brands_cache",
        kwargs={"force_refresh": True},
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600, # 1 hora de tolerância se o processo estava parado
    )

    from apscheduler.triggers.interval import IntervalTrigger
    from services.category_scheduler_jobs import category_biweekly_enqueue_job
    scheduler.add_job(
        category_biweekly_enqueue_job,
        CronTrigger(day="1,15", hour="6", minute="0", timezone=timezone(timedelta(hours=-3))),
        id="category_biweekly_enqueue_job",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,  # 1 hora de tolerância se o processo estava parado
    )

    scheduler.start()
    logger.info("Monitor de categorias iniciado (slots UTC alinhados de 10 minutos).")

    # Pré-aquece o cache de categorias em background ao iniciar o servidor.
    # Isso garante que, mesmo após restart inesperado na Azure (deploy, health
    # check, etc.), o cache esteja preenchido sem precisar esperar o cron das 6h.
    async def _startup_pre_warm():
        try:
            logger.info("[Startup] Iniciando pre-warm do cache de categorias em background...")
            await pre_warm_all_brands_cache()
            logger.info("[Startup] Pre-warm do cache de categorias concluído.")
        except Exception:
            logger.exception("[Startup] Falha no pre-warm do cache de categorias. Degradando para busca ao vivo.")

    asyncio.create_task(_startup_pre_warm(), name="startup_pre_warm_categories")

    # 4) RabbitMQ consumer — inicia após o scheduler para garantir que o
    # job_manager e os monitors já estão reconciliados antes de processar
    # qualquer mensagem. Falha de conexão ao broker é logada mas NAO
    # interrompe o boot (o worker simplesmente nao estara ativo).
    rabbit_consumer_task = None
    try:
        await rabbitmq_service.connect()
        rabbit_consumer_task = asyncio.create_task(
            rabbitmq_service.start_consumer(process_category_scan_message),
            name="rabbitmq_consumer",
        )
        logger.info("RabbitMQ consumer iniciado (fila: category_scans).")
    except Exception:
        logger.exception(
            "Falha ao conectar/iniciar o consumer RabbitMQ. "
            "O worker de categorias nao estara ativo nesta sessao."
        )

    try:
        yield
    finally:
        # Cancela o consumer RabbitMQ antes de pausar o scheduler para garantir
        # que nenhuma mensagem nova é aceita durante o processo de shutdown.
        if rabbit_consumer_task is not None and not rabbit_consumer_task.done():
            rabbit_consumer_task.cancel()
            try:
                await asyncio.gather(rabbit_consumer_task, return_exceptions=True)
            except Exception:
                pass
        try:
            await rabbitmq_service.close()
        except Exception:
            logger.exception("Falha ao fechar conexão RabbitMQ no shutdown.")
        # Bloqueia novos schedules antes de cancelar qualquer trabalho em
        # andamento (D-05/D-06): o scheduler para de disparar novos ciclos e
        # `job_manager.shutdown()` marca `_closing` antes de qualquer await,
        # entao nenhuma claim nova é concedida durante o cleanup abaixo.
        scheduler.pause()
        try:
            await job_manager.shutdown()
        except Exception:
            logger.exception("Falha ao encerrar job_manager no shutdown.")
        try:
            await _cancel_and_await_tasks(monitor_service.tasks)
        except Exception:
            logger.exception("Falha ao encerrar tasks do monitor de preco no shutdown.")
        scheduler.shutdown(wait=True)
        try:
            from core.db import db
            await db.disconnect()
            logger.info("Prisma disconnected.")
        except Exception:
            logger.exception("Falha ao desconectar Prisma no shutdown.")


app = FastAPI(
    title="OmniPulse - E-Commerce Intelligence API",
    description="API de monitoramento, extração de produtos e inteligência de mercado.",
    version="2.0.0",
    lifespan=lifespan,
)

# Manipulador de erro flat {error, message} para falhas de autenticação Connect / X-API-Key (IAM-01)
register_auth_error_handler(app)

# Traces/métricas HTTP + WebSocket (OBS-01), com hooks de redação de segredos.
if _observability_enabled:
    instrument_fastapi_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_header(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


app.include_router(public_router)
app.include_router(api_router)


@app.get("/health-check")
async def health_check():
    try:
        from core.db import db
        from services.rabbitmq_service import rabbitmq_service
        await asyncio.wait_for(db.query_raw("SELECT 1"), timeout=2.0)
        return {
            "status": "ok",
            "mode": "local",
            "database": "connected",
            "rabbitmq": rabbitmq_service.get_status(),
        }
    except Exception as e:
        from fastapi import HTTPException
        logger.error(f"Health-check failed: {e}")
        raise HTTPException(status_code=503, detail="Database timeout or disconnected")


@app.get("/download-report/{filename}")
async def download_report(filename: str):
    """Serve um relatorio gerado localmente."""
    if "/" in filename or "\\" in filename or not filename.endswith((".xlsx", ".csv")):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Invalid filename")

    if not os.path.exists(filename):
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        filename,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# Um build local pode ser servido pelo backend; no desenvolvimento, use o Vite.
_frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
_FRONTEND_BASE_PATHS = ("/monitor", "/comparativa", "/sku", "/categorias", "/banners", "/marcas")


def install_frontend_routes(target_app: FastAPI, frontend_dist: Path) -> None:
    """Serve somente documentos conhecidos da SPA; APIs e assets mantêm handlers nativos."""
    frontend_index = frontend_dist / "index.html"
    if not frontend_index.is_file():
        @target_app.get("/")
        async def local_frontend_hint():
            return {"message": f"Backend em :{settings.APP_PORT} — Frontend local em http://127.0.0.1:5173"}
        return

    async def read_spa_document():
        return FileResponse(frontend_index)

    target_app.add_api_route("/", read_spa_document, methods=["GET"], include_in_schema=False)
    for frontend_path in _FRONTEND_BASE_PATHS:
        target_app.add_api_route(frontend_path, read_spa_document, methods=["GET"], include_in_schema=False)
    target_app.add_api_route("/comparativa/{job_id:path}", read_spa_document, methods=["GET"], include_in_schema=False)
    target_app.add_api_route("/sku/{job_id:path}", read_spa_document, methods=["GET"], include_in_schema=False)

    assets_dir = frontend_dist / "assets"
    if assets_dir.is_dir():
        target_app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


install_frontend_routes(app, _frontend_dist)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)

