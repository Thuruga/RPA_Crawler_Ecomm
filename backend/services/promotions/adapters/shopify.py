"""Pure Shopify product and cart promotion parsers."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from core.models import ConditionalOffer, OfferScope, OfferSource
from services.promotions.adapters.html import fetch_public_payload
from services.promotions.guards import validate_promotion_target


def _money(cents: Any) -> Decimal | None:
    if cents in (None, ""):
        return None
    return (Decimal(str(cents)) / Decimal("100")).quantize(Decimal("0.01"))


def parse_product(payload: dict[str, Any]) -> list[ConditionalOffer]:
    variants = payload.get("variants") or []
    variant = variants[0] if variants else {}
    offers = []
    for raw_tag in payload.get("tags") or []:
        text = str(raw_tag).strip()
        lowered = text.casefold()
        if not any(marker in lowered for marker in ("pix", "cupom", "membro", "login")):
            continue
        percent_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
        requires_auth = "login" in lowered or "membro" in lowered
        offers.append(
            ConditionalOffer(
                condition_type="authentication" if requires_auth else ("coupon" if "cupom" in lowered else "payment_method"),
                condition_label=text,
                eligibility="requires_auth" if requires_auth else ("requires_coupon" if "cupom" in lowered else "requires_payment_method"),
                discount_value=Decimal(percent_match.group(1).replace(",", ".")) if percent_match else None,
                discount_unit="percent" if percent_match else None,
                payment_method="pix" if "pix" in lowered else None,
                scope=OfferScope(
                    product_id=str(payload.get("id") or "") or None,
                    sku_id=str(variant.get("id") or "") or None,
                ),
                state="requires_auth" if requires_auth else "advertised",
                source=OfferSource(kind="shopify_product", ref="product.tags", authority="structured"),
                raw_text=text,
            )
        )
    return offers


def parse_cart(payload: dict[str, Any]) -> list[ConditionalOffer]:
    offers = []
    for item in payload.get("items") or []:
        original = _money(item.get("original_line_price"))
        final = _money(item.get("final_line_price"))
        discounts = item.get("discounts") or []
        if final is None or original is None or final >= original or not discounts:
            continue
        title = str(discounts[0].get("title") or "Desconto aplicado")
        lowered = title.casefold()
        offers.append(
            ConditionalOffer(
                condition_type="payment_method" if "pix" in lowered else "checkout",
                condition_label=title,
                eligibility="requires_payment_method" if "pix" in lowered else "requires_checkout",
                price=final,
                discount_value=(original - final).quantize(Decimal("0.01")),
                discount_unit="amount",
                payment_method="pix" if "pix" in lowered else None,
                scope=OfferScope(sku_id=str(item.get("id") or "") or None),
                state="confirmed",
                source=OfferSource(kind="shopify_cart", ref="/cart.js", authority="authoritative"),
                raw_text=title,
            )
        )
    return offers


async def confirm_advertised(
    advertised: ConditionalOffer,
    *,
    brand_domain: str,
    variant_id: str,
    quantity: int = 1,
    transport: Any | None = None,
):
    """Confirm one advertised Shopify offer in a disposable anonymous cart."""
    if transport is None:
        from services.promotions.confirmation import AnonymousConfirmationTransport

        transport = AnonymousConfirmationTransport()
    return await transport.confirm_shopify(
        brand_domain=brand_domain,
        advertised=advertised,
        variant_id=variant_id,
        quantity=quantity,
    )


async def probe_product(
    target: str,
    *,
    allowed_host: str,
    session: Any | None = None,
):
    from core.models import ConditionalOfferResult

    validate_promotion_target(target, allowed_host=allowed_host)
    status, payload, diagnostics = await fetch_public_payload(
        target, allowed_host=allowed_host, session=session
    )
    if status == 401:
        return ConditionalOfferResult(status="requires_auth", probe_level="full", diagnostics=["http_status=401"])
    if status == 403:
        return ConditionalOfferResult(status="blocked", probe_level="full", diagnostics=["http_status=403"])
    if status == 429 or status >= 500:
        return ConditionalOfferResult(status="error", probe_level="full", diagnostics=[f"http_status={status}"])
    if not isinstance(payload, dict):
        return ConditionalOfferResult(status="error", probe_level="full", diagnostics=["unexpected promotion schema"])
    offers = parse_product(payload)
    return ConditionalOfferResult(
        status="found" if offers else "none_detected",
        offers=offers,
        probe_level="full",
        diagnostics=diagnostics,
    )
