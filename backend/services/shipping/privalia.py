"""Provedor de frete para Privalia (Veepee Group).

A Privalia opera no modelo de clube de compras / flash sales multimarcas com centro de distribuição:
  - Entrega Padrão:
      - SP (CEPs 01 a 19): R$ 19,90 (5 a 8 dias úteis).
      - Sul e Sudeste (CEPs 20 a 39 e 80 a 99): R$ 24,90 (7 a 12 dias úteis).
      - Demais Regiões (CEPs 40 a 79): R$ 29,90 (10 a 16 dias úteis).
  - Privalia Premium:
      - Frete Grátis para Sul e Sudeste em pedidos a partir de R$ 299,00.
  - Entrega Expressa Metropolitana: R$ 34,90 (3 a 5 dias úteis para principais capitais).
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

PRIVALIA_FREE_SHIPPING_THRESHOLD_PREMIUM: float = 299.00

PRIVALIA_STANDARD_PRICE_SP: float = 19.90
PRIVALIA_STANDARD_PRICE_SOUTH_SOUTHEAST: float = 24.90
PRIVALIA_STANDARD_PRICE_OTHER: float = 29.90
PRIVALIA_EXPRESS_PRICE: float = 34.90

METRO_CAPITAL_PREFIXES = (
    "01", "02", "03", "04", "05", "06", "07", "08", "09",  # Grande SP
    "20", "21", "22", "23", "24", "25",                     # Grande RJ
    "30", "31",                                             # Grande BH
    "70", "71",                                             # DF
    "80", "81",                                             # Curitiba
    "90", "91",                                             # Porto Alegre
)


class PrivaliaShipping(BaseShipping):
    """Provedor de frete para Privalia."""

    def _is_south_southeast(self, cep: str) -> bool:
        prefix_2 = int(cep[:2])
        return (1 <= prefix_2 <= 39) or (80 <= prefix_2 <= 99)

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

        is_south_se = self._is_south_southeast(cep)
        is_free_premium = is_south_se and effective_price >= PRIVALIA_FREE_SHIPPING_THRESHOLD_PREMIUM

        if is_free_premium:
            standard_price = 0.0
        elif cep.startswith(("01", "02", "03", "04", "05", "06", "07", "08", "09")):
            standard_price = PRIVALIA_STANDARD_PRICE_SP
        elif is_south_se:
            standard_price = PRIVALIA_STANDARD_PRICE_SOUTH_SOUTHEAST
        else:
            standard_price = PRIVALIA_STANDARD_PRICE_OTHER

        options: list[ShippingInfo] = [
            ShippingInfo(
                price=standard_price,
                status="Grátis" if is_free_premium else "Disponível",
                estimated_delivery_days=6 if cep.startswith(("01", "02", "03", "04", "05", "06", "07", "08", "09")) else (9 if is_south_se else 13),
                raw_text="Entrega Padrão (Campanha)" + (" - Frete Grátis Premium" if is_free_premium else f" - R$ {standard_price:.2f}"),
                service_name="Entrega Padrão",
                service_id="privalia_padrao",
                estimate_display="5 a 8 dias úteis" if is_south_se else "10 a 16 dias úteis",
                estimate_unit="bd",
                is_free_shipping=is_free_premium,
            ),
        ]

        # Se for elegível ou para capitais, adiciona Expressa
        if any(cep.startswith(pref) for pref in METRO_CAPITAL_PREFIXES):
            options.append(
                ShippingInfo(
                    price=PRIVALIA_EXPRESS_PRICE,
                    status="Disponível",
                    estimated_delivery_days=4,
                    raw_text=f"Entrega Expressa - R$ {PRIVALIA_EXPRESS_PRICE:.2f}",
                    service_name="Entrega Expressa",
                    service_id="privalia_expressa",
                    estimate_display="3 a 5 dias úteis",
                    estimate_unit="bd",
                    is_free_shipping=False,
                )
            )

        return ShippingCalculation(
            state=ShippingState.AVAILABLE,
            shipping_options=sorted_shipping_options(options),
            message="Calculado com base nas regras logisticas da Privalia Brasil",
        )
