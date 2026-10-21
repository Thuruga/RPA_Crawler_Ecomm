from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.marketplace_resilience import (
    MarketplaceCoordinator,
    MarketplaceHttpError,
    MarketplaceUnavailable,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.mark.asyncio
async def test_deduplicates_same_source_and_limits_global_concurrency(tmp_path):
    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "state.json", clock=clock, jitter=lambda: 0)
    active = maximum = calls = 0

    async def loader(value):
        nonlocal active, maximum, calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"value": value}

    first, duplicate, other = await asyncio.gather(
        coordinator.execute("amazon", "item-a", lambda: loader("a")),
        coordinator.execute("amazon", "item-a", lambda: loader("duplicate")),
        coordinator.execute("netshoes", "item-b", lambda: loader("b")),
    )
    assert first.value == duplicate.value == {"value": "a"}
    assert other.value == {"value": "b"}
    assert calls == 2
    assert maximum == 1


@pytest.mark.asyncio
async def test_twelve_hour_interval_and_jitter_reuse_shared_cache(tmp_path):
    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "state.json", clock=clock, jitter=lambda: 3600)
    calls = 0

    async def loader():
        nonlocal calls
        calls += 1
        return {"price": 99.9}

    fresh = await coordinator.execute("mercado_livre", "MLB1", loader)
    clock.now += timedelta(hours=12, minutes=59)
    cached = await coordinator.execute("mercado_livre", "MLB1", loader)
    clock.now += timedelta(minutes=2)
    refreshed = await coordinator.execute("mercado_livre", "MLB1", loader)
    assert fresh.cache_hit is False
    assert cached.cache_hit is True
    assert cached.state == "current"
    assert refreshed.cache_hit is False
    assert calls == 2


@pytest.mark.asyncio
async def test_403_progresses_to_72_hours_and_success_resets_circuit(tmp_path):
    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "state.json", clock=clock, jitter=lambda: 0)

    async def forbidden():
        raise MarketplaceHttpError(403)

    for expected_hours in (12, 24, 36, 48, 60, 72, 72):
        with pytest.raises(MarketplaceUnavailable) as captured:
            await coordinator.execute("amazon", f"item-{expected_hours}", forbidden)
        blocked_until = datetime.fromisoformat(captured.value.next_retry_at)
        assert blocked_until == clock.now + timedelta(hours=expected_hours)
        clock.now = blocked_until + timedelta(seconds=1)

    success = await coordinator.execute("amazon", "working-item", lambda: asyncio.sleep(0, {"ok": True}))
    assert success.value == {"ok": True}
    clock.now += timedelta(hours=13)
    with pytest.raises(MarketplaceUnavailable) as captured:
        await coordinator.execute("amazon", "new-item", forbidden)
    assert datetime.fromisoformat(captured.value.next_retry_at) == clock.now + timedelta(hours=12)


@pytest.mark.asyncio
async def test_429_honors_retry_after_and_failure_returns_stale_value(tmp_path):
    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "state.json", clock=clock, jitter=lambda: 0)
    initial = await coordinator.execute(
        "netshoes", "product-1", lambda: asyncio.sleep(0, {"price": 120})
    )
    clock.now += timedelta(hours=13)

    async def limited():
        raise MarketplaceHttpError(429, retry_after_seconds=90)

    stale = await coordinator.execute("netshoes", "product-1", limited)
    assert stale.state == "stale"
    assert stale.value == {"price": 120}
    assert stale.last_success_at == initial.last_success_at
    assert datetime.fromisoformat(stale.next_retry_at) == clock.now + timedelta(seconds=90)


@pytest.mark.asyncio
async def test_unsupported_never_calls_loader(tmp_path):
    coordinator = MarketplaceCoordinator(tmp_path / "state.json", jitter=lambda: 0)
    called = False

    async def loader():
        nonlocal called
        called = True
        return {}

    with pytest.raises(MarketplaceUnavailable) as captured:
        await coordinator.execute("amazon", "shipping", loader, unsupported=True)
    assert captured.value.state == "unsupported"
    assert called is False

@pytest.mark.asyncio
async def test_engine_proxy_reuses_search_cache_without_changing_public_model(tmp_path, monkeypatch):
    from core.models import BrandSearchResult
    from services.engines import resilient_marketplace as proxy_module
    from services.engines.resilient_marketplace import ResilientMarketplaceEngine

    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "proxy.json", clock=clock, jitter=lambda: 0)
    monkeypatch.setattr(proxy_module, "marketplace_coordinator", coordinator)

    class Engine:
        calls = 0

        def get_engine_name(self):
            return "Amazon"

        async def search(self, query, **kwargs):
            self.calls += 1
            return BrandSearchResult(brand_key="amazon", brand_name="Amazon", products=[])

    raw = Engine()
    proxy = ResilientMarketplaceEngine(raw, "amazon")
    first = await proxy.search("camisa", max_results=10)
    second = await proxy.search("camisa", max_results=10)

    assert first.brand_key == second.brand_key == "amazon"
    assert raw.calls == 1


@pytest.mark.asyncio
async def test_engine_proxy_marks_cached_pdp_stale_without_new_observation(tmp_path, monkeypatch):
    from services.engines import resilient_marketplace as proxy_module
    from services.engines.resilient_marketplace import ResilientMarketplaceEngine

    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "proxy.json", clock=clock, jitter=lambda: 0)
    monkeypatch.setattr(proxy_module, "marketplace_coordinator", coordinator)

    class Engine:
        blocked = False

        async def get_pdp_product(self, url):
            if self.blocked:
                raise MarketplaceHttpError(403)
            return {"url": url, "price_full": 100}

    raw = Engine()
    proxy = ResilientMarketplaceEngine(raw, "netshoes")
    current = await proxy.get_pdp_product("https://example.test/item")
    clock.now += timedelta(hours=13)
    raw.blocked = True
    stale = await proxy.get_pdp_product("https://example.test/item")

    assert current["_marketplace_read_state"] == "current"
    assert stale["_marketplace_read_state"] == "stale"
    assert stale["price_full"] == 100
    assert stale["last_success_at"] == current["last_success_at"]


@pytest.mark.asyncio
async def test_calculate_shipping_none_stub_does_not_poison_shared_circuit(tmp_path, monkeypatch):
    """CR-01 regression: an engine whose Tier 2 calculate_shipping() is a
    permanent unimplemented stub (SHIPPING_TIER2_BLOCKS_ON_NONE = False, e.g.
    Amazon) must never have its None no-op result recorded as a 403 failure
    against the shared per-marketplace circuit — doing so would degrade
    search/PDP availability for up to 72h from ordinary shipping-tier calls."""
    from core.models import BrandSearchResult
    from services.engines import resilient_marketplace as proxy_module
    from services.engines.resilient_marketplace import ResilientMarketplaceEngine

    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "proxy.json", clock=clock, jitter=lambda: 0)
    monkeypatch.setattr(proxy_module, "marketplace_coordinator", coordinator)

    class StubShippingEngine:
        SHIPPING_TIER2_BLOCKS_ON_NONE = False

        def get_engine_name(self):
            return "Amazon"

        async def calculate_shipping(self, product, zipcode):
            return None  # permanent no-op stub, by design

        async def search(self, query, **kwargs):
            return BrandSearchResult(brand_key="amazon", brand_name="Amazon", products=[])

    raw = StubShippingEngine()
    proxy = ResilientMarketplaceEngine(raw, "amazon")

    # Several distinct products in one search result set, mirroring a single
    # compare_product call fanning out calculate_shipping per product.
    for product_id in range(5):
        shipping = await proxy.calculate_shipping({"id": product_id}, "01310100")
        assert shipping["_marketplace_read_state"] == "unsupported"

    # The shared circuit for the whole "amazon" marketplace must be untouched:
    # a plain search right after must not be degraded to blocked/stale.
    result = await proxy.search("camisa", max_results=10)
    assert result.error is None
    assert result.brand_key == "amazon"


@pytest.mark.asyncio
@pytest.mark.parametrize("marketplace", ["amazon", "netshoes"])
async def test_advanced_shipping_without_quote_does_not_block_marketplace(
    monkeypatch, marketplace
):
    from core.models import BrandSearchResult
    from services.engines import resilient_marketplace as proxy_module
    from services.engines.resilient_marketplace import ResilientMarketplaceEngine

    coordinator = MarketplaceCoordinator(Path("unused.json"), clock=Clock(), jitter=lambda: 0)
    state_holder = {"value": {"entries": {}, "circuits": {}}}
    monkeypatch.setattr(coordinator, "_load_state", lambda: deepcopy(state_holder["value"]))
    monkeypatch.setattr(coordinator, "_save_state", lambda value: state_holder.__setitem__("value", deepcopy(value)))
    monkeypatch.setattr(proxy_module, "marketplace_coordinator", coordinator)

    class Engine:
        def get_engine_name(self):
            return marketplace

        async def calculate_shipping_advanced(self, url, zipcode):
            return None  # CEP control or shipping price was not found

        async def search(self, query, **kwargs):
            return BrandSearchResult(brand_key=marketplace, brand_name=marketplace, products=[])

    proxy = ResilientMarketplaceEngine(Engine(), marketplace)
    result = await proxy.calculate_shipping_advanced("https://example.com/product", "01310100")
    assert result is None
    assert state_holder["value"]["circuits"].get(marketplace) is None

    search = await proxy.search("product")
    assert search.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("marketplace", ["amazon", "netshoes"])
async def test_advanced_shipping_captcha_does_not_open_shared_circuit(
    monkeypatch, marketplace
):
    """A quote-session CAPTCHA is retryable and cannot block search/PDP globally."""
    from core.models import BrandSearchResult
    from services.engines import resilient_marketplace as proxy_module
    from services.engines.resilient_marketplace import ResilientMarketplaceEngine

    coordinator = MarketplaceCoordinator(Path("unused.json"), clock=Clock(), jitter=lambda: 0)
    state_holder = {"value": {"entries": {}, "circuits": {}}}
    monkeypatch.setattr(coordinator, "_load_state", lambda: deepcopy(state_holder["value"]))
    monkeypatch.setattr(
        coordinator,
        "_save_state",
        lambda value: state_holder.__setitem__("value", deepcopy(value)),
    )
    monkeypatch.setattr(proxy_module, "marketplace_coordinator", coordinator)

    class Engine:
        def get_engine_name(self):
            return marketplace

        async def calculate_shipping_advanced(self, url, zipcode):
            return {
                "_marketplace_read_state": "blocked",
                "error": "CAPTCHA/anti-bot na sessão de cotação",
            }

        async def search(self, query, **kwargs):
            return BrandSearchResult(brand_key=marketplace, brand_name=marketplace, products=[])

    proxy = ResilientMarketplaceEngine(Engine(), marketplace)
    assert await proxy.calculate_shipping_advanced("https://example.com/product", "01310100") is None
    assert state_holder["value"]["circuits"].get(marketplace) is None
    assert (await proxy.search("product")).error is None


@pytest.mark.asyncio
async def test_calculate_shipping_keys_by_stable_url_not_volatile_product_dict(tmp_path, monkeypatch):
    """WR-02 regression: two calls for the identical product URL but with
    different volatile per-search-run fields (final_match_score, preco, ...)
    must share the same coordinator cache entry — keying by str(product)
    defeated single-flight dedup/12h cache reuse and grew
    marketplace_resilience.json's `entries` unbounded (one key per product
    per search, forever)."""
    from services.engines import resilient_marketplace as proxy_module
    from services.engines.resilient_marketplace import ResilientMarketplaceEngine

    clock = Clock()
    coordinator = MarketplaceCoordinator(tmp_path / "proxy.json", clock=clock, jitter=lambda: 0)
    monkeypatch.setattr(proxy_module, "marketplace_coordinator", coordinator)

    class Engine:
        SHIPPING_TIER2_BLOCKS_ON_NONE = True
        calls = 0

        async def calculate_shipping(self, product, zipcode):
            self.calls += 1
            return {"is_free_shipping": True, "shipping_price": 0.0}

    raw = Engine()
    proxy = ResilientMarketplaceEngine(raw, "mercado_livre")

    product_run_1 = {
        "url": "https://www.mercadolivre.com.br/produto/MLB123",
        "final_match_score": 87.5,
        "preco": 199.9,
    }
    product_run_2 = {
        "url": "https://www.mercadolivre.com.br/produto/MLB123",
        "final_match_score": 42.1,  # differs from run 1 — same product, new search
        "preco": 189.9,  # price changed between search runs
    }

    first = await proxy.calculate_shipping(product_run_1, "01310100")
    second = await proxy.calculate_shipping(product_run_2, "01310100")

    assert raw.calls == 1  # single-flight cache reused across both calls
    assert first["last_success_at"] == second["last_success_at"]
    assert second["_marketplace_read_state"] == "current"


@pytest.mark.parametrize(
    "text",
    [
        "timeout after 429ms",
        "preco R$ 429,00 encontrado",
        "porta 403 em uso",
        "item id 40312345",
    ],
)
def test_blocked_pattern_ignores_bare_digit_substrings(text):
    """WR-01 regression: a bare '403'/'429' substring with no HTTP-status
    context (timeout in ms, price, port number, item id) must not be
    misclassified as an anti-bot block."""
    from services.engines.resilient_marketplace import _BLOCKED_PATTERN

    assert _BLOCKED_PATTERN.search(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 403 Forbidden",
        "HTTP429",
        "403 Forbidden",
        "429 Too Many Requests",
        "captcha detectado",
        "bloqueado por anti-bot",
        "anubis challenge",
    ],
)
def test_blocked_pattern_still_matches_real_block_signals(text):
    """WR-01 regression: genuine HTTP-status-anchored 403/429 signals and the
    non-status keywords (captcha/anubis/anti-bot) must still be detected."""
    from services.engines.resilient_marketplace import _BLOCKED_PATTERN

    assert _BLOCKED_PATTERN.search(text) is not None
