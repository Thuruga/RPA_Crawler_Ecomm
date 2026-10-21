"""
Phase 01-07 — matriz de confiabilidade do probe VTEX (D-07).

Hermetic: nenhuma chamada de rede real. Mocka a sessão aiohttp / um objeto
de resposta fake para exercitar `_legacy_search_count` /
`category_result_count_with_tiles_fallback` (vtex_api_scraper.py) e
`_probe_category` (vtex_engine.py) contra a matriz de classificação
403/429/404/0/N + timeout por candidato, além do fix WR-02 de reuso de
sessão em `search()`.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.vtex_api_scraper import VtexApiClient


class _FakeResponse:
    """Resposta fake compatível com o uso de `async with session.get(...) as resp`."""

    def __init__(self, status: int = 200, headers: dict | None = None, json_data=None,
                 json_exc: Exception | None = None):
        self.status = status
        self.headers = headers or {}
        self._json_data = json_data
        self._json_exc = json_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._json_data


class _FakeSequenceSession:
    """Sessão fake: `.get()` devolve a próxima resposta de uma fila por URL (ou geral)."""

    def __init__(self, responses):
        # responses: lista de _FakeResponse, consumida em ordem a cada .get()
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if not self._responses:
            raise AssertionError("Sessão fake ficou sem respostas configuradas")
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Nunca dorme de verdade durante o backoff nos testes."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


def _client_with_session(session) -> VtexApiClient:
    client = VtexApiClient(brand_name="testbrand")
    client.session = session
    return client


# ---------------------------------------------------------------------------
# _legacy_search_count / category_result_count_with_tiles_fallback matrix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_count_200_with_resources_header_has_products():
    session = _FakeSequenceSession([
        _FakeResponse(status=200, headers={"resources": "0/47"}),
    ])
    client = _client_with_session(session)

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    assert count == 47


@pytest.mark.asyncio
async def test_legacy_count_zero_triggers_tiles_fallback_has_products(monkeypatch):
    session = _FakeSequenceSession([
        _FakeResponse(status=200, headers={"resources": "0/0"}),
    ])
    client = _client_with_session(session)
    monkeypatch.setattr(
        client, "_browser_fallback_products", AsyncMock(return_value=[object(), object()])
    )

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    assert count == 2


@pytest.mark.asyncio
async def test_legacy_count_zero_triggers_tiles_fallback_empty(monkeypatch):
    session = _FakeSequenceSession([
        _FakeResponse(status=200, headers={"resources": "0/0"}),
    ])
    client = _client_with_session(session)
    monkeypatch.setattr(client, "_browser_fallback_products", AsyncMock(return_value=[]))

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    assert count == 0


@pytest.mark.asyncio
async def test_legacy_count_404_api_unavailable_reaches_tiles_fallback(monkeypatch):
    """Regression guard for foxton: VTEX-IO 404 must reach the tiles fallback,
    never an immediate BLOCKED."""
    session = _FakeSequenceSession([
        _FakeResponse(status=404),
    ])
    client = _client_with_session(session)
    fallback = AsyncMock(return_value=[object()])
    monkeypatch.setattr(client, "_browser_fallback_products", fallback)

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    fallback.assert_awaited_once()
    assert count == 1


@pytest.mark.asyncio
async def test_legacy_count_200_non_json_body_is_api_unavailable_reaches_fallback(monkeypatch):
    """foxton's real observed failure: HTTP 200, no 'resources' header, non-JSON
    body (aiohttp.ContentTypeError) — must be api_unavailable, not hard_blocked."""
    import aiohttp

    session = _FakeSequenceSession([
        _FakeResponse(status=200, headers={}, json_exc=aiohttp.ContentTypeError(
            request_info=SimpleNamespace(real_url="https://loja.com/cat"), history=()
        )),
    ])
    client = _client_with_session(session)
    fallback = AsyncMock(return_value=[object(), object(), object()])
    monkeypatch.setattr(client, "_browser_fallback_products", fallback)

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    fallback.assert_awaited_once()
    assert count == 3


@pytest.mark.asyncio
async def test_legacy_count_403_hard_blocked_never_retried_never_fallback(monkeypatch):
    session = _FakeSequenceSession([
        _FakeResponse(status=403),
    ])
    client = _client_with_session(session)
    fallback = AsyncMock(return_value=[object()])
    monkeypatch.setattr(client, "_browser_fallback_products", fallback)

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    assert count is None
    fallback.assert_not_awaited()
    assert session.calls == 1  # 403 nunca é re-tentado


@pytest.mark.asyncio
async def test_tiles_fallback_ignores_tree_url_uses_registered_domain(monkeypatch):
    """Regression guard for foxton (Plano 01-11, D-08c): a árvore oficial da VTEX
    pode devolver uma URL absoluta usando o domínio genérico da conta (ex.
    `foxton.vtexcommercestable.com.br`), que não necessariamente serve a mesma
    loja/tema do domínio de produção cadastrado. O fallback de tiles deve
    SEMPRE navegar para `https://{domain}{path}` (o domínio confiável
    cadastrado da marca), nunca para a URL bruta da árvore."""
    session = _FakeSequenceSession([
        _FakeResponse(status=200, headers={"resources": "0/0"}),
    ])
    client = _client_with_session(session)
    fallback = AsyncMock(return_value=[object(), object()])
    monkeypatch.setattr(client, "_browser_fallback_products", fallback)

    mismatched_tree_url = "https://foxton.vtexcommercestable.com.br/roupas/camisetas/camiseta-algodao"
    count = await client.category_result_count_with_tiles_fallback(
        mismatched_tree_url, "/roupas/camisetas/camiseta-algodao", "www.foxtonbrasil.com.br"
    )

    fallback.assert_awaited_once_with(
        "https://www.foxtonbrasil.com.br/roupas/camisetas/camiseta-algodao",
        "www.foxtonbrasil.com.br",
    )
    assert count == 2


@pytest.mark.asyncio
async def test_legacy_count_429_retried_still_blocked_after_max_retries(monkeypatch):
    from services.vtex_api_scraper import _PROBE_MAX_RETRIES

    session = _FakeSequenceSession([
        _FakeResponse(status=429) for _ in range(_PROBE_MAX_RETRIES + 1)
    ])
    client = _client_with_session(session)
    fallback = AsyncMock(return_value=[object()])
    monkeypatch.setattr(client, "_browser_fallback_products", fallback)

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    assert count is None
    fallback.assert_not_awaited()  # bloqueio confirmado nunca aciona o fallback caro
    assert session.calls == _PROBE_MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_legacy_count_429_then_200_recovers_has_products():
    session = _FakeSequenceSession([
        _FakeResponse(status=429),
        _FakeResponse(status=200, headers={"resources": "0/12"}),
    ])
    client = _client_with_session(session)

    count = await client.category_result_count_with_tiles_fallback(
        "https://loja.com/cat", "/cat", "loja.com"
    )

    assert count == 12
    assert session.calls == 2


# ---------------------------------------------------------------------------
# Per-candidate timeout (vtex_engine._probe_category)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_category_timeout_returns_blocked_not_raise(monkeypatch):
    from services.category_discovery import ProbeStatus
    from services.engines.vtex_engine import VTEXEngine

    monkeypatch.setattr(
        "services.engines.vtex_engine.PER_CANDIDATE_PROBE_TIMEOUT_SECONDS", 0.01
    )

    async def _hangs_forever(*args, **kwargs):
        # Bloqueia de verdade via primitiva asyncio (nunca resolve) — não usa
        # asyncio.sleep, que a fixture `_no_real_sleep` substitui por um mock
        # instantâneo para os testes de backoff.
        await asyncio.Event().wait()
        return 1

    fake_client = SimpleNamespace(
        category_result_count_with_tiles_fallback=_hangs_forever
    )
    candidate = SimpleNamespace(url="https://loja.com/cat", path="/cat")

    result = await VTEXEngine._probe_category(fake_client, candidate, "loja.com")

    assert result.status is ProbeStatus.BLOCKED
    assert "timeout" in result.detail.lower()


@pytest.mark.asyncio
async def test_probe_category_still_classifies_has_products_when_fast(monkeypatch):
    from services.category_discovery import ProbeStatus
    from services.engines.vtex_engine import VTEXEngine

    async def _fast_count(*args, **kwargs):
        return 5

    fake_client = SimpleNamespace(category_result_count_with_tiles_fallback=_fast_count)
    candidate = SimpleNamespace(url="https://loja.com/cat", path="/cat")

    result = await VTEXEngine._probe_category(fake_client, candidate, "loja.com")

    assert result.status is ProbeStatus.HAS_PRODUCTS


# ---------------------------------------------------------------------------
# WR-02: search() must never leave an injected session closed/replaced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_reuses_injected_session_wr02(monkeypatch):
    from core.models import BrandSearchResult

    fake_brand = SimpleNamespace(brand_name="Test Brand", domain="loja.com", mappings=[])
    monkeypatch.setattr(
        "services.vtex_api_scraper.brand_service.get_brand", lambda k: fake_brand
    )

    injected_session = _FakeSequenceSession([
        _FakeResponse(status=200, json_data=[]),
    ])
    client = VtexApiClient(brand_name="testbrand", session=injected_session)

    result = await client.search(query="camisa", max_results=5)

    assert client.session is injected_session
    assert isinstance(result, BrandSearchResult)
