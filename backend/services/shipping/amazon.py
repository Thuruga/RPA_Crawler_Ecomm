from __future__ import annotations

import logging
from typing import Any

from core.models import SearchProductResult, ShippingInfo
from services.shipping.base import (
    BaseShipping,
    DEFAULT_MESSAGES,
    ShippingCalculation,
    ShippingState,
    get_field,
    is_url_allowed_for_brand,
    normalize_zipcode,
    sorted_shipping_options,
)

logger = logging.getLogger(__name__)


def _info_from_result(result: dict[str, Any]) -> ShippingInfo:
    price = result.get("shipping_price")
    is_free = bool(result.get("is_free_shipping"))
    days = result.get("estimated_delivery_days")
    raw_text = result.get("delivery_raw_text")
    return ShippingInfo(
        price=price,
        status="Gratis" if is_free else "Disponivel",
        estimated_delivery_days=days,
        raw_text=raw_text,
        is_free_shipping=is_free or price == 0.0,
    )


class AmazonShipping(BaseShipping):
    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Any:
        if self._engine is None:
            # D-04: resolve pela factory (import lazy preserva a mesma seguranca
            # contra ciclo de import que a instanciacao direta ja garantia — a
            # factory importa brand_service, grafo maior que o dos modulos de
            # frete). A factory devolve a engine ja envolta em
            # ResilientMarketplaceEngine, entao frete compartilha cache e estado
            # de circuito com busca, detalhe e monitor: uma leitura recente pode
            # servir uma cotacao, e um marketplace bloqueado fica bloqueado
            # tambem para frete.
            from services.engines.factory import engine_factory

            self._engine = engine_factory.get_engine("amazon")
        return self._engine

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

        try:
            result = await self.engine.calculate_shipping_advanced(product_url, cep)
        except Exception as exc:
            logger.warning(
                "[AmazonShipping] quote failed brand=%s status=%s",
                get_field(brand, "brand_key", "unknown"),
                type(exc).__name__,
            )
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message="Frete temporariamente indisponivel",
            )

        # D-04 / Pitfall 4: o gate de _marketplace_read_state vem ANTES do teste
        # de truthiness abaixo, porque o dict de leitura degradada do coordinator
        # e truthy e passaria batido direto para o calculo de preco.
        marketplace_read_state = (
            result.get("_marketplace_read_state") if isinstance(result, dict) else None
        )
        if marketplace_read_state == "blocked":
            return ShippingCalculation(
                state=ShippingState.BLOCKED,
                message=DEFAULT_MESSAGES[ShippingState.BLOCKED],
            )
        if marketplace_read_state == "unsupported":
            return ShippingCalculation(
                state=ShippingState.UNSUPPORTED,
                message=DEFAULT_MESSAGES[ShippingState.UNSUPPORTED],
            )
        if marketplace_read_state == "stale":
            return ShippingCalculation(
                state=ShippingState.STALE,
                shipping_options=sorted_shipping_options([_info_from_result(result)]),
                message=DEFAULT_MESSAGES[ShippingState.STALE],
            )

        if not result:
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message="Frete temporariamente indisponivel",
            )

        if "error" in result:
            # CAPTCHA/anti-bot e transitorio por sessao — NUNCA BLOCKED (D-02).
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message=result["error"],
            )

        return ShippingCalculation(
            state=ShippingState.AVAILABLE,
            shipping_options=sorted_shipping_options([_info_from_result(result)]),
        )
