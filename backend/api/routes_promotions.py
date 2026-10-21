"""Protected progressive conditional-offer enrichment endpoints."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from core.constants import CONDITIONAL_OFFER_EXCLUDED_MARKETPLACES
from core.models import ConditionalOffer, ConditionalOfferResult
from services.brand_service import brand_service
from services.engines.factory import engine_factory
from services.promotions.adapters import shopify, vtex
from services.promotions.confirmation import AnonymousConfirmationTransport
from services.promotions.cache import normalize_product_url
from services.promotions.guards import validate_promotion_target
from services.promotions.orchestrator import PromotionOrchestrator
from services.promotions.registry import resolve_brand, resolve_for_engine
from services.promotions.runtime import execute_resolved_probes
from services.shipping.base import is_url_allowed_for_brand


router = APIRouter(prefix="/search/product", tags=["conditional-offers"])
_MARKETPLACES = CONDITIONAL_OFFER_EXCLUDED_MARKETPLACES
_BROWSER_TRIGGERS = {"product_opened", "manual_revalidate"}
_SECRET_DIAGNOSTIC = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|password|secret|session|token)=([^\s&]+)"
)
_ORCHESTRATOR_LIMIT = 256
_orchestrators: OrderedDict[tuple[str, str, str], PromotionOrchestrator] = OrderedDict()


def _clear_orchestrators() -> None:
    _orchestrators.clear()


def _orchestrator_registry_size() -> int:
    return len(_orchestrators)


def _brand_fingerprint(brand: Any) -> str:
    """Stable hash of the resolved-probe-relevant brand config.

    Included in the orchestrator cache key so that editing a brand's
    ``promotion_probes``/``domain``/``engine`` invalidates cached
    orchestrators for that brand instead of silently keeping stale
    descriptors alive until LRU eviction or process restart.
    """
    engine = str(getattr(brand, "engine", "") or "").casefold()
    domain = str(getattr(brand, "domain", "") or "").casefold()
    probes = getattr(brand, "promotion_probes", None) or []
    serialized_probes = [
        probe.model_dump(mode="json") if hasattr(probe, "model_dump") else probe
        for probe in probes
    ]
    payload = json.dumps(
        {"engine": engine, "domain": domain, "promotion_probes": serialized_probes},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _orchestrator_key(brand_key: str, url: str, fingerprint: str = "") -> tuple[str, str, str]:
    return brand_key.casefold().strip(), normalize_product_url(url), fingerprint


def _get_or_create_orchestrator(
    key: tuple[str, str, str],
    factory: Any,
) -> PromotionOrchestrator:
    existing = _orchestrators.pop(key, None)
    if existing is not None:
        _orchestrators[key] = existing
        return existing
    created = factory()
    _orchestrators[key] = created
    while len(_orchestrators) > _ORCHESTRATOR_LIMIT:
        _orchestrators.popitem(last=False)
    return created


class ConditionalOfferRequest(BaseModel):
    brand: str = Field(..., min_length=1, max_length=80)
    url: str = Field(..., min_length=1, max_length=2048)
    trigger: Literal["product_opened", "manual_revalidate"] = "product_opened"
    force: bool = False

    @model_validator(mode="after")
    def validate_force_trigger(self):
        if self.force and self.trigger != "manual_revalidate":
            raise ValueError("force is allowed only for manual_revalidate")
        return self


def _sanitize_result(result: ConditionalOfferResult) -> ConditionalOfferResult:
    diagnostics = []
    for diagnostic in result.diagnostics:
        cleaned = _SECRET_DIAGNOSTIC.sub(r"\1=[redacted]", str(diagnostic))
        diagnostics.append(cleaned[:300])
    return result.model_copy(update={"diagnostics": diagnostics})


def _brand_host(brand: Any) -> list[str]:
    hosts: list[str] = []
    value = str(getattr(brand, "domain", "") or "")
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if parsed.hostname:
        hosts.append(parsed.hostname)
    aliases = getattr(brand, "domain_aliases", []) or []
    if isinstance(aliases, str):
        aliases = [aliases]
    for a in aliases:
        a_clean = str(a or "").replace("https://", "").replace("http://", "").strip("/")
        if a_clean and a_clean not in hosts:
            hosts.append(a_clean)
    return hosts


async def _resolved_product_evidence(
    *,
    brand: Any,
    brand_key: str,
    url: str,
    level: str,
    resolved: Any,
) -> ConditionalOfferResult:
    """Fetch the PDP once, then execute every applicable resolved source."""
    try:
        engine = engine_factory.get_engine(brand_key)
        product = await engine.get_pdp_product(url)
    except Exception as exc:
        return ConditionalOfferResult(
            status="error",
            probe_level="full",
            diagnostics=[f"offer evidence failed: {type(exc).__name__}"],
        )
    if product:
        product_offers = (
            product.conditional_offers
            if hasattr(product, "conditional_offers")
            else (product.get("conditional_offers", []) if isinstance(product, dict) else [])
        )
        if product_offers:
            parsed_offers = [
                o if isinstance(o, ConditionalOffer) else ConditionalOffer.model_validate(o)
                for o in product_offers
            ]
            return ConditionalOfferResult(
                status="found",
                offers=parsed_offers,
                probe_level="full",
                sources_attempted=["engine_pdp"],
            )
    return await execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand=brand,
        url=url,
        level=level,
        product=product,
    )


def _confirm_callback(brand: Any, transport: AnonymousConfirmationTransport):
    engine = str(getattr(brand, "engine", "") or "").casefold()
    domain = _brand_host(brand)

    async def confirm(offer: ConditionalOffer) -> ConditionalOfferResult:
        sku_id = offer.scope.sku_id
        if not sku_id:
            return ConditionalOfferResult(
                status="none_detected",
                probe_level="confirm",
                diagnostics=["confirmation skipped: advertised offer has no SKU/variant scope"],
            )
        if engine == "vtex":
            return await vtex.confirm_advertised(
                offer,
                brand_domain=domain,
                sku_id=sku_id,
                seller_id=offer.scope.seller_id or "1",
                quantity=offer.quantity_required or 1,
                transport=transport,
            )
        if engine == "shopify":
            return await shopify.confirm_advertised(
                offer,
                brand_domain=domain,
                variant_id=sku_id,
                quantity=offer.quantity_required or 1,
                transport=transport,
            )
        return ConditionalOfferResult(
            status="unsupported",
            probe_level="confirm",
            diagnostics=[f"anonymous confirmation unsupported for engine {engine or 'missing'}"],
        )

    return confirm


async def promotion_orchestrator(
    *,
    brand: Any,
    brand_key: str,
    url: str,
    trigger: str,
    force: bool = False,
    level: str = "full",
    product: Any = None,
) -> ConditionalOfferResult:
    """Resolve capability and run a cached, bounded independent offer collection."""
    if level == "fast":
        offers = list(getattr(product, "conditional_offers", []) or []) if product else []
        return ConditionalOfferResult(
            status="found" if offers else "none_detected",
            offers=offers,
            probe_level="fast",
        )

    resolved = resolve_brand(brand)
    if resolved.status != "ready":
        return ConditionalOfferResult(
            status=resolved.status,
            probe_level="full",
            diagnostics=list(resolved.diagnostics),
        )
    key = _orchestrator_key(brand_key, url, _brand_fingerprint(brand))

    def create_orchestrator() -> PromotionOrchestrator:
        transport = AnonymousConfirmationTransport()

        async def probe(_level: str):
            return await _resolved_product_evidence(
                brand=brand,
                brand_key=brand_key,
                url=url,
                level=_level,
                resolved=resolved,
            )

        return PromotionOrchestrator(
            probe=probe,
            confirm=_confirm_callback(brand, transport),
        )

    orchestrator = _get_or_create_orchestrator(key, create_orchestrator)

    return await orchestrator.collect(
        brand=brand_key,
        url=url,
        trigger=trigger,
        advertised=[],
        force=force,
    )


def _validated_brand(brand_key: str, url: str) -> Any:
    brand = brand_service.get_brand(brand_key)
    if brand is None:
        raise HTTPException(status_code=400, detail=f"Marca '{brand_key}' não encontrada.")
    if not bool(getattr(brand, "is_active", False)):
        raise HTTPException(status_code=400, detail="Marca inativa.")
    if not is_url_allowed_for_brand(url, brand):
        raise HTTPException(status_code=400, detail="URL do produto não pertence ao domínio da marca.")
    try:
        validate_promotion_target(url, allowed_host=_brand_host(brand))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="URL pública de produto inválida.") from exc
    return brand


@router.post(
    "/conditional-offers",
    response_model=ConditionalOfferResult,
    summary="Enriquecimento progressivo e revalidação de ofertas condicionais",
)
async def conditional_offers(request: ConditionalOfferRequest) -> ConditionalOfferResult:
    brand_key = request.brand.casefold().strip()
    if brand_key in _MARKETPLACES:
        resolved = resolve_for_engine(brand_key, brand_key=brand_key)
        return ConditionalOfferResult(
            status=resolved.status,
            probe_level="full",
            diagnostics=list(resolved.diagnostics),
        )
    brand = _validated_brand(brand_key, request.url)
    try:
        result = await promotion_orchestrator(
            brand=brand,
            brand_key=brand_key,
            url=request.url,
            trigger=request.trigger,
            force=request.force,
            level="full",
        )
    except (TimeoutError, OSError) as exc:
        result = ConditionalOfferResult(
            status="error",
            probe_level="full",
            diagnostics=[f"offer enrichment failed: {type(exc).__name__}"],
        )
    except ValueError as exc:
        # Post-validation capability failures are typed integration outcomes.
        result = ConditionalOfferResult(
            status="error",
            probe_level="full",
            diagnostics=[f"offer enrichment rejected: {type(exc).__name__}"],
        )
    except Exception as exc:
        result = ConditionalOfferResult(
            status="error",
            probe_level="full",
            diagnostics=[f"offer enrichment failed: {type(exc).__name__}"],
        )
    return _sanitize_result(result)


async def fast_offers_for_search(
    products: Iterable[Any],
    *,
    trigger: Literal["search", "category"] = "search",
) -> list[ConditionalOfferResult]:
    """Forward already-cheap structured evidence; never opens PDP/browser/cart."""
    results = []
    for product in products:
        getter = product.get if isinstance(product, dict) else lambda name, default=None: getattr(product, name, default)
        brand_key = str(getter("brand", "") or getter("brand_key", "")).casefold()
        brand = brand_service.get_brand(brand_key)
        if brand is None:
            results.append(
                ConditionalOfferResult(
                    status="unsupported",
                    probe_level="fast",
                    diagnostics=[f"brand not found: {brand_key or '(empty)'}"],
                )
            )
            continue
        results.append(
            await promotion_orchestrator(
                brand=brand,
                brand_key=brand_key,
                url=str(getter("url", "")),
                trigger=trigger,
                level="fast",
                product=product,
            )
        )
    return results
