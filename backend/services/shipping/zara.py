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

# Política oficial Zara Brasil (zara.com / kids.zara.com)
ZARA_FREE_SHIPPING_THRESHOLD: float = 499.00
ZARA_STANDARD_SHIPPING_PRICE: float = 14.00
ZARA_EXPRESS_SHIPPING_PRICE: float = 20.00


class ZaraShipping(BaseShipping):
    """Provedor determinístico baseado na política oficial de logística da Zara Brasil (Inditex).
    
    A Zara adota tabela fixa nacional de frete para e-commerce no Brasil:
      - Retirada em Loja física: R$ 0,00 (Grátis, 1 a 3 dias úteis).
      - Entrega Padrão Domiciliar: R$ 14,00 (2 a 7 dias úteis) — Grátis a partir de R$ 499,00.
      - Entrega Expressa / Dia Seguinte: R$ 20,00 (1 a 2 dias úteis) para capitais e regiões metropolitanas.
    """

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

        # Resolve preço do item para apurar isenção de frete
        price_discount = get_field(product, "price_discount")
        price_full = get_field(product, "price_full")
        effective_price = float(price_discount if price_discount is not None else (price_full or 0.0))

        is_free_standard = effective_price >= ZARA_FREE_SHIPPING_THRESHOLD
        standard_price = 0.0 if is_free_standard else ZARA_STANDARD_SHIPPING_PRICE

        options: list[ShippingInfo] = [
            ShippingInfo(
                price=standard_price,
                status="Grátis" if is_free_standard else "Disponível",
                estimated_delivery_days=5,
                raw_text="Entrega padrão (2-7 dias úteis)" + (" - Frete Grátis" if is_free_standard else f" - R$ {standard_price:.2f}"),
                service_name="Entrega Padrão",
                service_id="zara_padrao",
                estimate_display="2 a 7 dias úteis",
                estimate_unit="bd",
                is_free_shipping=is_free_standard,
            ),
            ShippingInfo(
                price=0.0,
                status="Grátis",
                estimated_delivery_days=2,
                raw_text="Retirada em Loja (1-3 dias úteis) - Grátis",
                service_name="Retirada em Loja",
                service_id="zara_retirada",
                estimate_display="1 a 3 dias úteis",
                estimate_unit="bd",
                is_free_shipping=True,
            ),
        ]

        # Para capitais / regiões metropolitanas com prefixos selecionados (Grande SP, Grande RJ, Grande BH, BSB, etc.)
        metro_prefixes = ("01", "02", "03", "04", "05", "06", "07", "08", "09", "20", "21", "22", "23", "24", "25", "30", "31", "70", "71")
        if any(cep.startswith(pref) for pref in metro_prefixes):
            options.append(
                ShippingInfo(
                    price=ZARA_EXPRESS_SHIPPING_PRICE,
                    status="Disponível",
                    estimated_delivery_days=1,
                    raw_text=f"Entrega no dia seguinte - R$ {ZARA_EXPRESS_SHIPPING_PRICE:.2f}",
                    service_name="Entrega Expressa",
                    service_id="zara_expressa",
                    estimate_display="1 a 2 dias úteis",
                    estimate_unit="bd",
                    is_free_shipping=False,
                )
            )

        return ShippingCalculation(
            state=ShippingState.AVAILABLE,
            shipping_options=sorted_shipping_options(options),
        )
