"""Shipping enrichment for category-scan products."""
from __future__ import annotations

from typing import Any

from services.brand_service import brand_service
from services.shipping.base import ShippingState, sorted_shipping_options
from services.shipping.resolver import resolve_shipping_provider


async def enrich_category_shipping(product: Any, brand_key: str, zipcode: str | None) -> Any:
    """Attach the cheapest shipping option when a valid CEP was requested.

    Failures are intentionally non-fatal: product extraction is more important
    than an individual shipping quote.
    """
    if not zipcode:
        return product
    digits = "".join(ch for ch in zipcode if ch.isdigit())
    if len(digits) != 8:
        return product
    brand = brand_service.get_brand(brand_key.lower())
    if brand is None:
        return product
    try:
        result = await resolve_shipping_provider(brand).calculate(product, digits, brand)
        options = sorted_shipping_options(result.shipping_options)
        if result.state != ShippingState.AVAILABLE or not options:
            return product
        primary = options[0]
        data = dict(product) if isinstance(product, dict) else product.model_dump(mode="json")
        data.update({
            "shipping_price": primary.price,
            "is_free_shipping": primary.price == 0 or primary.is_free_shipping is True,
            "shipping_carrier": primary.service_name,
            "estimated_delivery_days": primary.estimated_delivery_days,
        })
        return data
    except Exception:
        return product
