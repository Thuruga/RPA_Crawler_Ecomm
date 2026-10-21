"""Shipping providers for non-VTEX engines."""

from services.shipping.base import (
    BaseShipping,
    ShippingCalculation,
    ShippingState,
    apply_shipping_calculation,
)
from services.shipping.resolver import resolve_shipping_provider

__all__ = [
    "BaseShipping",
    "ShippingCalculation",
    "ShippingState",
    "apply_shipping_calculation",
    "resolve_shipping_provider",
]
