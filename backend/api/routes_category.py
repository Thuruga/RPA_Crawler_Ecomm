"""
Rotas de Categoria: listagem dinâmica e varredura em lote.

GET  /brands/{brand}/categories      — Categorias dinâmicas via VTEX API
GET  /canonical-categories            — Categorias canônicas de/para
POST /category-preview                — Preview do mapeamento de/para
POST /scrape-category                 — Inicia job de varredura (marca única)
POST /scrape-category-multi           — Inicia job de varredura multi-marca
WS   /ws/{job_id}                     — WebSocket para logs em tempo real
"""

import asyncio
import io
import logging
from datetime import datetime
import uuid

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
import pandas as pd
from pydantic import BaseModel, Field
from typing import Any, Optional, List, Dict, Tuple

from services.brand_service import brand_service
from core.constants import MARKETPLACE_ENGINE_KEYS
from services.wake_category_paths import canonicalize_wake_category_source
from core.websocket import manager
from core.job_manager import JOB_CANCEL_FLAGS
from services.engines.base_engine import BaseEngine
from services.engines.factory import engine_factory
from services.category_mapping import (
    get_canonical_categories,
    resolve_category_for_brands,
    get_category_preview,
)
from services.product_contract import build_standard_export_dataframe




logger = logging.getLogger("RoutesCategory")

router = APIRouter()

# Importado de core.constants — NÃO duplicar aqui.
_MARKETPLACES = MARKETPLACE_ENGINE_KEYS


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------
def clean_url(url: str) -> str:
    """Corrige caso a URL venha duplicada (ex: http...http...) ou com espaços."""
    url = url.strip()
    if url.count("http") > 1:
        idx = url.rfind("http")
        url = url[idx:]
    return url


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ScrapeCategoryRequest(BaseModel):
    brand: str
    category_path: Optional[str] = None   # e.g. "/roupas/polos"
    custom_url: Optional[str] = None      # raw URL override
    zipcode: Optional[str] = Field(None, pattern=r"^\d{5}-?\d{3}$")

    def resolved_url(self) -> str:
        if not self.custom_url and not self.category_path:
            raise ValueError("Forneça category_path ou custom_url.")

        if self.custom_url:
            return clean_url(self.custom_url)

        # Tenta resolver o path real usando o mapeamento de/para
        try:
            mapping = resolve_category_for_brands(self.category_path, [self.brand])
            return mapping[self.brand.lower()]["url"]
        except Exception:
            # Fallback para comportamento antigo se não encontrar mapeamento
            brand_info = brand_service.get_brand(self.brand.lower())
            domain = brand_info.domain if brand_info else ""
            path = self.category_path if self.category_path.startswith("/") else f"/{self.category_path}"
            return f"https://{domain}{path}"



class ScrapeMultiBrandRequest(BaseModel):
    """Request para varredura multi-marca com mapeamento de/para ou manual."""
    brands: List[str] = Field(
        ..., min_length=1,
        description="Lista de brand_keys para varrer (ex: ['aramis', 'reserva'])"
    )
    category_slug: Optional[str] = Field(
        None, description="Slug canônico da categoria (ex: 'camisas', 'polos')"
    )
    brand_category_map: Optional[Dict[str, str]] = Field(
        None, description="Mapeamento manual {brand: path/url}"
    )
    zipcode: Optional[str] = Field(None, pattern=r"^\d{5}-?\d{3}$")


class CategoryPreviewRequest(BaseModel):
    """Request para preview do mapeamento de/para."""
    brands: List[str]
    category_slug: str


# ---------------------------------------------------------------------------
# Category-discovery diagnostic (Plano 01-04, D-04) — aditivo, sem duplicar
# logica de categoria: reusa a MESMA CategoryValidationOutcome que o proprio
# engine ja calcula em `_discover_and_validate()` (VTEX/Shopify/Wake/SFCC/Zara,
# Wave 2/3) e o helper ja existente `build_grouped_catalog` — apenas tambem
# le `.probes` (que `get_catalog()` publico descarta). Espelha o mesmo helper
# de `api/routes_brands.py` (mesma decisao, superficie API irmã).
# ---------------------------------------------------------------------------
def _is_catalog_capable(engine: Any) -> bool:
    """True quando o engine sobrescreve `category_has_products` (participa do
    pipeline compartilhado). Engines search-only (Amazon/Mercado Livre/
    Netshoes) nao sobrescrevem — logo sao N/A (D-04)."""
    return type(engine).category_has_products is not BaseEngine.category_has_products


async def _resolve_get_catalog(engine: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Retorna (catalogo agrupado, diagnostico aditivo) para `GET /brands/{b}/categories`.

    O shape de `catalogo` e IDENTICO ao que `engine.get_catalog()` sempre
    retornou (`[{"group":..., "items":[{"label","path"}]}]`) — nenhum campo
    e removido/renomeado. `diagnostico` e um novo campo aditivo no corpo da
    resposta (que ja e um dict, `{"brand":..., "categories":...}` — seguro
    adicionar chave nova sem quebrar o frontend, que só lê `.categories`).
    """
    if not _is_catalog_capable(engine):
        categories = await engine.get_catalog()
        return categories, {
            "catalog_applicability": "not_applicable",
            "state": "not_applicable",
            "offered_count": 0,
            "empty_count": 0,
            "blocked_count": 0,
        }

    discover_and_validate = getattr(engine, "_discover_and_validate", None)
    if discover_and_validate is None:
        categories = await engine.get_catalog()
        state = "ok" if categories else "unknown"
        return categories, {
            "catalog_applicability": "catalog_capable",
            "state": state,
            "offered_count": 0,
            "empty_count": 0,
            "blocked_count": 0,
        }

    from services.category_discovery import ProbeStatus, build_grouped_catalog
    from services.category_discovery_cache import (
        get_cached_outcome,
        set_cached_outcome,
        load_from_db,
        persist_to_db,
    )

    brand_key = getattr(engine, "brand_key", None)

    # --- Camada 1: cache RAM (mais rápido) ---
    outcome = await get_cached_outcome(brand_key) if brand_key else None

    # --- Camada 2: banco de dados (sobrevive a restarts) ---
    if outcome is None and brand_key:
        # A camada de cache é aceleração, não fonte de verdade: banco fora do ar
        # degrada para o crawl ao vivo em vez de virar 500 na rota. Logado como
        # exception para a falha ficar visível — nunca engolida em silêncio.
        try:
            outcome = await load_from_db(brand_key)
        except Exception:
            logger.exception(
                "[%s] Falha ao ler categorias do banco; seguindo para crawl ao vivo.",
                brand_key,
            )
            outcome = None
        if outcome is not None:
            # Restaura cache RAM a partir do banco para requisições subsequentes
            await set_cached_outcome(brand_key, outcome)

    # --- Camada 3: crawl ao vivo (primeira vez ou banco vazio) ---
    if outcome is None:
        outcome = await discover_and_validate()
        if brand_key:
            # Persiste no banco e no cache RAM para não repetir o crawl. Falha de
            # escrita custa uma varredura extra no próximo restart, não a resposta
            # que já temos em mãos.
            try:
                await persist_to_db(brand_key, outcome)
            except Exception:
                logger.exception(
                    "[%s] Falha ao persistir categorias no banco; cache RAM segue válido.",
                    brand_key,
                )
            await set_cached_outcome(brand_key, outcome)

    # Mesmo helper compartilhado que TODO engine catalog-capable já usa em
    # get_catalog() — não é lógica de categoria nova, apenas reaproveitada.
    categories = build_grouped_catalog(outcome.offered)
    probe_values = list(outcome.probes.values())
    empty_count = sum(1 for p in probe_values if p.status == ProbeStatus.EMPTY)
    blocked_count = sum(1 for p in probe_values if p.status == ProbeStatus.BLOCKED)
    offered_count = len(outcome.offered)
    state = "ok" if offered_count > 0 else ("blocked" if blocked_count > 0 else "empty")

    return categories, {
        "catalog_applicability": "catalog_capable",
        "state": state,
        "offered_count": offered_count,
        "empty_count": empty_count,
        "blocked_count": blocked_count,
    }


# ---------------------------------------------------------------------------
# Endpoints — Existentes (retrocompatíveis)
# ---------------------------------------------------------------------------
@router.get("/brands/{brand}/categories")
async def get_categories(brand: str):
    """Retorna categorias agrupadas da marca, buscando via Engine (ex: VTEX API)."""
    brand_key = brand.lower()
    if brand_key in _MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail=f"Marketplaces não possuem categorias próprias para monitoramento: {brand}",
        )
    brand_data = brand_service.get_brand(brand_key)
    if not brand_data:
        raise HTTPException(status_code=404, detail=f"Marca '{brand}' não suportada.")

    engine = engine_factory.get_engine(brand_key)
    categories, diagnostic = await _resolve_get_catalog(engine)
    
    # Injetar categorias mapeadas (DB + canônicas) para garantir que sempre haja opções padronizadas
    from services.category_mapping import _CATEGORY_INDEX
    
    seen_paths = set()
    seen_labels = set()
    for g in categories:
        for it in g.get("items", []):
            p = (it.get("path") or "").strip().rstrip("/").lower()
            lbl = (it.get("label") or "").strip().lower()
            if p:
                seen_paths.add(p)
            if lbl:
                seen_labels.add(lbl)

    mapped_items = []
    # 1. Dinâmicas (Tabela brand_category_mappings)
    if getattr(brand_data, "mappings", None):
        for m in brand_data.mappings:
            path = m.vtex_fq_path if m.vtex_fq_path else f"/{m.canonical_slug}"
            label = m.label or m.canonical_slug
            norm_p = path.strip().rstrip("/").lower()
            norm_l = label.strip().lower()
            if norm_p not in seen_paths and norm_l not in seen_labels:
                seen_paths.add(norm_p)
                seen_labels.add(norm_l)
                mapped_items.append({"label": label, "path": path})

    has_aramis_next_catalog = brand_key == "aramis" and any(
        str(getattr(mapping, "vtex_fq_path", "") or "").startswith("/infantil-next/")
        for mapping in getattr(brand_data, "mappings", [])
    )

    # 2. Canônicas
    for cat in _CATEGORY_INDEX.values():
        # A antiga categoria única `/infantil` foi substituída pela vitrine
        # Next, que possui categorias próprias. Não reintroduzir o fallback
        # legado quando o catálogo Next já está mapeado no banco.
        if has_aramis_next_catalog and cat.slug == "infantil":
            continue
        if brand_key in cat.brands:
            brand_info = cat.brands[brand_key]
            norm_p = brand_info.path.strip().rstrip("/").lower()
            norm_l = cat.label.strip().lower()
            if norm_p not in seen_paths and norm_l not in seen_labels:
                seen_paths.add(norm_p)
                seen_labels.add(norm_l)
                mapped_items.append({"label": cat.label, "path": brand_info.path})
            
    if mapped_items:
        categories.append({
            "group": "Categorias Principais",
            "items": mapped_items
        })
        
    return {"brand": brand, "categories": categories, "category_discovery_state": diagnostic}


# O WebSocket foi movido para api/__init__.py (ws_router) para evitar
# o conflito com OAuth2PasswordBearer, que exige Request HTTP e não WebSocket.


@router.post("/scrape-category")
async def scrape_category(request: ScrapeCategoryRequest, background_tasks: BackgroundTasks):
    brand_lower = request.brand.lower()
    if brand_lower in _MARKETPLACES:
        raise HTTPException(
            status_code=400,
            detail=f"Marketplaces não suportam varredura por categoria: {request.brand}",
        )

    try:
        url = request.resolved_url()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not url:
        raise HTTPException(status_code=400, detail="Forneça category_path ou custom_url.")

    job_id = str(uuid.uuid4())
    
    cancel_event = asyncio.Event()
    JOB_CANCEL_FLAGS[job_id] = cancel_event

    from services.orchestrator import run_orchestrator
    
    async def task_wrapper():
        def log_wrapper(msg):
            asyncio.create_task(manager.send_message(msg, job_id))
            
        try:
            await run_orchestrator(
                marca=request.brand, 
                url_categoria=url, 
                log_callback=log_wrapper, 
                cancel_event=cancel_event,
                job_id=job_id,
                zipcode=request.zipcode,
            )
        except Exception as e:
            logger.exception("Erro fatal no orquestrador de categorias")
            log_wrapper({"type": "job_failed", "message": f"Erro interno: {e}"})
        finally:
            JOB_CANCEL_FLAGS.pop(job_id, None)

    background_tasks.add_task(task_wrapper)
    
    return {"job_id": job_id, "message": "Orquestração iniciada.", "url": url}


# ---------------------------------------------------------------------------
# Endpoints — Multi-Marca (novos)
# ---------------------------------------------------------------------------

@router.get("/canonical-categories")
async def list_canonical_categories():
    """Retorna todas as categorias canônicas agrupadas para o frontend."""
    return {"categories": get_canonical_categories()}


@router.post("/category-preview")
async def preview_category_mapping(request: CategoryPreviewRequest):
    """Retorna o preview do mapeamento de/para antes de iniciar a varredura."""
    preview = get_category_preview(request.category_slug, request.brands)
    if not preview:
        raise HTTPException(
            status_code=404,
            detail=f"Categoria '{request.category_slug}' não encontrada."
        )
    return preview


@router.post("/scrape-category-multi")
async def scrape_category_multi(
    request: ScrapeMultiBrandRequest,
    background_tasks: BackgroundTasks,
):
    """
    Inicia job de varredura multi-marca em paralelo.

    Resolve automaticamente o mapeamento de/para usando o slug canônico,
    OU usa o mapeamento manual enviado pelo frontend.
    """
    # Validar marcas — apenas marcas ativas são alvos válidos de varredura (D-08, OQ2)
    all_brands = {b.brand_key: b for b in brand_service.list_brands(active_only=True)}
    invalid_brands = [b for b in request.brands if b.lower() not in all_brands]
    if invalid_brands:
        raise HTTPException(
            status_code=400,
            detail=f"Marcas não suportadas: {invalid_brands}"
        )

    marketplace_brands = [b for b in request.brands if b.lower() in _MARKETPLACES]
    if marketplace_brands:
        raise HTTPException(
            status_code=400,
            detail=f"Marketplaces não suportam varredura por categoria: {marketplace_brands}"
        )

    url_map = {}
    category_label = "Múltiplas"

    if request.brand_category_map:
        # Modo Manual (Novo fluxo)
        for bk, path_or_url in request.brand_category_map.items():
            bk_lower = bk.lower()
            # Normaliza antes de formar a URL para que a resposta do endpoint,
            # os logs e o engine recebam a mesma fonte canônica. O engine também
            # normaliza como defesa em profundidade para chamadas diretas.
            path_or_url = canonicalize_wake_category_source(bk_lower, path_or_url)
            if path_or_url.startswith("http"):
                url_map[bk_lower] = clean_url(path_or_url)
            else:
                brand_info = all_brands.get(bk_lower)
                domain = brand_info.domain if brand_info else ""
                clean_path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
                url_map[bk_lower] = f"https://{domain}{clean_path}"
        
        if request.category_slug:
            category_label = request.category_slug.title()
    elif request.category_slug:
        # Modo Canônico (Antigo fluxo)
        try:
            brand_url_map = resolve_category_for_brands(
                request.category_slug, request.brands
            )
            url_map = {bk: info["url"] for bk, info in brand_url_map.items()}
            category_label = brand_url_map[request.brands[0].lower()]["label"]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        raise HTTPException(status_code=400, detail="Forneça category_slug ou brand_category_map.")

    job_id = str(uuid.uuid4())
    
    cancel_event = asyncio.Event()
    JOB_CANCEL_FLAGS[job_id] = cancel_event

    # Importar e lançar o orquestrador multi-marca
    from services.orchestrator_multi import run_multi_orchestrator

    background_tasks.add_task(
        run_multi_orchestrator,
        job_id=job_id,
        brand_url_map=url_map,
        category_label=category_label,
        cancel_event=cancel_event,
        zipcode=request.zipcode,
    )

    return {
        "job_id": job_id,
        "message": f"Varredura multi-marca iniciada para '{category_label}'.",
        "brands": list(url_map.keys()),
        "urls": url_map,
    }


# ---------------------------------------------------------------------------
# Exportação em Excel dos Produtos Extraídos
# ---------------------------------------------------------------------------

class CategoryExportItem(BaseModel):
    product_name: str = "Produto"
    brand: Optional[str] = None
    price: Optional[float] = None
    price_original: Optional[float] = None
    url: Optional[str] = None
    image_url: Optional[str] = None
    sku: Optional[str] = None
    is_available: Optional[bool] = True
    available_sizes: List[str] = Field(default_factory=list)
    size_grid: List[str] = Field(default_factory=list)
    shipping_price: Optional[float] = None
    shipping_carrier: Optional[str] = None
    estimated_delivery_days: Optional[int] = None
    is_free_shipping: Optional[bool] = None


class CategoryExportRequest(BaseModel):
    items: List[CategoryExportItem] = Field(..., min_length=1)
    filename: Optional[str] = None


@router.post("/scrape-category/export", summary="Exporta produtos extraídos em Excel")
async def export_category_products(request: CategoryExportRequest):
    """
    Recebe os produtos extraídos exibidos no frontend e devolve um arquivo .xlsx
    com a ordenação e filtros selecionados.
    """
    df = build_standard_export_dataframe([item.model_dump() for item in request.items])
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Produtos Extraídos")
    output.seek(0)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = request.filename or f"produtos_categoria_{timestamp}.xlsx"
    if not filename.endswith(".xlsx"):
        filename = f"{filename}.xlsx"

    return StreamingResponse(
        output,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )




