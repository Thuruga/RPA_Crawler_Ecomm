"""Execute closed promotion descriptors against one already-fetched PDP."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from core.models import (
    ConditionalOffer,
    ConditionalOfferResult,
    OfferProbeLevel,
    PromotionAdapter,
    PromotionProbeConfig,
)
from services.promotions.adapters import html, reserva_prime, shopify, vtex
from services.promotions.orchestrator import aggregate_status, merge_offers
from services.promotions.registry import ResolvedProbe


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _brand_domain(brand: Any, url: str) -> str:
    raw = str(_get(brand, "domain", "") or "")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return parsed.hostname or urlparse(url).hostname or ""


def _existing_offers(product: Any) -> list[ConditionalOffer]:
    values = list(_get(product, "conditional_offers", []) or []) if product is not None else []
    return [
        value if isinstance(value, ConditionalOffer) else ConditionalOffer.model_validate(value)
        for value in values
    ]


def _scope_identity(
    product: Any,
    *,
    sku_id: str | None,
    seller_id: str | None,
) -> tuple[str | None, str | None]:
    # Prefer the product's own authoritative context (GAP-01, Fase 03 Plano 13) —
    # works for both RawProductBronze and dict payloads via `_get()`. Falls back to
    # previously-detected offers only for legacy payloads without these fields.
    sku_id = sku_id or _get(product, "sku_id")
    seller_id = seller_id or _get(product, "seller_id")
    for offer in _existing_offers(product):
        sku_id = sku_id or offer.scope.sku_id
        seller_id = seller_id or offer.scope.seller_id
        if sku_id and seller_id:
            break
    return sku_id, seller_id


def _product_payload(
    product: Any,
    *,
    sku_id: str | None,
    seller_id: str | None,
) -> dict[str, Any]:
    # Only a dict already in VTEX catalog shape (nested items/sellers/commertialOffer)
    # is safe to pass through untouched. `engine.get_pdp_product()` — the real
    # runtime path via routes_promotions.py, for every brand engine (VTEX, Shopify,
    # SFCC, Wake, Zara) — returns a flat `RawProductBronze.model_dump()` dict instead
    # (sku_id/seller_id/price_without_discount as top-level keys, no "items"). Treating
    # that flat dict as already-shaped silently dropped every field below, so adapters
    # always saw items=[] and reported false insufficient_context/none_detected.
    if isinstance(product, dict) and isinstance(product.get("items"), list):
        return product
    if product is None:
        return {}
    # Prefer the authoritative pre-discount price captured on the PDP (GAP-01);
    # fall back to the price_full+price_discount reconstruction for legacy
    # payloads that never carried price_without_discount.
    original_price = _get(product, "price_without_discount")
    if original_price is None:
        original_price = _get(product, "price_full")
        price_delta = _get(product, "price_discount")
        if _get(product, "price_discount_is_delta", False) and original_price is not None:
            original_price = float(original_price) + float(price_delta or 0)
    breadcrumb = list(_get(product, "breadcrumb", []) or [])
    category = _get(product, "category")
    categories = [*breadcrumb, *([category] if category and category not in breadcrumb else [])]
    specifications = dict(_get(product, "specifications", {}) or {})
    collections = (
        list(_get(product, "collection_ids", []) or [])
        or specifications.get("collectionIds")
        or specifications.get("collections")
        or []
    )
    if isinstance(collections, str):
        collections = [collections]
    return {
        "productId": _get(product, "product_code"),
        "brand": _get(product, "brand"),
        "categories": categories,
        "collectionIds": collections,
        "items": [
            {
                "itemId": sku_id,
                "sellers": [
                    {
                        "sellerId": seller_id,
                        "commertialOffer": {"PriceWithoutDiscount": original_price},
                    }
                ],
            }
        ] if sku_id or seller_id else [],
    }


def _structured_result(
    probe: ResolvedProbe,
    product: Any,
    payload: dict[str, Any],
) -> ConditionalOfferResult:
    existing = _existing_offers(product)
    if existing:
        offers = [offer for offer in existing if offer.source.kind.value.startswith(probe.adapter.value)]
        if offers:
            return ConditionalOfferResult(status="found", offers=offers, probe_level=probe.config.level)
    if probe.adapter == PromotionAdapter.VTEX:
        commercial = payload
        if payload.get("items"):
            item = payload["items"][0]
            seller = (item.get("sellers") or [{}])[0]
            commercial = {
                "productId": payload.get("productId"),
                "itemId": item.get("itemId"),
                "sellerId": seller.get("sellerId"),
                **(seller.get("commertialOffer") or {}),
            }
        offers = vtex.parse_teasers(commercial)
    elif probe.adapter == PromotionAdapter.SHOPIFY:
        offers = shopify.parse_product(payload)
    else:
        offers = []
    return ConditionalOfferResult(
        status="found" if offers else "none_detected",
        offers=offers,
        probe_level=probe.config.level,
    )


async def _execute_probe(
    probe: ResolvedProbe,
    *,
    brand: Any,
    url: str,
    level: OfferProbeLevel,
    product: Any,
    sku_id: str | None,
    seller_id: str | None,
    session: Any | None,
) -> ConditionalOfferResult:
    payload = _product_payload(product, sku_id=sku_id, seller_id=seller_id)
    domain = _brand_domain(brand, url)
    if probe.adapter in {PromotionAdapter.VTEX, PromotionAdapter.SHOPIFY}:
        return _structured_result(probe, product, payload)
    if probe.adapter == PromotionAdapter.RESERVA_PRIME:
        return await reserva_prime.probe(
            brand_domain=domain,
            path=probe.config.path or "",
            product=payload,
            sku_id=sku_id,
            seller_id=seller_id,
            session=session,
        )
    if probe.adapter == PromotionAdapter.HTML:
        return await html.probe(
            url,
            allowed_host=domain,
            level=level.value,
            session=session,
            product_id=str(payload.get("productId") or "") or None,
        )
    raise ValueError(f"unsupported resolved promotion adapter: {probe.adapter.value}")


async def execute_resolved_probes(
    probes: Iterable[ResolvedProbe],
    configs: Iterable[PromotionProbeConfig] = (),
    *,
    brand: Any,
    url: str,
    level: str | OfferProbeLevel,
    product: Any,
    sku_id: str | None = None,
    seller_id: str | None = None,
    session: Any | None = None,
) -> ConditionalOfferResult:
    """Execute applicable descriptors and retain per-source attempt/skip evidence."""
    parsed_level = level if isinstance(level, OfferProbeLevel) else OfferProbeLevel(level)
    descriptors = tuple(probes)
    configured = tuple(configs)
    descriptor_configs = tuple(probe.config for probe in descriptors)
    if configured and configured != descriptor_configs:
        raise ValueError("resolved promotion probes/configs are out of sync")
    sku_id, seller_id = _scope_identity(product, sku_id=sku_id, seller_id=seller_id)
    parts: list[ConditionalOfferResult] = []
    diagnostics: list[str] = []
    attempted: list[str] = []
    for probe in descriptors:
        if parsed_level == OfferProbeLevel.FAST and probe.config.level != OfferProbeLevel.FAST:
            diagnostics.append(f"probe skipped [{probe.source_name}]: full-only source")
            continue
        attempted.append(probe.source_name)
        try:
            result = await _execute_probe(
                probe,
                brand=brand,
                url=url,
                level=parsed_level,
                product=product,
                sku_id=sku_id,
                seller_id=seller_id,
                session=session,
            )
        except Exception as exc:
            result = ConditionalOfferResult(
                status="error",
                probe_level=parsed_level,
                diagnostics=[f"probe failed: {type(exc).__name__}"],
            )
        result.probe_level = parsed_level
        result.sources_attempted = [probe.source_name]
        result.diagnostics = [f"{probe.source_name}: {item}" for item in result.diagnostics]
        parts.append(result)
    offers, merge_diagnostics = merge_offers(
        offer for part in parts for offer in part.offers
    )
    if offers:
        status = "found"
    elif parts:
        status = aggregate_status(part.status.value for part in parts)
    else:
        status = "unsupported"
        diagnostics.append("no applicable promotion source executed")
    return ConditionalOfferResult(
        status=status,
        offers=offers,
        probe_level=parsed_level,
        sources_attempted=list(dict.fromkeys(attempted)),
        diagnostics=[
            *diagnostics,
            *(item for part in parts for item in part.diagnostics),
            *merge_diagnostics,
        ],
    )
