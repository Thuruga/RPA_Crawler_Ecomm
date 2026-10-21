"""
Orquestrador Multi-Marca.

Executa varreduras de categoria em paralelo para múltiplas marcas,
consolidando os resultados em um único arquivo Excel.

Fluxo:
    1. Recebe mapeamento {brand → url} do CategoryMapping
    2. Executa em paralelo (asyncio.gather) o pipeline de cada marca
    3. Consolida logs via WebSocket com prefixo por marca
    4. Gera Excel unificado com coluna `brand`
"""

import asyncio
from datetime import datetime, timezone
import time
import logging
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

import pandas as pd

from config import settings
from services.brand_service import brand_service
from services.product_contract import (
    build_canonical_export_dataframe,
    extract_category_product_item,
)
from core.websocket import manager
from core.job_manager import JOB_CANCEL_FLAGS

logger = logging.getLogger("OrchestratorMulti")


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------
@dataclass
class BrandJobResult:
    """Resultado da varredura de uma marca."""
    brand_key: str
    brand_name: str
    products: list = field(default_factory=list)
    total_links: int = 0
    success_count: int = 0
    error_count: int = 0
    error_message: Optional[str] = None
    finished: bool = False


# ---------------------------------------------------------------------------
# Per-brand worker (Async)
# ---------------------------------------------------------------------------
async def _run_brand_pipeline(
    brand_key: str,
    url: str,
    cancel_event: asyncio.Event,
    log_callback: Optional[Callable] = None,
    zipcode: str | None = None,
) -> BrandJobResult:
    """
    Pipeline completo para uma marca: varredura paginada na plataforma.
    """
    started_at = datetime.now(timezone.utc)
    brand_info = brand_service.get_brand(brand_key)
    brand_name = brand_info.brand_name if brand_info else brand_key.title()
    result = BrandJobResult(brand_key=brand_key, brand_name=brand_name)

    def emit(msg_dict):
        if log_callback:
            # Injeta brand_key no log para o frontend distinguir
            msg_dict["brand"] = brand_key
            msg_dict["brand_name"] = brand_name
            log_callback(msg_dict)

    def is_cancelled() -> bool:
        return cancel_event.is_set()

    # ── ETAPA 1: VARREDURA E EXTRAÇÃO PAGINADA ─────────────────────────────
    emit({
        "type": "category_url",
        "brand": brand_key,
        "brand_name": brand_name,
        "url": url,
        "message": f"[{brand_name}] {url}",
    })

    from services.engines.factory import engine_factory
    
    try:
        engine = engine_factory.get_engine(brand_key)
        
        async for produto in engine.run_bulk_scrape(
            category_url=url,
            log_callback=lambda msg: emit(
                msg if isinstance(msg, dict) else {"type": "info", "message": f"[{brand_name}] {msg}"}
            ),
            cancel_event=cancel_event
        ):
            if zipcode:
                from services.category_shipping import enrich_category_shipping
                produto = await enrich_category_shipping(produto, brand_key, zipcode)
            result.products.append(produto)
            result.success_count += 1

            prod_item = extract_category_product_item(produto, default_brand=brand_name)

            emit({
                "type": "category_product_item",
                "brand": brand_key,
                "brand_name": brand_name,
                "product": prod_item,
            })

    except Exception as e:
        result.error_message = str(e)
        result.finished = True
        logger.error(f"Erro em {brand_key}: {e}", exc_info=True)
        emit({"type": "error", "message": f"[{brand_name}] Erro: {e}"})
        return result

    if is_cancelled() and not result.products:
        emit({"type": "cancelled", "message": f"[{brand_name}] Varredura cancelada."})
        result.finished = True
        return result

    if not result.products:
        emit({"type": "error", "message": f"[{brand_name}] Nenhum produto encontrado."})
        result.finished = True
        return result

    # Persistência no banco de dados para a marca
    try:
        from services.category_monitor_service import persist_category_scan_to_db
        scan_db_id = await persist_category_scan_to_db(
            brand_key=brand_key,
            category_url=url,
            products=result.products,
            origin="ad_hoc_multi",
            started_at=started_at,
        )
        if scan_db_id:
            emit({
                "type": "info",
                "message": f"[{brand_name}] Varredura salva no banco de dados (scan_id={scan_db_id}) com {len(result.products)} produtos.",
            })
    except Exception as db_err:
        logger.error(f"Erro ao persistir scan de categoria no banco ({brand_key}): {db_err}", exc_info=True)

    result.total_links = len(result.products)
    result.finished = True

    emit({
        "type": "brand_done",
        "message": f"[{brand_name}] Concluído: {result.success_count} produtos extraídos.",
        "success_count": result.success_count,
        "error_count": result.error_count,
    })

    return result


# ---------------------------------------------------------------------------
# Public: Multi-Brand Orchestrator (Async)
# ---------------------------------------------------------------------------
async def run_multi_orchestrator(
    job_id: str,
    brand_url_map: Dict[str, str],
    category_label: str,
    cancel_event: asyncio.Event,
    zipcode: str | None = None,
):
    """
    Orquestrador principal assíncrono.
    Roda tudo no mesmo loop do FastAPI para reaproveitar conexões.
    """
    
    # Captura o loop ANTES de ir para a thread — será usado pelo log_callback
    loop = asyncio.get_running_loop()

    def log_callback(msg_dict):
        # Funciona tanto no loop principal quanto dentro de threads do executor
        asyncio.run_coroutine_threadsafe(manager.send_message(msg_dict, job_id), loop)

    log_callback({
        "type": "info",
        "message": f"Iniciando varredura multi-marca para '{category_label}': {list(brand_url_map.keys())}",
    })

    # ── Executar em paralelo ───────────────────────────────────────────
    tasks = []
    for brand_key, url in brand_url_map.items():
        tasks.append(_run_brand_pipeline(brand_key, url, cancel_event, log_callback, zipcode))

    # Gather results
    brand_results = await asyncio.gather(*tasks, return_exceptions=True)
    
    results_store: Dict[str, BrandJobResult] = {}
    for i, brand_key in enumerate(brand_url_map.keys()):
        res = brand_results[i]
        if isinstance(res, Exception):
            logger.error(f"Job para {brand_key} falhou: {res}")
            results_store[brand_key] = BrandJobResult(
                brand_key=brand_key, 
                brand_name=brand_key.title(), 
                error_message=str(res), 
                finished=True
            )
        else:
            results_store[brand_key] = res


    # ── Consolidar resultados ──────────────────────────────────────────
    is_cancelled = cancel_event.is_set()

    all_products = []
    total_success = 0
    total_errors = 0

    for brand_key, result in results_store.items():
        total_success += result.success_count
        total_errors += result.error_count
        for produto in result.products:
            # validate_single ja retorna dict via model_dump()
            row = dict(produto) if isinstance(produto, dict) else produto.model_dump()
            row["brand"] = result.brand_name
            all_products.append(row)


    # ── Gerar Excel ────────────────────────────────────────────────────
    slug = category_label.lower().replace(" ", "_").replace("&", "e")
    timestamp = int(time.time())
    arquivo_saida = f"dados_multimarca_{slug}_{timestamp}.xlsx"

    if all_products:
        # Offload pandas operations to a thread para não bloquear o loop
        await loop.run_in_executor(
            None, 
            consolidate_and_save, 
            all_products, 
            arquivo_saida, 
            is_cancelled, 
            results_store, 
            total_success, 
            total_errors,
            log_callback,
        )
    else:
        msg_type = "cancelled" if is_cancelled else "error_done"
        log_callback({
            "type": msg_type,
            "message": (
                "Operação cancelada. Nenhum produto coletado."
                if is_cancelled
                else "Nenhum produto válido extraído de nenhuma marca."
            ),
        })

    # Cleanup
    JOB_CANCEL_FLAGS.pop(job_id, None)


def consolidate_and_save(
    all_products,
    arquivo_saida,
    is_cancelled,
    results_store,
    total_success,
    total_errors,
    log_callback,
):
    """Função auxiliar para salvar o Excel (roda em thread do executor)."""
    try:
        df = build_canonical_export_dataframe(all_products)

        # Ordenar
        sort_cols = []
        if "brand" in df.columns: sort_cols.append("brand")
        if "price_full" in df.columns: sort_cols.append("price_full")
        if sort_cols:
            df = df.sort_values(sort_cols, na_position="last")

        df.to_excel(arquivo_saida, index=False)

        from core.pricing import calculate_price_averages
        brand_averages = {}
        for b_key, b_res in results_store.items():
            if getattr(b_res, "products", None):
                avgs = calculate_price_averages(b_res.products)
                brand_averages[b_key] = {
                    "average_price": avgs.get("average_price"),
                    "average_price_original": avgs.get("average_price_original"),
                    "count": avgs.get("count", avgs.get("total_items", len(b_res.products))),
                }

        msg_type = "cancelled_done" if is_cancelled else "done"
        log_callback({
            "type": msg_type,
            "valid_products": len(all_products),
            "total_success": total_success,
            "total_errors": total_errors,
            "output_file": arquivo_saida,
            "brands_completed": list(results_store.keys()),
            "brand_averages": brand_averages,
            "message": (
                f"{'Cancelado parcial' if is_cancelled else 'Concluído'}! "
                f"{len(all_products)} produtos de "
                f"{len(results_store)} marcas salvos em {arquivo_saida}."
            ),
        })
    except Exception as e:
        logger.error(f"Erro ao salvar Excel multi-marca: {e}")
        log_callback({"type": "error", "message": f"Erro ao gerar arquivo final: {e}"})
