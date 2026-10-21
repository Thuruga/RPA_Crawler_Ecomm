from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

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
from services.wake_token import resolve_wake_access_token_override

logger = logging.getLogger(__name__)

GRAPHQL_ENDPOINT = "https://storefront-api.fbits.net/graphql"

PRODUCT_QUERY = """
query ProductForShipping($productId: Long!) {
  product(productId: $productId) {
    productName
    productId
    productVariantId
    sku
    prices { price }
  }
}
""".strip()

SHIPPING_QUERY = """
query Shipping($cep: CEP, $productVariantId: Long, $quantity: Int) {
  shippingQuotes(
    cep: $cep,
    productVariantId: $productVariantId,
    quantity: $quantity
  ) {
    shippingQuoteId
    name
    value
    deadline
    deadlineInHours
    type
    distributionCenterId
  }
}
""".strip()


class WakeShipping(BaseShipping):
    def __init__(self, session_factory: Any = None) -> None:
        self.session_factory = session_factory

    def _open_session(self):
        if self.session_factory:
            return self.session_factory()
        return aiohttp.ClientSession()

    async def _extract_token_from_storefront(
        self, session: aiohttp.ClientSession, brand: Any
    ) -> str | None:
        domain = str(get_field(brand, "domain", "") or "").replace("https://", "").replace("http://", "").strip("/")
        if not domain:
            return None
        try:
            url = f"https://{domain}/"
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    match = re.search(
                        r"""storefrontAccessToken\s*:\s*['"]([^'"]+)['"]""",
                        text,
                        re.IGNORECASE,
                    )
                    if match:
                        return match.group(1).strip()
        except Exception:
            pass
        return None

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

        variant_id = get_field(product, "shipping_variant_id")
        try:
            async with self._open_session() as session:
                token = get_field(brand, "wake_access_token") or resolve_wake_access_token_override(
                    brand
                )
                if not token:
                    token = await self._extract_token_from_storefront(session, brand)
                if not token:
                    return ShippingCalculation(
                        state=ShippingState.UNSUPPORTED,
                        message="Token Wake nao resolvido",
                    )

                if not variant_id:
                    product_id = get_field(product, "shipping_product_id") or self._product_id_from_url(product_url)
                    if not product_id:
                        return ShippingCalculation(
                            state=ShippingState.UNSUPPORTED,
                            message="Produto Wake sem productId para cotacao",
                        )
                    variant_id = await self._fetch_variant_id(session, str(token), int(product_id))
                if not variant_id:
                    return ShippingCalculation(
                        state=ShippingState.UNSUPPORTED,
                        message="Produto Wake sem productVariantId para cotacao",
                    )
                options = await self._fetch_quotes(session, str(token), int(variant_id), cep)
        except Exception as exc:
            logger.warning(
                "[WakeShipping] quote failed brand=%s status=%s",
                get_field(brand, "brand_key", "unknown"),
                type(exc).__name__,
            )
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message="Frete temporariamente indisponivel",
            )

        if not options:
            return ShippingCalculation(
                state=ShippingState.UNAVAILABLE_FOR_CEP,
                message="Entrega indisponivel para este CEP",
            )

        return ShippingCalculation(
            state=ShippingState.AVAILABLE,
            shipping_options=sorted_shipping_options(options),
        )

    async def _graphql(
        self,
        session: aiohttp.ClientSession,
        token: str,
        query: str,
        variables: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with session.post(
            GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
            headers={"TCS-Access-Token": token},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 500:
                raise RuntimeError(f"Wake GraphQL HTTP {resp.status}")
            try:
                data = await resp.json()
            except Exception:
                return None
        if not isinstance(data, dict) or data.get("errors"):
            return None
        return data

    async def _fetch_variant_id(
        self,
        session: aiohttp.ClientSession,
        token: str,
        product_id: int,
    ) -> str | None:
        data = await self._graphql(session, token, PRODUCT_QUERY, {"productId": product_id})
        product = ((data or {}).get("data") or {}).get("product") or {}
        variant_id = product.get("productVariantId")
        return str(variant_id) if variant_id else None

    async def _fetch_quotes(
        self,
        session: aiohttp.ClientSession,
        token: str,
        product_variant_id: int,
        cep: str,
    ) -> list[ShippingInfo]:
        data = await self._graphql(
            session,
            token,
            SHIPPING_QUERY,
            {"cep": cep, "productVariantId": product_variant_id, "quantity": 1},
        )
        quotes = ((data or {}).get("data") or {}).get("shippingQuotes") or []
        return self._parse_quotes(quotes)

    def _parse_quotes(self, quotes: list[dict[str, Any]]) -> list[ShippingInfo]:
        options: list[ShippingInfo] = []
        for quote in quotes:
            price = self._parse_price(quote.get("value"))
            days = self._parse_deadline_days(quote)
            service_name = quote.get("name") or "Entrega"
            options.append(
                ShippingInfo(
                    price=price,
                    status="Gratis" if price == 0.0 else "Disponivel",
                    estimated_delivery_days=days,
                    raw_text=f"{service_name} - {days} dias" if days is not None else service_name,
                    service_name=service_name,
                    service_id=str(quote.get("shippingQuoteId") or service_name),
                    estimate_display=f"Ate {days} dias uteis" if days is not None else None,
                    estimate_unit="d" if days is not None else None,
                    is_free_shipping=price == 0.0,
                )
            )
        return options

    @staticmethod
    def _product_id_from_url(product_url: str) -> str | None:
        match = re.search(r"-(\d+)(?:[/?#].*)?$", product_url)
        return match.group(1) if match else None

    @staticmethod
    def _parse_price(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return round(float(str(value).replace(",", ".").strip()), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_deadline_days(quote: dict[str, Any]) -> int | None:
        deadline = quote.get("deadline")
        if deadline is not None:
            try:
                return int(deadline)
            except (TypeError, ValueError):
                return None
        hours = quote.get("deadlineInHours")
        if hours is not None:
            try:
                return max(1, int(hours) // 24)
            except (TypeError, ValueError):
                return None
        return None
