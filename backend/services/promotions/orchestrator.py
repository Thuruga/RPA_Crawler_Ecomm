"""Layered conditional-promotion orchestration with honest partial outcomes."""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from core.models import (
    ConditionalOffer,
    ConditionalOfferResult,
    OfferCacheMetadata,
    OfferProbeLevel,
    OfferSource,
    OfferScope,
)
from services.promotions.cache import (
    EXPENSIVE_TTL_SECONDS,
    PRODUCT_NEGATIVE_TTL_SECONDS,
    PRODUCT_POSITIVE_TTL_SECONDS,
    PromotionCache,
    make_product_key,
)


_STATUS_PRECEDENCE = {
    "error": 60,
    "blocked": 50,
    "requires_auth": 40,
    "found": 30,
    "unsupported": 20,
    "none_detected": 10,
}
_ALLOWED_CONFIRM_TRIGGERS = {"product_opened", "monitor_started", "manual_revalidate"}
_ALLOWED_COLLECTION_TRIGGERS = {"search", "category", "monitor_cycle", *_ALLOWED_CONFIRM_TRIGGERS}


def aggregate_status(statuses: Iterable[str]) -> str:
    normalized = [getattr(status, "value", status) for status in statuses]
    if not normalized:
        return "none_detected"
    return max(normalized, key=lambda status: _STATUS_PRECEDENCE.get(status, 0))


def merge_offers(offers: Iterable[ConditionalOffer]) -> tuple[list[ConditionalOffer], list[str]]:
    """Compatibility wrapper around the single canonical evidence merge."""
    from services.promotions.parsers import deduplicate_offers

    return deduplicate_offers(offers)


def offer_fixture(
    *,
    state: str,
    authority: str,
    price: str,
    sku_id: str,
) -> ConditionalOffer:
    normalized_authority = "advertised" if authority == "observed" else authority
    return ConditionalOffer(
        offer_key=f"fixture:{sku_id}",
        condition_type="membership",
        condition_label="Cliente Prime",
        eligibility="requires_membership",
        price=Decimal(price),
        scope=OfferScope(sku_id=sku_id),
        state=state,
        source=OfferSource(kind="fixture", ref="fixture", authority=normalized_authority),
    )


async def _call(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        value = callback(*args, **kwargs)
    except TypeError:
        value = callback()
    if inspect.isawaitable(value):
        return await value
    return value


def _as_result(value: Any, level: OfferProbeLevel) -> ConditionalOfferResult:
    if isinstance(value, ConditionalOfferResult):
        result = value.model_copy(deep=True)
        result.probe_level = level
        return result
    if isinstance(value, dict):
        payload = dict(value)
        payload.setdefault("probe_level", level)
        return ConditionalOfferResult.model_validate(payload)
    return ConditionalOfferResult(status="none_detected", probe_level=level)


class PromotionOrchestrator:
    def __init__(
        self,
        *,
        probe: Callable[..., Any] | None = None,
        fetch: Callable[..., Any] | None = None,
        confirm: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl: float | None = None,
        concurrency_per_domain: int = 2,
        retries: int = 2,
    ):
        self.probe = probe
        self.fetch = fetch
        self.confirm = confirm
        self.clock = clock
        self.ttl = ttl
        self.cache = PromotionCache(clock=clock)
        self.retries = retries
        self._singleflight_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task[ConditionalOfferResult]] = {}
        self._domain_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(max(1, concurrency_per_domain))
        )

    async def _execute_callback(self, callback: Callable[..., Any], level: OfferProbeLevel) -> ConditionalOfferResult:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return _as_result(await _call(callback, level.value), level)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                await asyncio.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
            except Exception as exc:
                return ConditionalOfferResult(
                    status="error", probe_level=level,
                    diagnostics=[f"probe failed: {type(exc).__name__}"],
                )
        return ConditionalOfferResult(
            status="error", probe_level=level,
            diagnostics=[f"probe timed out after {self.retries + 1} attempts: {type(last_error).__name__}"],
        )

    async def _uncached_collect(
        self,
        *,
        level: OfferProbeLevel,
        trigger: str,
        advertised: list[Any],
        domain: str,
    ) -> ConditionalOfferResult:
        async with self._domain_semaphores[domain]:
            parts: list[ConditionalOfferResult] = []
            advertised_values = list(advertised)
            advertised_offers = [
                offer for offer in advertised if isinstance(offer, ConditionalOffer)
            ]
            if advertised_offers:
                parts.append(
                    ConditionalOfferResult(
                        status="found",
                        offers=advertised_offers,
                        probe_level=level,
                        diagnostics=["preserved advertised evidence before confirmation"],
                    )
                )
            callback = self.probe or self.fetch
            if callback is not None:
                parts.append(await self._execute_callback(callback, level))
            else:
                parts.append(ConditionalOfferResult(status="none_detected", probe_level=level))

            confirmation_candidates = list(advertised_values)
            seen_confirmation_keys = {
                getattr(offer, "offer_key", None) for offer in confirmation_candidates
            }
            for part in parts:
                for offer in part.offers:
                    if offer.state not in {"advertised", "requires_auth"}:
                        continue
                    if offer.offer_key in seen_confirmation_keys:
                        continue
                    seen_confirmation_keys.add(offer.offer_key)
                    confirmation_candidates.append(offer)

            if self.confirm is not None and confirmation_candidates and trigger in _ALLOWED_CONFIRM_TRIGGERS:
                confirmed: list[ConditionalOffer] = []
                confirm_statuses: list[str] = []
                diagnostics: list[str] = []
                confirm_sources: list[str] = []
                for offer in confirmation_candidates:
                    try:
                        value = await _call(self.confirm, offer)
                        result = _as_result(value, OfferProbeLevel.CONFIRM)
                        confirmed.extend(result.offers)
                        confirm_statuses.append(result.status.value)
                        diagnostics.extend(result.diagnostics)
                        confirm_sources.extend(result.sources_attempted)
                    except Exception as exc:
                        confirm_statuses.append("error")
                        diagnostics.append(f"confirm failed: {type(exc).__name__}")
                parts.append(ConditionalOfferResult(
                    status=aggregate_status(confirm_statuses), offers=confirmed,
                    probe_level="confirm", diagnostics=diagnostics,
                    sources_attempted=list(dict.fromkeys(confirm_sources)),
                ))

            all_offers = [offer for part in parts for offer in part.offers]
            merged, merge_diagnostics = merge_offers(all_offers)
            status = "found" if merged else aggregate_status(part.status.value for part in parts)
            return ConditionalOfferResult(
                status=status,
                offers=merged,
                probe_level=level,
                sources_attempted=list(dict.fromkeys(
                    source for part in parts for source in part.sources_attempted
                )),
                diagnostics=[diagnostic for part in parts for diagnostic in part.diagnostics] + merge_diagnostics,
            )

    async def collect(
        self,
        *,
        brand: str,
        url: str,
        trigger: str = "product_opened",
        advertised: Iterable[Any] | None = None,
        force: bool = False,
        product_detail: Any = None,
        sku_id: str | None = None,
        seller_id: str | None = None,
    ) -> ConditionalOfferResult:
        if trigger not in _ALLOWED_COLLECTION_TRIGGERS:
            raise ValueError("unsupported conditional-offer trigger")
        if force and trigger in {"search", "category"}:
            raise ValueError("force revalidation is forbidden for search/category triggers")
        if trigger in {"search", "category"}:
            level = OfferProbeLevel.FAST
        else:
            level = OfferProbeLevel.FULL
        key = make_product_key(
            brand=brand, url=url, level=level.value, sku_id=sku_id, seller_id=seller_id
        )
        stale_value, _ = self.cache.get_product(key, allow_stale=True)
        if not force:
            cached, stale = self.cache.get_product(key)
            if cached is not None:
                result = cached.model_copy(deep=True)
                result.cache = OfferCacheMetadata(hit=True, stale=stale)
                return result

        advertised_list = list(advertised or [])
        async with self._singleflight_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._uncached_collect(
                    level=level, trigger=trigger, advertised=advertised_list,
                    domain=(urlsplit(url).hostname or brand).casefold(),
                ))
                self._inflight[key] = task
        try:
            result = await asyncio.shield(task)
        finally:
            if task.done():
                async with self._singleflight_lock:
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

        if result.status in {"error", "blocked"} and stale_value is not None and stale_value.offers:
            preserved = stale_value.model_copy(deep=True)
            preserved.diagnostics.extend(result.diagnostics)
            preserved.diagnostics.append("served stale valid offers after expensive probe failure")
            preserved.cache = OfferCacheMetadata(hit=True, stale=True)
            return preserved

        if self.ttl is not None:
            ttl = self.ttl
        elif level != OfferProbeLevel.FAST:
            ttl = EXPENSIVE_TTL_SECONDS
        elif result.status == "found":
            ttl = PRODUCT_POSITIVE_TTL_SECONDS
        else:
            ttl = PRODUCT_NEGATIVE_TTL_SECONDS
        self.cache.set_product(key, result.model_copy(deep=True), ttl=ttl)
        return result
