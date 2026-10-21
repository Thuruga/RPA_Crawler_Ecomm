"""Pure evaluator for Reserva's public Prime promotion rule."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from core.models import ConditionalOffer, OfferScope, OfferSource
from services.promotions.adapters.html import fetch_public_payload
from services.promotions.guards import build_probe_endpoint


def _values(values: Iterable[Any] | None) -> set[str]:
    return {str(value).casefold().strip() for value in (values or []) if str(value).strip()}


def _product_values(product: dict[str, Any], key: str) -> set[str]:
    value = product.get(key) or []
    if isinstance(value, str):
        value = [value]
    return _values(value)


def _matches(required: set[str], actual: set[str]) -> bool:
    if not required:
        return True
    return any(req == item or req in item for req in required for item in actual)


def is_eligible(
    rule: dict[str, Any],
    product: dict[str, Any],
    *,
    sku_id: str | None = None,
    seller_id: str | None = None,
) -> bool:
    if not rule.get("isActive", False):
        return False

    categories = _product_values(product, "categories")
    collections = _product_values(product, "collectionIds")
    brands = _values([product.get("brand")])
    seller = _values([seller_id])
    sku = _values([sku_id])

    def _check_condition(rule_key: str, inclusive_key: str, old_incl_key: str, old_excl_key: str, actual: set[str]) -> bool:
        if rule_key in rule:
            items = rule.get(rule_key) or []
            if isinstance(items, str):
                rule_values = _values(items.split(","))
            else:
                rule_values = _values([str(item.get("id", item)) if isinstance(item, dict) else str(item) for item in items])
            
            is_inclusive = rule.get(inclusive_key, True)
            if is_inclusive:
                if rule_values and not _matches(rule_values, actual):
                    return False
            else:
                if rule_values and _matches(rule_values, actual):
                    return False
            return True
        else:
            included = _values(rule.get(old_incl_key))
            if included and not _matches(included, actual):
                return False
            excluded = _values(rule.get(old_excl_key))
            if excluded and _matches(excluded, actual):
                return False
            return True

    if not _check_condition("categories", "categoriesAreInclusive", "includedCategories", "excludedCategories", categories):
        return False
    if not _check_condition("collections", "collectionsIsInclusive", "includedCollections", "excludedCollections", collections):
        return False
    if not _check_condition("brands", "brandsAreInclusive", "includedBrands", "excludedBrands", brands):
        return False
    if not _check_condition("idSeller", "idSellerIsInclusive", "includedSellers", "excludedSellers", seller):
        return False
    if not _check_condition("skus", "skusAreInclusive", "includedSkus", "excludedSkus", sku):
        return False

    return True


def _selected_offer(
    product: dict[str, Any], sku_id: str | None, seller_id: str | None
) -> tuple[str | None, str | None, dict[str, Any]]:
    for item in product.get("items") or []:
        current_sku = str(item.get("itemId") or "") or None
        if sku_id and current_sku != str(sku_id):
            continue
        for seller in item.get("sellers") or []:
            current_seller = str(seller.get("sellerId") or "") or None
            if seller_id and current_seller != str(seller_id):
                continue
            return current_sku, current_seller, seller.get("commertialOffer") or {}
    return sku_id, seller_id, {}


def parse_rule(
    rule: dict[str, Any],
    product: dict[str, Any],
    *,
    sku_id: str | None = None,
    seller_id: str | None = None,
) -> list[ConditionalOffer]:
    selected_sku, selected_seller, commercial = _selected_offer(product, sku_id, seller_id)
    if not is_eligible(
        rule,
        product,
        sku_id=selected_sku,
        seller_id=selected_seller,
    ):
        return []
    percent_raw = rule.get("percentualDiscountValue")
    public_raw = commercial.get("PriceWithoutDiscount")
    if percent_raw in (None, "") or public_raw in (None, ""):
        return []
    percent = Decimal(str(percent_raw))
    public = Decimal(str(public_raw))
    price = (public * (Decimal("1") - percent / Decimal("100"))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return [
        ConditionalOffer(
            condition_type="membership",
            condition_label="Cliente Prime",
            eligibility="requires_membership",
            price=price,
            discount_value=percent,
            discount_unit="percent",
            scope=OfferScope(
                product_id=str(product.get("productId") or "") or None,
                sku_id=selected_sku,
                seller_id=selected_seller,
            ),
            state="confirmed",
            source=OfferSource(
                kind="brand_custom_api",
                ref="/_v/rsv-prime/primepromotion",
                authority="authoritative",
            ),
            raw_text=f"Cliente Prime · {percent.normalize()}% OFF",
        )
    ]


parse_reserva_prime_rule = parse_rule


async def probe(
    *,
    brand_domain: str,
    path: str,
    product: dict[str, Any],
    sku_id: str | None = None,
    seller_id: str | None = None,
    session: Any | None = None,
):
    from core.models import ConditionalOfferResult

    target = build_probe_endpoint(brand_domain=brand_domain, path=path)
    status, payload, diagnostics = await fetch_public_payload(
        target, allowed_host=brand_domain, session=session
    )
    if status == 401:
        return ConditionalOfferResult(status="requires_auth", probe_level="full", diagnostics=["http_status=401"])
    if status == 403:
        return ConditionalOfferResult(status="blocked", probe_level="full", diagnostics=["http_status=403"])
    if status == 429 or status >= 500:
        return ConditionalOfferResult(status="error", probe_level="full", diagnostics=[f"http_status={status}"])
    if not isinstance(payload, dict):
        diag = "unexpected html response" if isinstance(payload, str) else f"unexpected payload type: {type(payload).__name__}"
        return ConditionalOfferResult(status="error", probe_level="full", diagnostics=[diag])
    offers = parse_rule(payload, product, sku_id=sku_id, seller_id=seller_id)
    if offers:
        return ConditionalOfferResult(
            status="found",
            offers=offers,
            probe_level="full",
            sources_attempted=[path],
            diagnostics=diagnostics,
        )
    _, _, commercial = _selected_offer(product, sku_id, seller_id)
    if not commercial:
        return ConditionalOfferResult(
            status="unsupported",
            probe_level="full",
            sources_attempted=[path],
            diagnostics=[
                *diagnostics,
                "insufficient_context: reserva_prime requires sku_id/seller_id/PriceWithoutDiscount "
                "from the PDP; none resolved for this product",
            ],
        )
    return ConditionalOfferResult(
        status="none_detected",
        offers=offers,
        probe_level="full",
        sources_attempted=[path],
        diagnostics=diagnostics,
    )
