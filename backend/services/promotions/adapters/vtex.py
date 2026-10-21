"""Pure VTEX teaser and anonymous-simulation parsers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from core.models import ConditionalOffer, OfferScope, OfferSource
from services.promotions.adapters.html import fetch_public_payload
from services.promotions.guards import validate_promotion_target


def _decimal(value: Any, *, cents: bool = False) -> Decimal | None:
    if value in (None, ""):
        return None
    result = Decimal(str(value))
    if cents:
        result /= Decimal("100")
    return result.quantize(Decimal("0.01"))


def _condition(name: str) -> tuple[str, str, str | None]:
    lowered = name.casefold()
    if "pix" in lowered or "cartão" in lowered or "cartao" in lowered:
        return "payment_method", "requires_payment_method", "pix" if "pix" in lowered else None
    if "cupom" in lowered:
        return "coupon", "requires_coupon", None
    if "prime" in lowered or "membro" in lowered or "assinatura" in lowered or "clube" in lowered:
        return "membership", "requires_membership", None
    if "login" in lowered:
        return "authentication", "requires_auth", None
    return "other", "other", None


def _checkout_condition_name(payload: dict[str, Any]) -> str | None:
    """Pick only conditions an anonymous checkout simulation can confirm."""
    identifiers = (payload.get("ratesAndBenefitsData") or {}).get("rateAndBenefitsIdentifiers") or []
    if not identifiers:
        return "Benefício aplicado"
    for identifier in identifiers:
        name = str(identifier.get("name") or "").strip()
        if not name:
            continue
        condition, eligibility, _ = _condition(name)
        if condition in {"membership", "authentication"} or eligibility in {
            "requires_membership",
            "requires_auth",
        }:
            continue
        return name
    return None


def parse_teasers(payload: dict[str, Any]) -> list[ConditionalOffer]:
    scope = OfferScope(
        product_id=str(payload.get("productId") or "") or None,
        sku_id=str(payload.get("itemId") or "") or None,
        seller_id=str(payload.get("sellerId") or "") or None,
    )
    offers = []
    evidence = [
        *(payload.get("Teasers") or payload.get("teasers") or []),
        *(payload.get("Benefits") or payload.get("benefits") or []),
    ]
    for teaser in evidence:
        name = str(teaser.get("name") or teaser.get("Name") or "Oferta VTEX").strip()
        condition, eligibility, payment = _condition(name)
        discount = None
        effects = teaser.get("effects") or teaser.get("Effects") or {}
        for parameter in effects.get("parameters") or effects.get("Parameters") or []:
            if str(parameter.get("name") or "").casefold() == "percentualdiscount":
                discount = _decimal(parameter.get("value"))
        offers.append(
            ConditionalOffer(
                condition_type=condition,
                condition_label=name,
                eligibility=eligibility,
                discount_value=discount,
                discount_unit="percent" if discount is not None else None,
                payment_method=payment,
                scope=scope,
                state="requires_auth" if eligibility == "requires_auth" else "advertised",
                source=OfferSource(
                    kind="vtex_catalog",
                    ref="commercialOffer.Teasers",
                    authority="structured",
                ),
                raw_text=name,
            )
        )
    return offers


def parse_checkout(payload: dict[str, Any]) -> list[ConditionalOffer]:
    name = _checkout_condition_name(payload)
    if not name:
        return []
    condition, eligibility, payment = _condition(name)
    offers = []
    for item in payload.get("items") or []:
        original = _decimal(item.get("price"), cents=True)
        price = _decimal(item.get("sellingPrice"), cents=True)
        if price is None or original is None or price >= original:
            continue
        offers.append(
            ConditionalOffer(
                condition_type=condition,
                condition_label=name,
                eligibility=eligibility,
                price=price,
                discount_value=(original - price).quantize(Decimal("0.01")),
                discount_unit="amount",
                payment_method=payment,
                scope=OfferScope(
                    sku_id=str(item.get("id") or "") or None,
                    seller_id=str(item.get("seller") or "") or None,
                ),
                state="confirmed",
                source=OfferSource(
                    kind="vtex_checkout",
                    ref="/api/checkout/pub/orderForms/simulation",
                    authority="authoritative",
                ),
                raw_text=name,
            )
        )
    return offers


parse_benefits = parse_teasers


async def confirm_advertised(
    advertised: ConditionalOffer,
    *,
    brand_domain: str,
    sku_id: str,
    seller_id: str = "1",
    quantity: int = 1,
    transport: Any | None = None,
):
    """Confirm one advertised VTEX offer through the isolated simulation transport."""
    if transport is None:
        from services.promotions.confirmation import AnonymousConfirmationTransport

        transport = AnonymousConfirmationTransport()
    return await transport.confirm_vtex(
        brand_domain=brand_domain,
        advertised=advertised,
        sku_id=sku_id,
        seller_id=seller_id,
        quantity=quantity,
    )


async def probe_catalog(
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
    offers = parse_teasers(payload)
    return ConditionalOfferResult(
        status="found" if offers else "none_detected",
        offers=offers,
        probe_level="full",
        diagnostics=diagnostics,
    )
