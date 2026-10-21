"""Provedor determinístico de frete para Lacoste Brasil (SFCC/Algolia).

A Lacoste Brasil adota política logística nacional com regras claras:
  - Clique e Retire (Retirada em Loja Física): R$ 0,00 (Grátis, 2 a 5 dias úteis).
  - Entrega Padrão Domiciliar: R$ 19,90 (4 a 8 dias úteis) — Grátis a partir de R$ 499,00.
  - Entrega Expressa: R$ 29,90 (1 a 2 dias úteis) para capitais e regiões metropolitanas.
"""

from __future__ import annotations

import logging
from typing import Any

from core.models import SearchProductResult, ShippingInfo
from services.shipping.base import (
    BaseShipping,
    ShippingCalculation,
    ShippingState,
    get_field,
    is_url_allowed_for_brand,
    normalize_zipcode,
    sorted_shipping_options,
)

logger = logging.getLogger(__name__)

LACOSTE_FREE_SHIPPING_THRESHOLD: float = 499.00
LACOSTE_STANDARD_SHIPPING_PRICE: float = 19.90
LACOSTE_EXPRESS_SHIPPING_PRICE: float = 29.90

METRO_ZIPCODE_PREFIXES = (
    "01", "02", "03", "04", "05", "06", "07", "08", "09",  # Grande SP
    "20", "21", "22", "23", "24", "25",                     # Grande RJ
    "30", "31",                                             # Grande BH
    "70", "71",                                             # Distrito Federal
    "80", "81",                                             # Grande Curitiba
    "90", "91",                                             # Grande Porto Alegre
)


class LacosteShipping(BaseShipping):
    """Provedor de frete para Lacoste Brasil."""

    async def calculate(
        self,
        product: SearchProductResult | dict[str, Any],
        zipcode: str,
        brand: Any,
    ) -> ShippingCalculation:
        try:
            cep = normalize_zipcode(zipcode)
        except ValueError:
            return ShippingCalculation(
                state=ShippingState.UNAVAILABLE_FOR_CEP,
                message="CEP invalido",
            )

        product_url = str(get_field(product, "url", "") or "")
        if not is_url_allowed_for_brand(product_url, brand):
            return ShippingCalculation(
                state=ShippingState.UNSUPPORTED,
                message="URL do produto nao pertence ao dominio da marca",
            )

        price_discount = get_field(product, "price_discount")
        price_full = get_field(product, "price_full")
        effective_price = float(price_discount if price_discount is not None else (price_full or 0.0))

        is_free_standard = effective_price >= LACOSTE_FREE_SHIPPING_THRESHOLD
        standard_price = 0.0 if is_free_standard else LACOSTE_STANDARD_SHIPPING_PRICE

        options: list[ShippingInfo] = [
            ShippingInfo(
                price=standard_price,
                status="Grátis" if is_free_standard else "Disponível",
                estimated_delivery_days=6,
                raw_text="Entrega Padrão (4-8 dias úteis)" + (" - Frete Grátis" if is_free_standard else f" - R$ {standard_price:.2f}"),
                service_name="Entrega Padrão",
                service_id="lacoste_padrao",
                estimate_display="4 a 8 dias úteis",
                estimate_unit="bd",
                is_free_shipping=is_free_standard,
            ),
            ShippingInfo(
                price=0.0,
                status="Grátis",
                estimated_delivery_days=3,
                raw_text="Clique e Retire (Loja Física) - Grátis (2-5 dias úteis)",
                service_name="Clique e Retire",
                service_id="lacoste_clique_retire",
                estimate_display="2 a 5 dias úteis",
                estimate_unit="bd",
                is_free_shipping=True,
            ),
        ]

        if any(cep.startswith(pref) for pref in METRO_ZIPCODE_PREFIXES):
            options.append(
                ShippingInfo(
                    price=LACOSTE_EXPRESS_SHIPPING_PRICE,
                    status="Disponível",
                    estimated_delivery_days=1,
                    raw_text=f"Entrega Expressa (1-2 dias úteis) - R$ {LACOSTE_EXPRESS_SHIPPING_PRICE:.2f}",
                    service_name="Entrega Expressa",
                    service_id="lacoste_expressa",
                    estimate_display="1 a 2 dias úteis",
                    estimate_unit="bd",
                    is_free_shipping=False,
                )
            )

        return ShippingCalculation(
            state=ShippingState.AVAILABLE,
            shipping_options=sorted_shipping_options(options),
        )
