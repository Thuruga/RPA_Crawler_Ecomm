"""
Phase 01-08 — confiabilidade dos probes Zara e Shopify (D-07, share não-VTEX).

Hermetic: nenhuma chamada de rede/navegador real.
- Zara: mocka `BrowserManager.fetch_html` e `parse_products`.
- Shopify: mocka a sessão aiohttp via `SessionManager.get_session`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.category_discovery import ProbeStatus


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Nunca dorme de verdade durante backoff/throttle nos testes."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


# ---------------------------------------------------------------------------
# Zara: category_has_products — render-budget parity com _discover_and_validate
# ---------------------------------------------------------------------------


def _zara_engine(monkeypatch):
    from services.engines.zara_engine import ZaraEngine

    engine = ZaraEngine(brand_key="zara")
    monkeypatch.setattr(
        engine,
        "_brand_config",
        lambda: {
            "brand_name": "Zara",
            "domain": "www.zara.com",
            "search_url_template": (
                "https://www.zara.com/br/pt/search?searchTerm={query}&section=MAN"
            ),
            "proxy_url": None,
        },
    )
    return engine


@pytest.mark.asyncio
async def test_zara_probe_has_products_when_tiles_parsed(monkeypatch):
    engine = _zara_engine(monkeypatch)

    fetch_mock = AsyncMock(return_value="<html>tiles</html>")
    monkeypatch.setattr("services.engines.zara_engine.BrowserManager.fetch_html", fetch_mock)
    monkeypatch.setattr(
        "services.engines.zara_engine.parse_products",
        lambda html, **kwargs: [{"raw_title": "camisa"}],
    )

    candidate = SimpleNamespace(
        url="https://www.zara.com/br/pt/man-collection-l7421.html", path="man-collection-l7421.html"
    )
    result = await engine.category_has_products(candidate)

    assert result.status is ProbeStatus.HAS_PRODUCTS


@pytest.mark.asyncio
async def test_zara_probe_empty_when_no_tiles_parsed(monkeypatch):
    engine = _zara_engine(monkeypatch)

    fetch_mock = AsyncMock(return_value="<html></html>")
    monkeypatch.setattr("services.engines.zara_engine.BrowserManager.fetch_html", fetch_mock)
    monkeypatch.setattr("services.engines.zara_engine.parse_products", lambda html, **kwargs: [])

    candidate = SimpleNamespace(
        url="https://www.zara.com/br/pt/man-shoes-l715.html", path="man-shoes-l715.html"
    )
    result = await engine.category_has_products(candidate)

    assert result.status is ProbeStatus.EMPTY


@pytest.mark.asyncio
async def test_zara_probe_blocked_on_fetch_exception_never_empty(monkeypatch):
    engine = _zara_engine(monkeypatch)

    fetch_mock = AsyncMock(side_effect=RuntimeError("anti-bot interstitial"))
    monkeypatch.setattr("services.engines.zara_engine.BrowserManager.fetch_html", fetch_mock)

    candidate = SimpleNamespace(
        url="https://www.zara.com/br/pt/man-suits-l483.html", path="man-suits-l483.html"
    )
    result = await engine.category_has_products(candidate)

    assert result.status is ProbeStatus.BLOCKED


@pytest.mark.asyncio
async def test_zara_probe_akamai_access_denied_is_blocked_not_empty(monkeypatch):
    """Live-diagnostic regression (01-08): Playwright's page.goto does not raise
    on a non-2xx HTTP response, so Akamai's 'Access Denied' interstitial reaches
    parse_products as a normal 200-shaped fetch and (finding 0 tiles) was
    silently misclassified as EMPTY instead of BLOCKED. man-shoes-l769 is the
    real brand/category that surfaced this live."""
    engine = _zara_engine(monkeypatch)

    access_denied_html = (
        '<html><head>\n<title>Access Denied</title>\n</head><body>\n'
        '<h1>Access Denied</h1>\nYou don\'t have permission to access '
        '"http://www.zara.com/br/pt/man-shoes-l769.html?" on this server.<p>\n'
        'Reference #18.e67e1202.1783725995.9d7a037a\n</p>'
        '<p>https://errors.edgesuite.net/18.e67e1202.1783725995.9d7a037a</p>\n\n\n</body></html>'
    )
    fetch_mock = AsyncMock(return_value=access_denied_html)
    monkeypatch.setattr("services.engines.zara_engine.BrowserManager.fetch_html", fetch_mock)
    parse_mock = AsyncMock()
    monkeypatch.setattr("services.engines.zara_engine.parse_products", parse_mock)

    candidate = SimpleNamespace(
        url="https://www.zara.com/br/pt/man-shoes-l769.html", path="man-shoes-l769.html"
    )
    result = await engine.category_has_products(candidate)

    assert result.status is ProbeStatus.BLOCKED
    parse_mock.assert_not_called()


@pytest.mark.asyncio
async def test_zara_probe_render_budget_matches_discovery(monkeypatch):
    """The probe must no longer under-render relative to `_discover_and_validate`
    (timeout=45000, extra_sleep=2.0 in this same file, per D-07 gap-closure)."""
    engine = _zara_engine(monkeypatch)

    fetch_mock = AsyncMock(return_value="<html>tiles</html>")
    monkeypatch.setattr("services.engines.zara_engine.BrowserManager.fetch_html", fetch_mock)
    monkeypatch.setattr(
        "services.engines.zara_engine.parse_products",
        lambda html, **kwargs: [{"raw_title": "camisa"}],
    )

    candidate = SimpleNamespace(
        url="https://www.zara.com/br/pt/man-collection-l7421.html", path="man-collection-l7421.html"
    )
    await engine.category_has_products(candidate)

    assert fetch_mock.await_count == 1
    _, kwargs = fetch_mock.await_args
    assert kwargs.get("extra_sleep", 0) >= 2.0
    assert kwargs.get("timeout", 0) >= 45000


# ---------------------------------------------------------------------------
# Shopify: category_result_count — bounded retry + throttle, genuine empty/blocked preserved
# ---------------------------------------------------------------------------


class _FakeShopifyResponse:
    """Resposta fake compatível com `async with session.get(...) as resp`."""

    def __init__(self, status: int = 200, json_data=None):
        self.status = status
        self._json_data = json_data if json_data is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_data


class _FakeShopifySequenceSession:
    """Sessão fake: `.get()` devolve a próxima resposta de uma fila, em ordem."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if not self._responses:
            raise AssertionError("Sessão fake ficou sem respostas configuradas")
        return self._responses.pop(0)


def _shopify_client(monkeypatch, session):
    from services.shopify_api_client import ShopifyApiClient

    monkeypatch.setattr(
        "services.shopify_api_client.brand_service.get_brand", lambda k: None
    )
    client = ShopifyApiClient(brand_key="ricardoalmeida")
    monkeypatch.setattr(
        "services.shopify_api_client.SessionManager.get_session",
        AsyncMock(return_value=session),
    )
    return client


@pytest.mark.asyncio
async def test_shopify_probe_200_with_product_has_products(monkeypatch):
    session = _FakeShopifySequenceSession(
        [_FakeShopifyResponse(status=200, json_data={"products": [{"id": 1}]})]
    )
    client = _shopify_client(monkeypatch, session)

    count = await client.category_result_count("polos")

    assert count == 1


@pytest.mark.asyncio
async def test_shopify_probe_200_empty_products_list_is_genuine_empty(monkeypatch):
    session = _FakeShopifySequenceSession(
        [_FakeShopifyResponse(status=200, json_data={"products": []})]
    )
    client = _shopify_client(monkeypatch, session)

    count = await client.category_result_count("out-of-stock")

    assert count == 0


@pytest.mark.asyncio
async def test_shopify_probe_429_then_200_recovers_has_products(monkeypatch):
    session = _FakeShopifySequenceSession(
        [
            _FakeShopifyResponse(status=429),
            _FakeShopifyResponse(status=200, json_data={"products": [{"id": 1}]}),
        ]
    )
    client = _shopify_client(monkeypatch, session)

    count = await client.category_result_count("tenis")

    assert count == 1
    assert session.calls == 2


@pytest.mark.asyncio
async def test_shopify_probe_429_persistent_stays_blocked(monkeypatch):
    from services.shopify_api_client import _SHOPIFY_PROBE_MAX_RETRIES

    session = _FakeShopifySequenceSession(
        [_FakeShopifyResponse(status=429) for _ in range(_SHOPIFY_PROBE_MAX_RETRIES + 1)]
    )
    client = _shopify_client(monkeypatch, session)

    count = await client.category_result_count("sapatos")

    assert count is None
    assert session.calls == _SHOPIFY_PROBE_MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_shopify_probe_throttles_between_sequential_calls(monkeypatch):
    from services.shopify_api_client import _SHOPIFY_PROBE_THROTTLE_SECONDS

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    session = _FakeShopifySequenceSession(
        [
            _FakeShopifyResponse(status=200, json_data={"products": [{"id": 1}]}),
            _FakeShopifyResponse(status=200, json_data={"products": [{"id": 2}]}),
        ]
    )
    client = _shopify_client(monkeypatch, session)

    await client.category_result_count("polos")
    await client.category_result_count("tenis")

    throttle_calls = [
        call for call in sleep_mock.await_args_list
        if call.args and call.args[0] == _SHOPIFY_PROBE_THROTTLE_SECONDS
    ]
    assert len(throttle_calls) >= 2
