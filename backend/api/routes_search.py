"""
Rota de Busca Comparativa Multi-Brand.

POST /search  —  Busca um termo em todas as marcas simultaneamente e retorna
                 os resultados em formato de comparação lado a lado.

Não usa Playwright — chama a API VTEX full-text diretamente (HTTP puro).
"""

from typing import Any, Literal, List, Optional
import io
import json
import re
import uuid
import pandas as pd
import asyncio
from datetime import datetime
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from pydantic import ValidationError

from config import settings
from core.constants import MARKETPLACE_CANONICAL_KEYS, MARKETPLACE_KEY_ALIASES
from core.job_manager import job_manager
from services.brand_service import brand_service
from services.notification_service import notification_store
from core.models import (
    ComparisonResult,
    RawProductBronze,
    SearchProductResult,
    ShippingInfo,
    add_money_values,
    coerce_money_decimal,
    money_decimal_to_json_number,
)
from services.engines.factory import engine_factory
from services.engines.brand_key_utils import normalize_brand_key
from services.review_service import cap_review_pages, get_review_comments

from services.product_contract import build_standard_export_dataframe
from services.search_history_service import search_history_service
from services.shipping.base import apply_shipping_calculation, is_url_allowed_for_brand
from services.shipping.resolver import resolve_shipping_provider

router = APIRouter(prefix="/search", tags=["search"])


# Hosts permitidos para frete de marketplace carregados dinamicamente do brand_service
# (domain + domain_aliases). Não há mais dicionário estático aqui.

def _marketplace_allowed_hosts(marketplace_key: str) -> set[str]:
    """Retorna o conjunto de hosts permitidos para um marketplace,
    lendo domain e domain_aliases diretamente do banco de dados via brand_service."""
    brand = brand_service.get_brand(marketplace_key)
    if brand is None:
        return set()
    hosts: set[str] = set()
    domain = str(getattr(brand, "domain", "") or "").replace("https://", "").replace("http://", "").strip("/")
    if domain:
        hosts.add(domain)
        # Adiciona variação www se não tiver, e vice-versa
        if domain.startswith("www."):
            hosts.add(domain[4:])
        else:
            hosts.add(f"www.{domain}")
    aliases = getattr(brand, "domain_aliases", []) or []
    if isinstance(aliases, str):
        aliases = [aliases]
    for a in aliases:
        a_clean = str(a or "").replace("https://", "").replace("http://", "").strip("/").lower()
        if a_clean:
            hosts.add(a_clean)
    return hosts


def _status_url(job_id: str) -> str:
    """URL de polling autenticado (D-08) — job_id sempre url-encoded."""
    return f"/history/{quote(job_id, safe='')}"


class JobAccepted(BaseModel):
    """Resposta imediata de um POST de busca que agora roda em background."""

    job_id: str
    status: str
    status_url: str
    poll_after_ms: int = 1500


def _has_export_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        cleaned = value.strip()
        return bool(cleaned) and cleaned.lower() != "none"
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _seed_search_export_product(
    product: SearchProductResult,
    brand_name: str,
) -> dict[str, Any]:
    return {
        "brand": brand_name or product.brand,
        "url": product.url,
        "product_name": product.product_name,
        "price_full": product.price_full,
        "price_discount": product.price_discount,
        "price_discount_is_delta": product.price_discount_is_delta,
        "image_url": product.image_url,
        "category": product.category,
        "product_code": product.product_code,
        "review_product_id": product.review_product_id or product.product_code,
        "available": product.available,
        "rating": product.rating,
        "review_count": product.review_count,
        "available_colors": product.available_colors,
        "available_sizes": product.available_sizes,
        # D-01: forwarding aditivo dos campos de proveniencia bruta ja
        # fornecidos pelo engine — nunca reconstruidos/inferidos aqui.
        "raw_colors": product.raw_colors,
        "raw_sizes": product.raw_sizes,
        "seller": product.seller,
        "sku_id": product.sku_id,
        "seller_id": product.seller_id,
        "is_free_shipping": product.is_free_shipping,
        "shipping_price": product.shipping_price,
        "landed_price": product.landed_price,
    }


def _merge_search_export_product(
    product: SearchProductResult,
    brand_name: str,
    details: Optional[dict[str, Any]],
) -> dict[str, Any]:
    merged = _seed_search_export_product(product, brand_name)
    detail_payload = dict(details or {})
    detail_price = detail_payload.pop("price", None)
    if _has_export_value(detail_price) and not _has_export_value(
        detail_payload.get("price_full")
    ):
        detail_payload["price_full"] = detail_price

    for key, value in detail_payload.items():
        if _has_export_value(value):
            merged[key] = value

    merged["brand"] = brand_name or merged.get("brand")
    return merged


def _normalize_search_product_price(product: SearchProductResult) -> SearchProductResult:
    if (
        not product.price_discount_is_delta
        or product.price_full is None
        or product.price_discount is None
    ):
        return product

    payload = product.model_dump(mode="json")
    payload["price_full"] = round(product.price_full + product.price_discount, 2)
    payload["price_discount"] = round(product.price_full, 2)
    payload["price_discount_is_delta"] = False
    payload["landed_price"] = None
    return SearchProductResult.model_validate(payload)


def _normalize_brand_results_for_response(
    brand_results: List[Any],
) -> List[Any]:
    from core.pricing import calculate_price_averages

    normalized: list[Any] = []
    for brand_result in brand_results:
        norm_prods = [
            _normalize_search_product_price(product)
            for product in brand_result.products
        ]
        b_avg = calculate_price_averages(norm_prods)
        normalized.append(
            brand_result.model_copy(
                update={
                    "products": norm_prods,
                    "average_price": b_avg["average_price"],
                    "average_price_original": b_avg["average_price_original"],
                }
            )
        )
    return normalized





# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    """Payload da busca comparativa."""

    query: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Termo de busca livre. Ex: 'Polo Piquet', 'Camisa Listrada'.",
        examples=["Polo Piquet"],
    )
    brands: Optional[List[str]] = Field(
        default=None,
        description=(
            "Lista de marcas para pesquisar. "
            "Se omitido, busca em todas as marcas cadastradas e marketplaces virtuais. "
            "Valores válidos: 'aramis', 'reserva', 'tommy', 'mercadolivre', 'netshoes'."
        ),
        examples=[["aramis", "reserva", "tommy", "mercadolivre", "netshoes"]],
    )
    max_per_brand: Optional[int] = Field(
        default=10,
        ge=1,
        description="Máximo de produtos por marca; null busca todos os resultados disponíveis.",
    )
    sort: Optional[str] = Field(
        default="relevance",
        description="Ordenação: 'relevance', 'price_asc', 'price_desc', 'top_selling'.",
    )
    zipcode: Optional[str] = Field(
        default=None,
        pattern=r"^\d{5}-?\d{3}$",
        description="CEP de destino para o cálculo do frete"
    )
    include_shipping: bool = Field(
        default=False,
        description="Incluir a extração do valor de frete nos motores que suportam"
    )

class CrossMarketplaceRequest(BaseModel):
    target_sku: str = Field(..., description="SKU base para referência.")
    brand_key: Optional[str] = Field(
        default="aramis",
        description="Chave da marca de origem do SKU (ex: 'aramis', 'reserva', 'tommy'). Usada para buscar o produto canonical no catálogo VTEX da marca.",
    )
    search_query: Optional[str] = Field(None, description="Query estrita/específica. Se vazia, será extraída da VTEX.")
    broad_query: Optional[str] = Field(None, description="Query ampla para buscar volume.")
    min_score: float = Field(55.0, description="Match score mínimo.")
    zipcode: Optional[str] = Field(None, pattern=r"^\d{5}-?\d{3}$", description="CEP de destino")



class SearchConfigResponse(BaseModel):
    """Resposta do endpoint de configuração de busca — somente dados não-sensíveis."""

    default_cep: str = Field(
        ...,
        description="CEP padrão configurado no backend para cálculo de frete.",
    )


class CalculateShippingRequest(BaseModel):
    marketplace: str = Field(
        ..., description="Nome do marketplace (amazon, mercado_livre, netshoes, dafiti, privalia)"
    )
    url: str = Field(..., description="URL do produto no marketplace")
    zipcode: str = Field(..., pattern=r"^\d{5}-?\d{3}$", description="CEP de destino")

    @field_validator("marketplace", mode="before")
    @classmethod
    def _normalize_marketplace(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = normalize_brand_key(value).replace("-", "")
        # Normaliza aliases (ex: "mercadolivre" -> "mercado_livre")
        canonical = MARKETPLACE_KEY_ALIASES.get(normalized, normalized)
        if canonical not in MARKETPLACE_CANONICAL_KEYS:
            raise ValueError(f"Marketplace '{value}' não suportado. Use: {sorted(MARKETPLACE_CANONICAL_KEYS)}")
        return canonical

class CalculateVtexShippingRequest(BaseModel):
    """Cálculo de frete VTEX sob demanda para um produto da busca comparativa."""

    brand_key: str = Field(..., min_length=1, description="Chave da marca VTEX (ex: 'aramis', 'foxton').")
    sku_id: str = Field(..., min_length=1, description="itemId do SKU (vem do produto da busca).")
    seller_id: str = Field(default="1", description="ID do seller do SKU.")
    zipcode: str = Field(..., pattern=r"^\d{5}-?\d{3}$", description="CEP de destino.")


class CalculateBrandShippingRequest(BaseModel):
    """Calculo de frete sob demanda para marcas nao-VTEX."""

    brand_key: str = Field(..., min_length=1, description="Chave da marca.")
    product_url: str = Field(..., min_length=1, description="URL do produto na marca.")
    zipcode: str = Field(..., pattern=r"^\d{5}-?\d{3}$", description="CEP de destino.")
    price: Optional[float] = Field(default=None, description="Preco do item para checagem de regras de frete gratis.")


class CalculateBrandShippingResponse(BaseModel):
    state: str
    shipping_options: List[ShippingInfo] = Field(default_factory=list)
    shipping: Optional[ShippingInfo] = None
    shipping_price: Optional[float] = None
    is_free_shipping: bool = False
    message: Optional[str] = None

    @field_validator("shipping_price")
    @classmethod
    def shipping_price_must_be_valid_money(cls, v: Optional[float]) -> Optional[float]:
        amount = coerce_money_decimal(v, field_name="shipping_price")
        if amount is None:
            return None
        if amount < 0:
            raise ValueError("shipping_price must be non-negative")
        return money_decimal_to_json_number(amount)

class ProductDetailsRequest(BaseModel):
    """Payload para obter os detalhes completos de um produto sob demanda (modal)."""

    brand: str = Field(..., min_length=1, description="Chave da marca ou marketplace virtual (ex: 'aramis', 'mercado_livre').")
    url: str = Field(..., min_length=1, description="URL do produto.")


# Marketplaces virtuais sem registro de marca em brands.json (mesma guarda usada
# pela SettingsPage no frontend) — únicas chaves aceitas quando brand_service.get_brand
# não encontra a marca; qualquer outra chave desconhecida é 400 (nunca busca host
# arbitrário sob uma marca inexistente).
_VIRTUAL_MARKETPLACE_KEYS = {"mercadolivre", "netshoes", "amazon"}


class ProductReviewsRequest(BaseModel):
    """Payload para buscar avaliações sob demanda de um produto individual."""

    brand: str
    product_id: str
    max_pages: Optional[int] = None

    model_config = {"extra": "forbid"}


@router.post(
    "/product/reviews",
    summary="Avaliações sob demanda de um produto individual (modal de produto)",
    description=(
        "Resolve a marca e delega para get_review_comments, retornando o "
        "ReviewCommentsResult (reviews_state disponível/não suportado/bloqueado/"
        "erro) como JSON. Nunca 500 — bloqueio/erro do provedor degrada para "
        "reviews_state, nunca é convertido em HTTP 5xx."
    ),
)
async def product_reviews(request: ProductReviewsRequest):
    brand_key = request.brand.lower().strip()
    brand = brand_service.get_brand(brand_key)

    if not brand and normalize_brand_key(brand_key) not in _VIRTUAL_MARKETPLACE_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Marca '{brand_key}' não encontrada.",
        )

    max_pages = cap_review_pages(request.max_pages)
    result = await get_review_comments(brand_key, request.product_id, max_pages)
    return result.model_dump(mode="json")


@router.post(
    "/product/details",
    summary="Detalhes completos de um produto sob demanda (modal de produto)",
    description=(
        "Resolve o engine da marca/marketplace e retorna o RawProductBronze completo "
        "(cores, tamanhos, descrição, composição, especificações, breadcrumb, preço, "
        "estoque) para o modal de detalhes de produto. Reaproveita o mesmo caminho do "
        "monitor de preço (engine.get_pdp_product). Nunca 500 — bloqueio/erro degrada "
        "para state='unavailable'/'error' com product=null."
    ),
)
async def product_details(request: ProductDetailsRequest):
    brand_key = request.brand.lower().strip()
    brand = brand_service.get_brand(brand_key)

    if brand:
        if not is_url_allowed_for_brand(request.url, brand):
            raise HTTPException(
                status_code=400,
                detail="URL do produto não pertence ao domínio da marca.",
            )
    else:
        # Marca virtual de marketplace (sem registro): domínio é o do próprio
        # marketplace, não há checagem de is_url_allowed_for_brand a aplicar.
        # Engine desconhecido (nem marca real, nem marketplace virtual) -> 400.
        if normalize_brand_key(brand_key) not in _VIRTUAL_MARKETPLACE_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"Marca '{brand_key}' não encontrada.",
            )

    product_data = None
    try:
        if brand:
            from core.db import db
            from services.url_utils import normalize_url
            import json

            norm_url = normalize_url(request.url)

            # 1. Tentar achar no scan_products
            # Buscamos pela URL normalizada ou variações de host (ex: migrações de domínio)
            url_variations = list({norm_url, request.url})
            if "secure.oficinareserva.com" in request.url:
                alt = request.url.replace("secure.oficinareserva.com", "www.oficina.com")
                url_variations.extend([alt, normalize_url(alt)])
            elif "oficina.com" in request.url:
                alt = request.url.replace("www.oficina.com", "secure.oficinareserva.com").replace("oficina.com", "secure.oficinareserva.com")
                url_variations.extend([alt, normalize_url(alt)])

            scan_prod = await db.scan_products.find_first(
                where={
                    "OR": [
                        {"normalized_url": {"in": url_variations}},
                        {"product_url": {"in": url_variations}},
                    ]
                },
                order={"captured_at": "desc"}
            )
            if scan_prod:
                specs = json.loads(scan_prod.specifications) if isinstance(scan_prod.specifications, str) else scan_prod.specifications
                
                cached_payload = {
                    "url": request.url,
                    "brand": brand_key,
                    "raw_title": scan_prod.raw_title,
                    "raw_description": scan_prod.raw_description,
                    "image_url": scan_prod.image_url,
                    "price_full": float(scan_prod.price_full) if scan_prod.price_full else None,
                    "price_discount": float(scan_prod.price_discount) if scan_prod.price_discount else None,
                    "available_colors": scan_prod.available_colors,
                    "available_sizes": scan_prod.available_sizes,
                    "breadcrumb": scan_prod.breadcrumb,
                    "composition": scan_prod.composition,
                    "specifications": specs or {},
                    "stock_availability": scan_prod.stock_availability,
                    "product_code": scan_prod.product_code,
                    "review_product_id": scan_prod.review_product_id or scan_prod.product_code,
                    "last_success_at": scan_prod.captured_at.isoformat() if scan_prod.captured_at else None,
                }
                # Sincronia de Preço: sobrescrever preço do scan pelo preço fresquinho do monitor, se existir
                monitor = await db.price_monitors.find_first(
                    where={
                        "OR": [
                            {"normalized_url": norm_url},
                            {"product_url": request.url}
                        ],
                        "source_key": brand_key
                    }
                )
                if monitor:
                    cached_payload["price_full"] = float(monitor.last_price_original) if monitor.last_price_original else None
                    cached_payload["price_discount"] = float(monitor.last_price_discount) if monitor.last_price_discount else float(monitor.last_price) if monitor.last_price else None

                try:
                    product_data = RawProductBronze.model_validate(cached_payload)
                except Exception as e:
                    import logging
                    logging.getLogger("routes_search").warning(f"Erro ao validar RawProductBronze do scan_products: {e}")

                # Se o cache do scan_products não tiver detalhes ricos de PDP (ex: veio de varredura
                # de catálogo onde composition é None e raw_description é igual ao raw_title ou vazio),
                # tenta enriquecer via live PDP scrape e atualizar o registro no banco.
                has_rich_details = (
                    bool(scan_prod.composition)
                    or (
                        bool(scan_prod.raw_description)
                        and scan_prod.raw_description.strip().lower() != scan_prod.raw_title.strip().lower()
                    )
                ) and bool(scan_prod.available_sizes or scan_prod.available_colors)
                if not has_rich_details:
                    try:
                        engine = engine_factory.get_engine(brand_key)
                        live_pdp = await engine.get_pdp_product(request.url)
                        if live_pdp:
                            live_payload = (
                                live_pdp.model_dump()
                                if isinstance(live_pdp, RawProductBronze)
                                else dict(live_pdp)
                            )
                            live_comp = live_payload.get("composition")
                            live_desc = live_payload.get("raw_description")
                            live_specs = live_payload.get("specifications") or {}
                            live_colors = live_payload.get("available_colors") or []
                            live_sizes = live_payload.get("available_sizes") or []

                            has_live_rich = (
                                bool(live_comp)
                                or (
                                    bool(live_desc)
                                    and str(live_desc).strip().lower() != str(scan_prod.raw_title).strip().lower()
                                )
                                or bool(live_colors)
                                or bool(live_sizes)
                                or bool(live_specs)
                            )
                            if has_live_rich:
                                try:
                                    validated_live = RawProductBronze.model_validate(live_payload)
                                    product_data = validated_live

                                    update_data = {}
                                    if live_comp and not scan_prod.composition:
                                        update_data["composition"] = live_comp
                                    if live_desc and (not scan_prod.raw_description or scan_prod.raw_description == scan_prod.raw_title):
                                        update_data["raw_description"] = live_desc
                                    if live_specs and (not specs or specs == {}):
                                        update_data["specifications"] = json.dumps(live_specs)
                                    if live_colors and not scan_prod.available_colors:
                                        update_data["available_colors"] = live_colors
                                    if live_sizes and not scan_prod.available_sizes:
                                        update_data["available_sizes"] = live_sizes
                                    if live_payload.get("raw_colors") and not scan_prod.raw_colors:
                                        update_data["raw_colors"] = live_payload["raw_colors"]
                                    if live_payload.get("raw_sizes") and not scan_prod.raw_sizes:
                                        update_data["raw_sizes"] = live_payload["raw_sizes"]
                                    if live_payload.get("breadcrumb") and not scan_prod.breadcrumb:
                                        update_data["breadcrumb"] = live_payload["breadcrumb"]
                                    if update_data:
                                        await db.scan_products.update(
                                            where={"id": scan_prod.id},
                                            data=update_data
                                        )

                                except Exception as live_val_err:
                                    logging.getLogger("routes_search").warning(
                                        f"Erro ao validar/salvar produto enriquecido ao vivo: {live_val_err}"
                                    )
                    except Exception as live_err:
                        logging.getLogger("routes_search").info(
                            f"Live PDP enrichment indisponível ({brand_key}, {request.url}): {live_err}. Mantendo cache existente."
                        )

    except Exception as e:
        import logging
        logging.getLogger("routes_search").error(f"Erro catastrofico ao buscar cache no DB: {e}")

    # Fallback para Live Scrape
    if not product_data:
        try:
            engine = engine_factory.get_engine(brand_key)
            product_data = await engine.get_pdp_product(request.url)
        except Exception as e:
            import logging
            logging.getLogger("routes_search").error(
                f"Erro ao buscar detalhes do produto ({brand_key}, {request.url}): {e}"
            )
            return {"state": "error", "product": None}

    if not product_data:
        return {"state": "unavailable", "product": None}

    marketplace_read_state = (
        product_data.get("_marketplace_read_state")
        if isinstance(product_data, dict) else None
    )
    if marketplace_read_state in {"blocked", "unsupported"}:
        return {
            "state": marketplace_read_state,
            "product": None,
            "last_success_at": product_data.get("last_success_at"),
            "next_retry_at": product_data.get("next_retry_at"),
        }

    try:
        payload = (
            product_data.model_dump()
            if isinstance(product_data, RawProductBronze)
            else dict(product_data)
        )
        # Mesma tolerância do monitor de preço (_monitor_loop): imagem ausente/
        # inválida não pode derrubar a validação de um produto que, no modal,
        # é exibido mesmo sem foto.
        img = payload.get("image_url")
        if not img or not str(img).strip() or str(img) == "None":
            payload.pop("image_url", None)
        product = RawProductBronze.model_validate(payload)
    except (ValidationError, ValueError, TypeError) as e:
        import logging
        logging.getLogger("routes_search").warning(
            f"Produto inválido em /product/details ({brand_key}, {request.url}): {e}"
        )
        return {"state": "error", "product": None}

    return {
        "state": "stale" if marketplace_read_state == "stale" else "ok",
        "product": product.model_dump(mode="json"),
        "last_success_at": product_data.get("last_success_at") if isinstance(product_data, dict) else None,
        "next_retry_at": product_data.get("next_retry_at") if isinstance(product_data, dict) else None,
    }



# ---------------------------------------------------------------------------
# Export helpers and models — POST /cross-marketplace/export
# ---------------------------------------------------------------------------

FORMULA_CHARS = ("=", "+", "-", "@")


def _sanitize_cell(value):
    """Prepend single-quote to strings starting with a formula character (injection guard)."""
    if isinstance(value, str):
        candidate = value.lstrip(" \t\r\n")
        if candidate.startswith(FORMULA_CHARS):
            return "'" + value
    return value

class ExportItem(BaseModel):
    marketplace: str
    seller: str
    title: str
    price: float
    shipping_price: Optional[float] = None
    landed_price: float
    is_free_shipping: bool = False
    final_match_score: float = 0.0
    match_score: float = 0.0
    is_similar: bool = False
    url: str
    rating: Optional[float] = None
    review_count: Optional[int] = None
    reviews_state: Optional[str] = None

    display_order: Optional[int] = Field(None, alias="_display_order")

    model_config = {"extra": "allow", "populate_by_name": True}

    @field_validator("price", "shipping_price", "landed_price")
    @classmethod
    def money_fields_must_be_valid(cls, v: Optional[float]) -> Optional[float]:
        amount = coerce_money_decimal(v)
        if amount is None:
            return None
        if amount < 0:
            raise ValueError("money fields must be non-negative")
        return money_decimal_to_json_number(amount)

    @model_validator(mode="after")
    def zero_shipping_requires_explicit_free_state(self):
        amount = coerce_money_decimal(self.shipping_price, field_name="shipping_price")
        if amount == 0 and not self.is_free_shipping:
            raise ValueError("zero shipping requires is_free_shipping")
        if self.is_free_shipping and amount is not None and amount != 0:
            raise ValueError("free shipping requires zero shipping_price")
        return self

class CrossMarketplaceExportRequest(BaseModel):
    items: List[ExportItem] = Field(..., min_length=1, max_length=500)
    search_query: Optional[str] = None
    target_sku: str = Field(..., min_length=1)




# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "",
    response_model=JobAccepted,
    status_code=202,
    summary="Busca comparativa multi-brand (job em background)",
    description=(
        "Persiste o job e agenda a busca em background (D-08). Retorna "
        "imediatamente job_id/status_url; acompanhe via GET /history/{job_id}."
    ),
)
async def search_products(request: SearchRequest) -> JobAccepted:
    """Valida e persiste o input completo, agenda o worker reproduzível e
    retorna 202 antes de qualquer I/O externo (scraping)."""

    # list_brands(active_only=True) já inclui os marketplaces (mercado_livre,
    # netshoes, amazon) como entradas reais em brands.json (Plan 04 / D-10) —
    # nenhum .extend() hardcoded é necessário (era a 2ª fonte de verdade que
    # duplicava cada marketplace na lista-alvo e quebrava ao desativar um deles).
    all_brands = [b.brand_key for b in brand_service.list_brands(active_only=True)]
    if request.brands:
        invalid = [b for b in request.brands if b.lower() not in all_brands]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Marcas inválidas: {invalid}. "
                    f"Marcas suportadas: {all_brands}"
                ),
            )

    target_brands = (
        [b.lower() for b in request.brands]
        if request.brands
        else all_brands
    )
    # Salvaguarda: dedupe preservando ordem (dict.fromkeys, NÃO set — a ordem das
    # colunas no front depende desta sequência).
    target_brands = list(dict.fromkeys(b.lower() for b in target_brands))

    clean_zipcode = request.zipcode.replace("-", "") if request.zipcode else None

    job_id = str(uuid.uuid4())
    # DEVIATION 1 (Pitfall 2): store raw query term — NOT a composed display label.
    # SearchPage reopen (App.tsx:656) dumps res.query back into the search box;
    # a label string would corrupt it. Label composition happens in the frontend.
    # D-08: input completo (marcas, limite, ordenação, CEP, frete)
    # é persistido ANTES do schedule — nenhum I/O externo roda antes do 202.
    created = search_history_service.create_job(
        job_id=job_id,
        query=request.query,
        brands=target_brands,
        type="search",
        max_per_brand=request.max_per_brand,
        sort=request.sort,
        zipcode=clean_zipcode,
        include_shipping=request.include_shipping,
    )

    await job_manager.schedule(
        job_id,
        lambda: search_history_service.run_search_job(job_id, created.input),
        record_id=job_id,
    )

    return JobAccepted(
        job_id=job_id,
        status="pending",
        status_url=_status_url(job_id),
        poll_after_ms=1500,
    )


@router.get(
    "",
    response_model=ComparisonResult,
    summary="Busca comparativa via GET (conveniência)",
    description="Versão GET do endpoint de busca para testes rápidos via browser.",
)
async def search_products_get(
    q: str = Query(..., min_length=2, max_length=100, description="Termo de busca"),
    max_per_brand: Optional[int] = Query(default=10, ge=1),
    sort: Optional[str] = Query(default="relevance"),
    zipcode: Optional[str] = Query(None, pattern=r"^\d{5}-?\d{3}$", description="CEP de destino"),
    include_shipping: bool = Query(default=False)
) -> ComparisonResult:
    """Atalho GET para facilitar testes direto pelo browser/swagger."""
    clean_zipcode = zipcode.replace("-", "") if zipcode else None

    brand_results = await engine_factory.search_all_brands(
        query=q,
        brands=None,  # Todas as marcas
        max_per_brand=max_per_brand,
        sort=sort,
        zipcode=clean_zipcode,
        include_shipping=include_shipping
    )
    brand_results = _normalize_brand_results_for_response(brand_results)

    # search_all_brands roda com brands=None → usa list_brands(active_only=True)
    # como fonte única. Montar brands_searched da MESMA fonte (sem .extend) mantém
    # a metadata alinhada com o que foi de fato buscado (D-10).
    all_brands = [b.brand_key for b in brand_service.list_brands(active_only=True)]

    all_products = [p for b in brand_results for p in b.products]
    from core.pricing import calculate_price_averages
    global_avg = calculate_price_averages(all_products)

    notification_store.add_notification(
        title="Busca Concluída",
        message=f"Busca por '{q}' finalizada com {len(brand_results)} resultados.",
        tab="search"
    )

    return ComparisonResult(
        query=q,
        brands_searched=all_brands,
        results=brand_results,
        average_price=global_avg["average_price"],
        average_price_original=global_avg["average_price_original"],
        brand_averages=global_avg["brand_averages"],
    )


@router.get(
    "/config",
    response_model=SearchConfigResponse,
    summary="Configuração de busca (somente leitura)",
    description=(
        "Retorna dados de configuração não-sensíveis para o frontend, "
        "como o CEP padrão usado no cálculo de frete. "
        "Não requer autenticação e não expõe segredos (T-33-01)."
    ),
)
async def get_search_config() -> SearchConfigResponse:
    """Endpoint read-only que expõe DEFAULT_CEP para o frontend (D-04).

    Sem acesso a banco, sem segredos — somente leitura de settings.
    CEP nunca é interpolado em URL; apenas retornado no corpo JSON.
    """
    return SearchConfigResponse(default_cep=settings.DEFAULT_CEP)


@router.post(
    "/export",
    summary="Exporta a busca comparativa em Excel",
    description="Pesquisa um termo em todas as marcas (ou filtradas) e retorna um arquivo .xlsx."
)
async def export_search_products(request: SearchRequest):
    """
    Executa a busca comparativa e retorna o resultado como download de arquivo Excel.
    """
    # list_brands(active_only=True) já inclui os marketplaces como entradas reais
    # (Plan 04 / D-10) — sem .extend() hardcoded redundante.
    all_brands = [b.brand_key for b in brand_service.list_brands(active_only=True)]
    if request.brands:
        invalid = [b for b in request.brands if b.lower() not in all_brands]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Marcas inválidas: {invalid}. Marcas suportadas: {all_brands}",
            )

    target_brands = (
        [b.lower() for b in request.brands]
        if request.brands
        else all_brands
    )
    # Salvaguarda: dedupe preservando ordem (dict.fromkeys, NÃO set).
    target_brands = list(dict.fromkeys(b.lower() for b in target_brands))

    clean_zipcode = request.zipcode.replace("-", "") if request.zipcode else None

    brand_results = await engine_factory.search_all_brands(
        query=request.query,
        brands=target_brands,
        max_per_brand=request.max_per_brand,
        sort=request.sort,
        zipcode=clean_zipcode,
        include_shipping=request.include_shipping
    )
    brand_results = _normalize_brand_results_for_response(brand_results)

    # -------------------------------------------------------------------------
    # Enriquece cada card de busca com PDP quando esse detalhe realmente agrega
    # dados, mas preserva a linha original para engines parciais/sparsos.
    # -------------------------------------------------------------------------
    tasks = []
    # Cap de round-trips concorrentes de enrichment para evitar 429 em engines
    # VTEX headless (Foxton/Wake) sem serializar o fan-out inteiro do export.
    enrich_semaphore = asyncio.Semaphore(8)

    async def fetch_full_product(brand_key, brand_name, product):
        try:
            async with enrich_semaphore:
                engine = engine_factory.get_engine(brand_key)
                prod_dict = await engine.get_pdp_product(product.url)
                return _merge_search_export_product(product, brand_name, prod_dict)
        except Exception as e:
            import logging
            logging.getLogger("routes_search").warning(
                f"Erro ao buscar detalhes de {product.url}: {e}"
            )
        return _merge_search_export_product(product, brand_name, None)

    for brand_res in brand_results:
        brand_key = brand_res.brand_key
        brand_name = brand_res.brand_name
        for p in brand_res.products:
            tasks.append(fetch_full_product(brand_key, brand_name, p))

    full_products = await asyncio.gather(*tasks)
    data = [p for p in full_products if p]

    df = build_standard_export_dataframe(data)
    if not df.empty:
        sort_cols = []
        if "brand" in df.columns:
            sort_cols.append("brand")
        if "price_full" in df.columns:
            sort_cols.append("price_full")
        if sort_cols:
            df = df.sort_values(sort_cols, na_position="last")

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Comparativo')
        
        # Opcional: auto-ajustar largura das colunas se openpyxl permitir de forma simples
        # Mas para garantir robustez sem erro, usaremos o padrão
    output.seek(0)

    # Formatar nome do arquivo
    safe_query = "".join([c if c.isalnum() else "_" for c in request.query])
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"busca_comparativa_{safe_query}_{timestamp}.xlsx"

    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Access-Control-Expose-Headers': 'Content-Disposition'
    }

    return StreamingResponse(
        output,
        headers=headers,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@router.post(
    "/cross-marketplace",
    response_model=JobAccepted,
    status_code=202,
    summary="Busca cruzada em Marketplaces (ML, NS, AMZ) — job em background",
    description=(
        "Persiste o SKU/thresholds e agenda a busca em background (D-08). "
        "O lookup VTEX de referência e a comparação nos marketplaces rodam "
        "inteiramente no worker — nenhum I/O externo roda antes do 202."
    ),
)
async def cross_marketplace_search(request: CrossMarketplaceRequest) -> JobAccepted:
    """Valida e persiste o input completo (SKU, queries, limiar, CEP), agenda
    o worker reproduzível e retorna 202 antes de qualquer lookup/scraping."""

    job_id = str(uuid.uuid4())
    display_query = request.search_query or request.broad_query or request.target_sku
    created = search_history_service.create_job(
        job_id=job_id,
        query=f"SKU: {display_query}",
        brands=["mercadolivre", "netshoes", "amazon"],
        type="cross",
        target_sku=request.target_sku,
        brand_key=request.brand_key or "aramis",
        search_query=request.search_query,
        broad_query=request.broad_query,
        min_score=request.min_score,
        zipcode=request.zipcode,
    )

    await job_manager.schedule(
        job_id,
        lambda: search_history_service.run_cross_search_job(job_id, created.input),
        record_id=job_id,
    )

    return JobAccepted(
        job_id=job_id,
        status="pending",
        status_url=_status_url(job_id),
        poll_after_ms=1500,
    )

@router.post("/cross-marketplace/export", summary="Exporta busca por SKU em Excel")
async def export_cross_marketplace(request: CrossMarketplaceExportRequest):
    """
    Recebe os itens já exibidos no frontend e devolve um .xlsx com 10 colunas PT-BR.
    Não re-executa a busca nem re-raspa nenhum produto (EXPORT-05 fidelity).
    """
    sorted_items = sorted(
        request.items,
        key=lambda i: i.display_order if i.display_order is not None else float('inf'),
    )


    rows = []
    for item in sorted_items:
        row = item.model_dump(mode="json")
        row["brand"] = item.marketplace
        row["product_name"] = item.title
        rows.append(row)

    df = build_standard_export_dataframe(rows)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Busca SKU")
    output.seek(0)

    query_token = request.search_query or request.target_sku
    safe_query = "".join(c if c.isalnum() else "_" for c in query_token)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"busca_sku_{safe_query}_{timestamp}.xlsx"

    return StreamingResponse(
        output,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _validate_marketplace_url(marketplace: str, url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="URL invalida para calculo de frete.")

    host = (parsed.hostname or "").lower()
    # Hosts carregados dinamicamente do banco — sem dicionário estático fixo.
    allowed_hosts = _marketplace_allowed_hosts(marketplace)
    if not allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=f"Marketplace '{marketplace}' não encontrado ou inativo no sistema.",
        )
    host_allowed = any(host == item or host.endswith(f".{item}") for item in allowed_hosts)
    if parsed.scheme != "https" or not host_allowed:
        raise HTTPException(status_code=400, detail="URL nao pertence ao marketplace solicitado.")
    return url.strip()

@router.post(
    "/calculate-shipping",
    summary="Cálculo avançado de frete sob demanda",
    description="Calcula o frete unitário usando Playwright para Netshoes/Amazon ou API para ML."
)
async def calculate_shipping_single(request: CalculateShippingRequest):
    """
    Aciona o engine específico do marketplace para buscar o valor real de frete
    usando automação de navegador ou endpoint privado.
    """
    product_url = _validate_marketplace_url(request.marketplace, request.url)
    engine = engine_factory.get_engine(request.marketplace)
    if not engine:
        raise HTTPException(status_code=400, detail=f"Marketplace '{request.marketplace}' não suportado.")
        
    try:
        # A engine deve implementar calculate_shipping_advanced
        if hasattr(engine, "calculate_shipping_advanced"):
            shipping_info = await engine.calculate_shipping_advanced(product_url, request.zipcode)

            marketplace_read_state = (
                shipping_info.get("_marketplace_read_state")
                if isinstance(shipping_info, dict) else None
            )
            if marketplace_read_state in {"blocked", "unsupported"}:
                reason = (
                    "Consulta bloqueada pela proteção anti-bot do marketplace."
                    if marketplace_read_state == "blocked"
                    else "Cotação de frete não suportada para este marketplace."
                )
                return {
                    "status": marketplace_read_state,
                    "message": reason,
                    "shipping_info": None,
                    "last_success_at": shipping_info.get("last_success_at"),
                    "next_retry_at": shipping_info.get("next_retry_at"),
                }
            if marketplace_read_state == "stale":
                cached_info = {
                    k: v
                    for k, v in shipping_info.items()
                    if k not in {"_marketplace_read_state", "last_success_at", "next_retry_at"}
                }
                return {
                    "status": "stale",
                    "message": "Exibindo a última cotação conhecida; não foi possível reconsultar agora.",
                    "shipping_info": cached_info,
                    "last_success_at": shipping_info.get("last_success_at"),
                    "next_retry_at": shipping_info.get("next_retry_at"),
                }

            if shipping_info:
                if isinstance(shipping_info, dict) and shipping_info.get("error"):
                    return {"status": "error", "message": shipping_info["error"]}
                return {"status": "success", "shipping_info": shipping_info}
            else:
                return {"status": "error", "message": "Não foi possível extrair o frete desta página."}
        else:
            raise HTTPException(status_code=501, detail=f"O motor {request.marketplace} não suporta cálculo avançado de frete sob demanda.")
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/calculate-shipping-vtex",
    summary="Cálculo de frete VTEX sob demanda (busca comparativa)",
    description=(
        "Calcula o frete de um produto VTEX via simulação de checkout, sob demanda. "
        "O domínio é resolvido a partir da marca persistida (nunca de input do caller — T-33-01). "
        "Retorna o estado (available / unavailable_for_cep / temporary_failure) e a lista de opções."
    ),
)
async def calculate_shipping_vtex(request: CalculateVtexShippingRequest):
    """Aciona a simulação de checkout VTEX para um único SKU e retorna as modalidades de entrega."""
    from services.vtex_api_scraper import VtexApiClient

    clean_zipcode = request.zipcode.replace("-", "")

    try:
        result = await VtexApiClient.calculate_for_brand(
            brand_key=request.brand_key.lower(),
            sku_id=request.sku_id,
            seller_id=request.seller_id or "1",
            zipcode=clean_zipcode,
        )
    except ValueError as e:
        # Marca inexistente ou não-VTEX → erro de cliente.
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "state": result["state"],
        "shipping_options": [opt.model_dump(mode="json") for opt in result["shipping_options"]],
    }


@router.post(
    "/calculate-shipping-brand",
    response_model=CalculateBrandShippingResponse,
    summary="Calculo de frete nao-VTEX sob demanda",
    description=(
        "Calcula frete para marcas Wake/Shopify/Mercado Livre/Amazon/Netshoes/Linx/Zara "
        "usando o resolver nao-VTEX. VTEX permanece no endpoint /calculate-shipping-vtex."
    ),
)
async def calculate_shipping_brand(request: CalculateBrandShippingRequest):
    brand_key = request.brand_key.lower()
    brand = brand_service.get_brand(brand_key)
    if not brand:
        raise HTTPException(status_code=404, detail=f"Marca '{brand_key}' nao encontrada.")

    engine = getattr(brand, "engine", "vtex")
    if engine == "vtex":
        raise HTTPException(
            status_code=400,
            detail="Use /search/calculate-shipping-vtex para marcas VTEX.",
        )

    if not is_url_allowed_for_brand(request.product_url, brand):
        raise HTTPException(
            status_code=400,
            detail="URL do produto nao pertence ao dominio da marca.",
        )

    clean_zipcode = request.zipcode.replace("-", "")
    product = SearchProductResult(
        brand=brand_key,
        product_name="Produto",
        url=request.product_url,
        price_full=request.price,
        price_discount=request.price,
    )
    provider = resolve_shipping_provider(brand)
    calculation = await provider.calculate(product, clean_zipcode, brand)
    apply_shipping_calculation(product, calculation)

    return CalculateBrandShippingResponse(
        state=calculation.state,
        shipping_options=product.shipping_options,
        shipping=product.shipping,
        shipping_price=product.shipping_price,
        is_free_shipping=product.is_free_shipping,
        message=calculation.message,
    )


