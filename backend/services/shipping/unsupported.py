from __future__ import annotations

from typing import Any

from core.models import SearchProductResult
from services.shipping.base import BaseShipping, ShippingCalculation, ShippingState


class UnsupportedShipping(BaseShipping):
    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or "Frete nao suportado para este engine"

    async def calculate(
        self,
        product: SearchProductResult | dict[str, Any],
        zipcode: str,
        brand: Any,
    ) -> ShippingCalculation:
        return ShippingCalculation(
            state=ShippingState.UNSUPPORTED,
            shipping_options=[],
            message=self.reason,
        )
