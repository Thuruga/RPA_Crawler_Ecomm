from __future__ import annotations

import json
import re
from typing import Any

from core.models import BrandSearchResult
from services.marketplace_resilience import (
    MarketplaceHttpError,
    MarketplaceUnavailable,
    marketplace_coordinator,
)
from services.shipping.base import get_field


# WR-01: anchor the digits to an actual HTTP-status context so a bare "403"/
# "429" substring (e.g. a timeout in ms, a port number, an item id, "R$ 429,00")
# inside a raw exception message never gets misclassified as an anti-bot block.
_BLOCKED_PATTERN = re.compile(
    r"HTTP\s*(403|429)\b|\b(403|429)\s*(Forbidden|Too Many Requests)\b|captcha|anubis|anti-bot",
    re.IGNORECASE,
)


class ResilientMarketplaceEngine:
    """Mantem o contrato da engine e coordena todas as leituras externas."""

    def __init__(self, engine: Any, marketplace: str) -> None:
        self._engine = engine
        self._marketplace = marketplace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)

    async def search(self, query: str, **kwargs: Any) -> BrandSearchResult:
        source = self._source("search", query, kwargs)

        async def load() -> dict[str, Any]:
            result = await self._engine.search(query=query, **kwargs)
            self._raise_if_blocked(getattr(result, "error", None))
            return result.model_dump(mode="json")

        try:
            read = await marketplace_coordinator.execute(self._marketplace, source, load)
        except MarketplaceUnavailable as exc:
            return BrandSearchResult(
                brand_key=self._marketplace,
                brand_name=self._engine.get_engine_name(),
                error=self._status_message(exc.state, None, exc.next_retry_at),
            )
        result = BrandSearchResult.model_validate(read.value)
        if read.state == "stale":
            result.error = self._status_message(read.state, read.last_success_at, read.next_retry_at)
        return result

    async def get_pdp_product(self, product_url: str):
        return await self._dict_call("pdp", product_url, self._engine.get_pdp_product)

    async def get_product_details(self, product_url: str):
        return await self._dict_call("detail", product_url, self._engine.get_product_details)

    async def calculate_shipping(self, product: Any, zipcode: str):
        # WR-02: key by the product's stable URL (as calculate_shipping_advanced
        # already does), not by str(product). `product` is a plain dict carrying
        # per-search-run fields (final_match_score, image_match_score, current
        # preco, etc.) that differ from one search to the next even for the
        # identical product URL — keying on str(product) defeated the
        # coordinator's single-flight dedup / 12h cache reuse and grew
        # marketplace_resilience.json's `entries` by one new key per product
        # per search, forever (no eviction).
        product_url = str(get_field(product, "url", "") or "")
        source = self._source("shipping", product_url, {"zipcode": zipcode})
        # A `None` return from calculate_shipping() does not always mean the
        # marketplace blocked the request: some engines (e.g. Amazon) treat
        # Tier 2 shipping as a permanent unimplemented stub and always return
        # None by design. Respect the engine's own SHIPPING_TIER2_BLOCKS_ON_NONE
        # contract so a documented no-op cannot poison the shared per-marketplace
        # circuit breaker (CR-01) — this also makes the "unsupported" read state
        # reachable through real production wiring instead of only test mocks
        # (WR-03).
        unsupported = not getattr(self._engine, "SHIPPING_TIER2_BLOCKS_ON_NONE", True)
        return await self._dict_call(
            "shipping", source, lambda _: self._engine.calculate_shipping(product, zipcode),
            unsupported=unsupported,
        )

    async def calculate_shipping_advanced(self, url: str, zipcode: str):
        source = self._source("shipping-advanced", url, {"zipcode": zipcode})
        return await self._dict_call(
            "shipping-advanced", source,
            lambda _: self._engine.calculate_shipping_advanced(url, zipcode),
        )

    async def _dict_call(self, operation, source, function, *, unsupported=False):
        async def load():
            value = await function(source)
            if value is None:
                if operation == "detail":
                    return {"seller": None, "price": None}
                if operation == "shipping-advanced":
                    raise MarketplaceUnavailable("temporary_failure")
                if unsupported:
                    raise MarketplaceUnavailable("unsupported")
                raise MarketplaceHttpError(403)
            if isinstance(value, dict):
                # A CAPTCHA while calculating a quote is local to the browser
                # session and CEP/product combination.  It must not open the
                # marketplace-wide persistent circuit: doing so previously made
                # every subsequent quote report BLOCKED for 12–72 hours, even
                # when the next browser session could succeed.  Keep the engine
                # signal honest, but expose it as a retryable quote failure.
                if operation == "shipping-advanced" and (
                    value.get("_marketplace_read_state") == "blocked"
                    or _BLOCKED_PATTERN.search(str(value.get("error") or value.get("message") or ""))
                ):
                    raise MarketplaceUnavailable("temporary_failure")
                self._raise_if_blocked(value.get("error") or value.get("message"))
            return value

        try:
            read = await marketplace_coordinator.execute(
                self._marketplace,
                self._source(operation, source, {}),
                load,
                unsupported=unsupported,
            )
        except MarketplaceUnavailable as exc:
            if exc.state == "temporary_failure":
                return None
            return {
                "_marketplace_read_state": exc.state,
                "next_retry_at": exc.next_retry_at,
                "last_success_at": None,
            }
        value = dict(read.value)
        value["_marketplace_read_state"] = read.state
        value["last_success_at"] = read.last_success_at
        value["next_retry_at"] = read.next_retry_at
        return value

    @staticmethod
    def _source(operation: str, primary: str, kwargs: dict[str, Any]) -> str:
        suffix = json.dumps(kwargs, sort_keys=True, ensure_ascii=True, default=str)
        return f"{operation}:{primary}:{suffix}"

    @staticmethod
    def _raise_if_blocked(message: Any) -> None:
        text = str(message or "")
        match = _BLOCKED_PATTERN.search(text)
        if not match:
            return
        status = 429 if "429" in match.group(0) else 403
        raise MarketplaceHttpError(status)

    @staticmethod
    def _status_message(state: str, last_success_at: str | None, next_retry_at: str | None) -> str:
        details = []
        if last_success_at:
            details.append(f"ultimo sucesso: {last_success_at}")
        if next_retry_at:
            details.append(f"proxima tentativa: {next_retry_at}")
        suffix = f" ({'; '.join(details)})" if details else ""
        return f"marketplace {state}{suffix}"
