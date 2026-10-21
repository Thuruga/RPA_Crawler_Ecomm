"""
API Router Aggregator.

Registra todos os sub-routers em um único router principal.
Autenticação: X-API-Key (scripts/jobs) ou Bearer token Connect/Clerk (D-05, D-08).
"""

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from typing import Optional
from api.auth import verify_connect_or_internal_key, verify_ws_connect_or_key

from api.routes_product import router as product_router
from api.routes_category import router as category_router
from api.routes_jobs import router as jobs_router
from api.routes_search import router as search_router
from api.routes_brands import router as brands_router
from api.routes_monitor import router as monitor_router
from api.routes_promotions import router as promotions_router

from api.routes_history import router as history_router
from api.routes_banners import assisted_router as assisted_banners_router, router as banners_router
from api.routes_system import router as system_router


# Todos os endpoints da API protegida exigem X-API-Key (scripts) ou Bearer token (Connect/Clerk)
api_router = APIRouter(dependencies=[Depends(verify_connect_or_internal_key)])

api_router.include_router(product_router)
api_router.include_router(category_router)
api_router.include_router(jobs_router)
api_router.include_router(search_router)
api_router.include_router(brands_router)
api_router.include_router(monitor_router)
api_router.include_router(promotions_router)
api_router.include_router(history_router)
api_router.include_router(banners_router)
api_router.include_router(system_router)


# Router público (health-check, WebSocket, etc.)
public_router = APIRouter()
public_router.include_router(assisted_banners_router)
# ---------------------------------------------------------------------------
# WebSocket — autenticação via ?token=<JWT> (navegador) ou ?api_key=<chave> (não-navegador)
# ---------------------------------------------------------------------------
from core.websocket import manager

_ws_router = APIRouter()

@_ws_router.websocket("/ws/{job_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    job_id: str,
    api_key: Optional[str] = None,
    token: Optional[str] = None,
):
    if not await verify_ws_connect_or_key(api_key, token):
        await websocket.close(code=4003, reason="Autenticação inválida")
        return

    await manager.connect(websocket, job_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(job_id)

public_router.include_router(_ws_router)
