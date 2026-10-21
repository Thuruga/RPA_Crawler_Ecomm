from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from core.models import (
    SearchProductResult,
    ShippingInfo,
    add_money_values,
    money_decimal_to_json_number,
    resolve_effective_price,
)


class ShippingState:
    AVAILABLE = "available"
    UNAVAILABLE_FOR_CEP = "unavailable_for_cep"
    TEMPORARY_FAILURE = "temporary_failure"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"
    STALE = "stale"


DEFAULT_MESSAGES = {
    ShippingState.UNAVAILABLE_FOR_CEP: "Entrega indisponivel para este CEP",
    ShippingState.TEMPORARY_FAILURE: "Frete temporariamente indisponivel",
    ShippingState.UNSUPPORTED: "Frete nao suportado para este engine",
    ShippingState.BLOCKED: "Bloqueado (anti-bot)",
    ShippingState.STALE: "Frete pode estar desatualizado — última cotação mantida",
}


@dataclass
class ShippingCalculation:
    state: str
    shipping_options: list[ShippingInfo] = field(default_factory=list)
    message: str | None = None
    raw: dict[str, Any] | None = None

    def model_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "shipping_options": [
                option.model_dump(mode="json") for option in self.shipping_options
            ],
            "message": self.message,
        }


class BaseShipping(ABC):
    @abstractmethod
    async def calculate(
        self,
        product: SearchProductResult | dict[str, Any],
        zipcode: str,
        brand: Any,
    ) -> ShippingCalculation:
        """Calculate shipping for a product and destination CEP."""


def normalize_zipcode(zipcode: str) -> str:
    digits = "".join(ch for ch in str(zipcode or "") if ch.isdigit())
    if len(digits) != 8:
        raise ValueError("CEP must contain 8 digits")
    return digits


def get_field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def brand_domain(brand: Any) -> str:
    return str(get_field(brand, "domain", "") or "").replace("https://", "").replace("http://", "").strip("/")


def is_url_allowed_for_brand(url: str, brand: Any) -> bool:
    expected = brand_domain(brand).lower()
    if not expected:
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if not host:
        return False
        
    if host == expected or host.endswith("." + expected):
        return True

    # Busca aliases dinâmicos configurados no cadastro da marca (PostgreSQL / DynamicBrand)
    aliases = get_field(brand, "domain_aliases", None)
    if not aliases:
        brand_key = str(get_field(brand, "brand_key", "") or "").lower()
        if brand_key:
            try:
                from services.brand_service import brand_service
                db_brand = brand_service.get_brand(brand_key)
                if db_brand:
                    aliases = getattr(db_brand, "domain_aliases", [])
            except Exception:
                aliases = []

    if aliases:
        if isinstance(aliases, str):
            aliases = [aliases]
        for a in aliases:
            a_clean = str(a or "").replace("https://", "").replace("http://", "").strip("/").lower()
            if a_clean and (host == a_clean or host.endswith("." + a_clean)):
                return True

    return False


def _option_sort_key(option: ShippingInfo) -> tuple[float, float, str]:
    price = option.price if option.price is not None else float("inf")
    days = (
        option.estimated_delivery_days
        if option.estimated_delivery_days is not None
        else float("inf")
    )
    return (float(price), float(days), option.service_name or "")


def sorted_shipping_options(options: list[ShippingInfo]) -> list[ShippingInfo]:
    return sorted(options, key=_option_sort_key)


def status_shipping(state: str, message: str | None = None) -> ShippingInfo:
    status = message or DEFAULT_MESSAGES.get(state) or "Frete indisponivel"
    return ShippingInfo(status=status, raw_text=status)


def apply_shipping_calculation(
    product: SearchProductResult,
    calculation: ShippingCalculation,
) -> SearchProductResult:
    options = sorted_shipping_options(calculation.shipping_options)
    product.shipping_options = options

    if calculation.state == ShippingState.AVAILABLE and options:
        primary = options[0]
        product.shipping = primary
        product.shipping_price = primary.price
        product.is_free_shipping = primary.price == 0.0 or primary.is_free_shipping is True
    else:
        product.shipping = status_shipping(calculation.state, calculation.message)
        product.shipping_price = None
        product.is_free_shipping = False

    base_price = resolve_effective_price(
        product.price_full,
        product.price_discount,
        product.price_discount_is_delta,
    )
    if base_price is not None:
        product.landed_price = money_decimal_to_json_number(
            add_money_values(base_price, product.shipping_price)
        )
    return product
