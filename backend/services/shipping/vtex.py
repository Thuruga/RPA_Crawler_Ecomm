from __future__ import annotations

import logging
from typing import Any

from core.models import SearchProductResult
from services.shipping.base import (
    BaseShipping,
    ShippingCalculation,
    ShippingState,
    get_field,
    is_url_allowed_for_brand,
    normalize_zipcode,
)
from services.vtex_api_scraper import VtexApiClient

logger = logging.getLogger(__name__)

class VtexShipping(BaseShipping):
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

        sku_id = get_field(product, "sku_id")
        seller_id = get_field(product, "seller_id")

        if not sku_id or not seller_id:
            return ShippingCalculation(
                state=ShippingState.UNSUPPORTED,
                message="Produto VTEX sem sku_id/seller_id",
            )

        brand_key = get_field(brand, "brand_key")
        sales_channel = get_field(brand, "vtex_sales_channel")
        try:
            result = await VtexApiClient.calculate_for_brand(
                brand_key=brand_key,
                sku_id=str(sku_id),
                seller_id=str(seller_id),
                zipcode=cep,
                sales_channel=sales_channel,
            )
        except Exception as exc:
            logger.warning(
                "[VtexShipping] quote failed brand=%s status=%s",
                brand_key,
                type(exc).__name__,
            )
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message="Frete temporariamente indisponivel",
            )

        state_str = result.get("state")
        options = result.get("shipping_options") or []

        if state_str == "available":
            return ShippingCalculation(
                state=ShippingState.AVAILABLE,
                shipping_options=options,
            )
        elif state_str == "unavailable_for_cep":
            return ShippingCalculation(
                state=ShippingState.UNAVAILABLE_FOR_CEP,
                message="Entrega indisponivel para este CEP",
            )
        else:
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message="Frete temporariamente indisponivel",
            )
