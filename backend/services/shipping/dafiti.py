"""Provedor de frete para Dafiti (Global Fashion Group).

A Dafiti opera com modelo de logística nacional com regras escalonadas:
  - Retira Fácil (Pontos Conveniados): R$ 0,00 (Grátis, 2 a 4 dias úteis).
  - Entrega Padrão Domiciliar:
      - Sul e Sudeste (CEPs 01-39 e 80-99): Grátis a partir de R$ 99,90 (abaixo: R$ 9,90 a R$ 14,90).
      - Demais regiões (CEPs 40-79): Grátis a partir de R$ 199,90 (abaixo: R$ 24,90).
  - Entrega Rápida / Expressa: R$ 19,90 (1 a 3 dias úteis) para regiões metropolitanas.
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

DAFITI_FREE_SHIPPING_THRESHOLD_SOUTH_SOUTHEAST: float = 99.90
DAFITI_FREE_SHIPPING_THRESHOLD_OTHER: float = 199.90

DAFITI_STANDARD_PRICE_SP: float = 9.90
DAFITI_STANDARD_PRICE_SOUTH_SOUTHEAST: float = 14.90
DAFITI_STANDARD_PRICE_OTHER: float = 24.90
DAFITI_EXPRESS_PRICE: float = 19.90

METRO_ZIPCODE_PREFIXES = (
    "01", "02", "03", "04", "05", "06", "07", "08", "09",  # Grande SP
    "20", "21", "22", "23", "24", "25",                     # Grande RJ
    "30", "31",                                             # Grande BH
    "70", "71",                                             # Distrito Federal
    "80", "81",                                             # Grande Curitiba
    "90", "91",                                             # Grande Porto Alegre
)


class DafitiShipping(BaseShipping):
    """Provedor de frete para Dafiti."""

    def _is_south_southeast(self, cep: str) -> bool:
        prefix_2 = int(cep[:2])
        # Sudeste: 01 a 39 (SP 01-19, RJ 20-28, ES 29, MG 30-39)
        # Sul: 80 a 99 (PR 80-87, SC 88-89, RS 90-99)
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
        free_threshold = (
            DAFITI_FREE_SHIPPING_THRESHOLD_SOUTH_SOUTHEAST
            if is_south_se
            else DAFITI_FREE_SHIPPING_THRESHOLD_OTHER
        )

        is_free_standard = effective_price >= free_threshold
        if is_free_standard:
            standard_price = 0.0
        elif cep.startswith(("01", "02", "03", "04", "05", "06", "07", "08", "09")):
            standard_price = DAFITI_STANDARD_PRICE_SP
        elif is_south_se:
            standard_price = DAFITI_STANDARD_PRICE_SOUTH_SOUTHEAST
        else:
            standard_price = DAFITI_STANDARD_PRICE_OTHER

        options: list[ShippingInfo] = [
            ShippingInfo(
                price=standard_price,
                status="Grátis" if is_free_standard else "Disponível",
                estimated_delivery_days=4 if is_south_se else 7,
                raw_text="Entrega Padrão" + (" - Frete Grátis" if is_free_standard else f" - R$ {standard_price:.2f}"),
                service_name="Entrega Padrão",
                service_id="dafiti_padrao",
                estimate_display="3 a 5 dias úteis" if is_south_se else "5 a 9 dias úteis",
                estimate_unit="bd",
                is_free_shipping=is_free_standard,
            ),
            ShippingInfo(
                price=0.0,
                status="Grátis",
                estimated_delivery_days=3,
                raw_text="Retira Fácil (Ponto de Retirada) - Grátis (2-4 dias úteis)",
                service_name="Retira Fácil",
                service_id="dafiti_retira_facil",
                estimate_display="2 a 4 dias úteis",
                estimate_unit="bd",
                is_free_shipping=True,
            ),
        ]

        if any(cep.startswith(pref) for pref in METRO_ZIPCODE_PREFIXES):
            options.append(
                ShippingInfo(
                    price=DAFITI_EXPRESS_PRICE,
                    status="Disponível",
                    estimated_delivery_days=2,
                    raw_text=f"Entrega Rápida (1-3 dias úteis) - R$ {DAFITI_EXPRESS_PRICE:.2f}",
                    service_name="Entrega Rápida",
                    service_id="dafiti_rapida",
                    estimate_display="1 a 3 dias úteis",
                    estimate_unit="bd",
                    is_free_shipping=False,
                )
            )

        return ShippingCalculation(
            state=ShippingState.AVAILABLE,
            shipping_options=sorted_shipping_options(options),
        )
