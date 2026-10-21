"""Transport-free conditional-promotion domain helpers."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from core.models import (
    ConditionalOffer,
    ConditionalOfferEvent,
    ConditionalOfferEventType,
    ConditionalOfferResult,
    OfferCacheMetadata,
    OfferConditionType,
    OfferEligibility,
    OfferProbeLevel,
    OfferProbeStatus,
    OfferScope,
    OfferSource,
    OfferSourceAuthority,
    OfferSourceKind,
    OfferState,
    _normalized_offer_identity,
)


def build_offer_key(
    *,
    condition_type: str,
    condition_label: str,
    eligibility: str,
    scope: OfferScope | dict[str, Any] | None,
    source: OfferSource | dict[str, Any],
    quantity_required: int | None = None,
    coupon_code: str | None = None,
    payment_method: str | None = None,
) -> str:
    scope_value = scope if isinstance(scope, OfferScope) else OfferScope(**(scope or {}))
    source_value = source if isinstance(source, OfferSource) else OfferSource(**source)
    return _normalized_offer_identity(
        {
            "condition_type": condition_type,
            "condition_label": " ".join(condition_label.casefold().split()),
            "eligibility": eligibility,
            "quantity_required": quantity_required,
            "coupon_code": (coupon_code or "").casefold(),
            "payment_method": (payment_method or "").casefold(),
            "scope": scope_value.model_dump(mode="json"),
            "source": {
                "kind": source_value.kind.value,
                "ref": source_value.ref,
                "authority": source_value.authority.value,
            },
        }
    )


def normalize_quantity_offer(
    *, quantity: int | None, total: Decimal | int | float | str | None
) -> Decimal | None:
    if quantity is None or quantity <= 0 or total is None:
        return None
    value = Decimal(str(total)) / Decimal(quantity)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


__all__ = [name for name in globals() if not name.startswith("_")]
