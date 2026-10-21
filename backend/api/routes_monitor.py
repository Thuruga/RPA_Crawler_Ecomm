"""Rotas locais para monitoramento de categorias."""

import json
import logging
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from core.constants import MARKETPLACE_ENGINE_KEYS
from core.models import CategoryScanRecord
from services.category_monitor_service import (
    get_category_price_history_for_scan_product,
    list_category_scans,
    create_category_monitor as service_create_monitor,
    load_all_monitored_categories,
    delete_category_monitor as service_delete_monitor,
    pause_category_monitor as service_pause_monitor,
    resume_category_monitor as service_resume_monitor,
    run_category_scan,
    load_monitored_categories,
)
from services.review_service import cap_review_pages, fetch_scan_product_review_comments
from core.db import db
from services.rabbitmq_service import rabbitmq_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitor", tags=["Monitoramento de Categorias"])


class CategoryMonitorCreate(BaseModel):
    url: str
    brand: str


class CategoryMonitorResponse(BaseModel):
    id: str
    url: str
    brand: str
    status: str
    last_scraped_at: Optional[str] = None
    last_map_violation_count: Optional[int] = None


class ReviewCommentsRequest(BaseModel):
    max_pages: Optional[int] = None

    model_config = {"extra": "forbid"}


# Importado de core.constants — NÃO duplicar aqui.
_MARKETPLACES = MARKETPLACE_ENGINE_KEYS


@router.post("/category", response_model=CategoryMonitorResponse)
async def create_category_monitor(
    data: CategoryMonitorCreate, background_tasks: BackgroundTasks
):
    if data.brand.lower() in _MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail=f"Marketplaces não suportam monitor de categoria: {data.brand}",
        )
    row = await service_create_monitor(data.url, data.brand)
    background_tasks.add_task(run_category_scan, row)
    return CategoryMonitorResponse(**row)


@router.get("/categories", response_model=List[CategoryMonitorResponse])
async def list_monitored_categories():
    return await load_all_monitored_categories()


@router.delete("/category/{monitor_id}")
async def delete_category_monitor(monitor_id: str):
    await service_delete_monitor(monitor_id)
    return {"status": "ok", "message": "Monitor deletado com sucesso."}


@router.post("/category/{monitor_id}/pause")
async def pause_category_monitor(monitor_id: str):
    updated = await service_pause_monitor(monitor_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Monitor não encontrado")
    return {"status": "paused", "monitor": updated}


@router.post("/category/{monitor_id}/resume")
async def resume_category_monitor(monitor_id: str):
    updated = await service_resume_monitor(monitor_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Monitor não encontrado")
    return {"status": "resumed", "monitor": updated}


@router.get("/category/{monitor_id}/products")
async def get_monitored_products(monitor_id: str):
    try:
        mon_id = int(monitor_id)
    except (ValueError, TypeError):
        return []
    latest_scan = await db.category_scans.find_first(
        where={"monitor_id": mon_id, "status": "completed"},
        order={"started_at": "desc"}
    )
    if not latest_scan:
        latest_scan = await db.category_scans.find_first(
            where={"monitor_id": mon_id},
            order={"id": "desc"}
        )
    if not latest_scan:
        return []
    products = await db.scan_products.find_many(where={"scan_id": latest_scan.id})
    res = []
    for p in products:
        d = p.model_dump(mode="json")
        raw_url = d.get("product_url") or d.get("normalized_url") or ""
        if "secure.oficinareserva.com" in raw_url:
            raw_url = raw_url.replace("secure.oficinareserva.com", "www.oficina.com")
        d["url"] = raw_url
        d["name"] = d.get("raw_title") or ""
        for price_field in ("price_full", "price_discount", "effective_price"):
            val = d.get(price_field)
            if val is not None:
                try:
                    d[price_field] = float(val)
                except (ValueError, TypeError):
                    d[price_field] = None
        res.append(d)
    return res



@router.get("/category/{monitor_id}/scans", response_model=List[CategoryScanRecord])
async def get_category_scans(monitor_id: str):
    """Snapshot autenticado do lifecycle completo de cada category_scan do monitor.

    Expoe integralmente o storage duravel ja produzido por `run_category_scan`
    (Plan 07-05, JOB-01/JOB-02): status/attempt/progress nullable, timestamps,
    error sanitizado e INTERRUPTED distinto de FAILED, sem remapeamento.
    Preserva a semantica ja existente de `list_category_scans`: um monitor_id
    inexistente nao tem scan algum e retorna lista vazia, sem inventar job.
    """
    rows = await list_category_scans(monitor_id)
    return [CategoryScanRecord.model_validate(row) for row in rows]


@router.post("/category/{monitor_id}/products/{scan_product_id}/reviews")
async def fetch_monitored_product_reviews(
    monitor_id: str,
    scan_product_id: str,
    data: ReviewCommentsRequest = ReviewCommentsRequest(),
):
    max_pages = cap_review_pages(data.max_pages)
    try:
        result = await fetch_scan_product_review_comments(
            monitor_id,
            scan_product_id,
            max_pages=max_pages,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result.model_dump(mode="json")


@router.get("/category/{monitor_id}/products/{scan_product_id}/price-history")
async def get_monitored_product_price_history(monitor_id: str, scan_product_id: str):
    points = await get_category_price_history_for_scan_product(monitor_id, scan_product_id)
    if points is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado neste monitor.")
    return {"points": points}


# ---------------------------------------------------------------------------
# Trigger-scan sob demanda via RabbitMQ (Phase 14)
# ---------------------------------------------------------------------------

from pydantic import Field


class TriggerScanRequest(BaseModel):
    brand: str = Field(..., description="Brand key dos monitors a enfileirar (ex: 'aramis').")


class TriggerScanResponse(BaseModel):
    enqueued: List[str]  # lista de monitor_ids enfileirados com sucesso
    reason: Optional[str] = None  # mensagem explicativa se enqueued for vazio


@router.post(
    "/category/trigger-scan",
    response_model=TriggerScanResponse,
    summary="Enfileira scans de todos os monitors ativos de uma marca via RabbitMQ",
)
async def trigger_category_scan(request: TriggerScanRequest) -> TriggerScanResponse:
    """Publica uma mensagem RabbitMQ por monitor ativo da marca informada.

    Retorna HTTP 503 se o broker estiver indisponível — diferentemente do
    cron (fire-and-forget), o chamador explícito precisa saber da falha.
    Busca de monitors é case-insensitive (normaliza para lowercase).
    """
    from services.category_monitor_service import load_monitored_categories
    from services.rabbitmq_service import rabbitmq_service

    brand_key = request.brand.lower().strip()
    all_monitors = await load_monitored_categories()
    monitors = [
        m for m in all_monitors
        if m.get("brand", "").lower() == brand_key
    ]

    if not monitors:
        return TriggerScanResponse(
            enqueued=[],
            reason=f"Nenhum monitor ativo encontrado para a marca '{brand_key}'.",
        )

    enqueued_ids: List[str] = []
    for monitor in monitors:
        try:
            await rabbitmq_service.publish({
                "brand": monitor["brand"],
                "monitor_id": monitor["id"],
            })
            enqueued_ids.append(monitor["id"])
        except Exception as exc:
            if enqueued_ids:
                logger.warning(
                    "Partial enqueue failure for brand '%s'. %d monitors enqueued successfully before failing on '%s'.",
                    brand_key,
                    len(enqueued_ids),
                    monitor["id"]
                )
            raise HTTPException(
                status_code=503,
                detail=f"Falha ao publicar mensagem para monitor {monitor['id']}: {exc}",
            )

    return TriggerScanResponse(enqueued=enqueued_ids)
