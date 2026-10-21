"""
Fase 06 Plano 01 — classificacao de estado de reviews (D-11).

Hermetic: nenhuma chamada de rede real. Monkeypatch de `aiohttp.ClientSession`
dentro de `services.review_service` com uma sessao/resposta fake, seguindo o
mesmo estilo de `test_vtex_probe_reliability.py`.

Cobre:
  - 403/429 -> "blocked" (bloqueio transitorio de anti-bot, nunca permanente)
  - 404/401 -> "unsupported" (endpoint/produto ausente ou credential-gated, A1)
  - outros HTTP nao-200 e excecao de transporte -> "error"
  - resumo (rating/count) valido com lista de comentarios vazia -> "available"
    com o review_count correto (regressao Pitfall 1 para VTEX-native, e guarda
    de nao-regressao para Trustvox)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from services import review_service


class _FakeResponse:
    """Resposta fake compativel com `async with session.get(...) as resp:`."""

    def __init__(
        self,
        status: int = 200,
        json_data: Any = None,
        text_data: str = "",
        headers: dict | None = None,
    ):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self._json_data

    async def text(self):
        if self._text_data:
            return self._text_data
        if self._json_data is not None:
            import json
            return json.dumps(self._json_data)
        return ""


class _FakeSession:
    """Sessao fake: `.get()` devolve a proxima resposta de uma fila (ignora URL)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.calls += 1
        if not self._responses:
            raise AssertionError("Sessao fake ficou sem respostas configuradas")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_session(monkeypatch, responses) -> _FakeSession:
    fake_session = _FakeSession(responses)
    monkeypatch.setattr(
        review_service.aiohttp, "ClientSession", lambda *a, **kw: fake_session
    )
    return fake_session


TRUSTVOX_BRAND = SimpleNamespace(review_store_id="store-1", domain="loja.com.br")
VTEX_BRAND = SimpleNamespace(review_store_id=None, domain="loja.com.br")


# ---------------------------------------------------------------------------
# Trustvox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trustvox_403_is_blocked(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=403)])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_trustvox_429_is_blocked(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=429)])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_trustvox_404_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=404)])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_trustvox_400_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=400, json_data={"error": "O produto solicitado nao pode ser encontrado"})])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_trustvox_401_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=401)])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_trustvox_500_is_error(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=500)])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_trustvox_transport_exception_is_error(monkeypatch):
    _patch_session(monkeypatch, [ConnectionError("boom")])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_trustvox_empty_summary_is_available_zero_count(monkeypatch):
    """Guarda de nao-regressao: resumo com 0 avaliacoes e sem entradas continua
    'available' com review_count 0 (nunca 'unsupported')."""
    root_response = _FakeResponse(
        status=200, json_data={"rate": {"average": 0, "count": 0}}
    )
    opinions_response = _FakeResponse(status=200, json_data=[])
    _patch_session(monkeypatch, [root_response, opinions_response])

    result = await review_service._fetch_trustvox_comments(TRUSTVOX_BRAND, "p1", 1)

    assert result.reviews_state == "available"
    assert result.review_count == 0


# ---------------------------------------------------------------------------
# VTEX Native
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vtex_native_403_is_blocked(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=403)])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_vtex_native_429_is_blocked(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=429)])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_vtex_native_404_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=404)])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_vtex_native_400_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=400, json_data={"error": "invalid product"})])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_vtex_native_401_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=401)])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_vtex_native_500_is_error(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=500)])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_vtex_native_transport_exception_is_error(monkeypatch):
    _patch_session(monkeypatch, [ConnectionError("boom")])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_vtex_native_summary_with_empty_entries_is_available_with_count(
    monkeypatch,
):
    """Pitfall 1 regression guard: resumo parseado + lista de comentarios vazia
    deve retornar 'available' com o review_count parseado, nunca 'unsupported'."""
    payload = {"average": 4.5, "totalCount": 7, "reviews": []}
    _patch_session(monkeypatch, [_FakeResponse(status=200, json_data=payload)])

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 1)

    assert result.reviews_state == "available"
    assert result.review_count == 7


@pytest.mark.asyncio
async def test_vtex_native_pagination_end_preserves_prior_page_comments(monkeypatch):
    """Code review regression guard: uma pagina > 1 sem mais entradas deve encerrar
    a paginacao (break) e devolver os comentarios ja acumulados, nunca um
    `comments=[]` que descarta silenciosamente o que ja foi lido nas paginas
    anteriores (bug real: o guard `if not entries` do Pitfall 1 disparava em
    qualquer pagina vazia, nao so na primeira)."""
    page_1 = {
        "average": 4.5,
        "totalCount": 1,
        "reviews": [{"rating": 5, "text": "Otimo produto"}],
    }
    page_2 = {"average": 4.5, "totalCount": 1, "reviews": []}
    _patch_session(
        monkeypatch,
        [
            _FakeResponse(status=200, json_data=page_1),
            _FakeResponse(status=200, json_data=page_2),
        ],
    )

    result = await review_service._fetch_vtex_native_comments(VTEX_BRAND, "p1", 2)

    assert result.reviews_state == "available"
    assert len(result.comments) == 1
    assert result.review_count == 1


# ---------------------------------------------------------------------------
# Vurdere AI (Richards)
# ---------------------------------------------------------------------------


class _FakeCurlResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


class _FakeCurlSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.calls += 1
        if not self._responses:
            raise AssertionError("Sessao fake curl ficou sem respostas configuradas")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_curl_session(monkeypatch, responses) -> _FakeCurlSession:
    import curl_cffi.requests
    fake_session = _FakeCurlSession(responses)
    monkeypatch.setattr(
        curl_cffi.requests, "AsyncSession", lambda *a, **kw: fake_session
    )
    return fake_session


VURDERE_BRAND = SimpleNamespace(review_store_id="richards", domain="richards.com.br")


@pytest.mark.asyncio
async def test_vurdere_403_is_blocked(monkeypatch):
    _patch_curl_session(monkeypatch, [_FakeCurlResponse(status_code=403)])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_vurdere_429_is_blocked(monkeypatch):
    _patch_curl_session(monkeypatch, [_FakeCurlResponse(status_code=429)])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_vurdere_404_is_unsupported(monkeypatch):
    _patch_curl_session(monkeypatch, [_FakeCurlResponse(status_code=404)])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_vurdere_500_is_error(monkeypatch):
    _patch_curl_session(monkeypatch, [_FakeCurlResponse(status_code=500)])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_vurdere_transport_exception_is_error(monkeypatch):
    _patch_curl_session(monkeypatch, [ConnectionError("timeout")])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_vurdere_summary_with_empty_entries_is_available_with_count(monkeypatch):
    payload = {
        "aggregateRating": {"ratingValue": 4.8, "reviewCount": 10},
        "items": [],
    }
    _patch_curl_session(monkeypatch, [_FakeCurlResponse(status_code=200, json_data=payload)])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "available"
    assert result.rating == 4.8
    assert result.review_count == 10
    assert result.comments == []


@pytest.mark.asyncio
async def test_vurdere_comments_parsing(monkeypatch):
    payload = {
        "aggregateRating": {"ratingValue": 5.0, "reviewCount": 1},
        "items": [
            {
                "hashId": "hash-999",
                "author": "uuid-1234",
                "authorInfo": {"name": "Cliente VIP"},
                "location": {"city": "Rio de Janeiro"},
                "reviewRating": 5,
                "reviewBody": "Camisa de linho impecavel.",
                "datePublished": "15/08/2025",
            }
        ],
    }
    _patch_curl_session(monkeypatch, [_FakeCurlResponse(status_code=200, json_data=payload)])

    result = await review_service._fetch_vurdere_comments(VURDERE_BRAND, "p1", 1)

    assert result.reviews_state == "available"
    assert result.rating == 5.0
    assert len(result.comments) == 1
    comment = result.comments[0]
    assert comment.review_id == "hash-999"
    assert comment.author == "Cliente VIP (Rio de Janeiro)"
    assert comment.rating == 5.0
    assert comment.text == "Camisa de linho impecavel."
    assert comment.created_at == "15/08/2025"
    assert comment.source_provider == "vurdere"


# ---------------------------------------------------------------------------
# YourViews (Levi's)
# ---------------------------------------------------------------------------

YOURVIEWS_BRAND = SimpleNamespace(review_store_id="store-yv-1", domain="www.levi.com.br")


@pytest.mark.asyncio
async def test_yourviews_403_is_blocked(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=403)])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_yourviews_429_is_blocked(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=429)])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "blocked"


@pytest.mark.asyncio
async def test_yourviews_404_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=404)])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_yourviews_400_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=400)])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_yourviews_500_is_error(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=500)])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_yourviews_transport_exception_is_error(monkeypatch):
    _patch_session(monkeypatch, [ConnectionError("timeout")])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "error"


@pytest.mark.asyncio
async def test_yourviews_has_results_false_is_unsupported(monkeypatch):
    _patch_session(monkeypatch, [_FakeResponse(status=200, json_data={"hasResults": False, "html": ""})])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "unsupported"


@pytest.mark.asyncio
async def test_yourviews_comments_parsing(monkeypatch):
    sample_html = """
    <div class="yv-main">
      <div itemprop="aggregateRating" itemscope itemtype="http://schema.org/AggregateRating">
        <meta itemprop="ratingValue" content="4.5" />
        <strong itemprop="ratingCount">12</strong>
      </div>
      <div itemprop="review" itemscope itemtype="http://schema.org/Review" name="yv-rev-101">
        <strong itemprop="author">Mariana S.</strong>
        <div itemprop="reviewRating" itemscope itemtype="http://schema.org/Rating">
          <meta itemprop="ratingValue" content="5" />
        </div>
        <meta itemprop="datePublished" content="2026-10-18T10:00:00" />
        <strong itemprop="name">Excelente caimento</strong>
        <p itemprop="description">A calca veste muito bem, tecido de altissima qualidade.</p>
      </div>
    </div>
    """
    _patch_session(monkeypatch, [_FakeResponse(status=200, json_data={"hasResults": True, "html": sample_html})])

    result = await review_service._fetch_yourviews_comments(YOURVIEWS_BRAND, "p1", 1)

    assert result.reviews_state == "available"
    assert result.rating == 4.5
    assert result.review_count == 12
    assert len(result.comments) == 1
    c = result.comments[0]
    assert c.review_id == "yv-rev-101"
    assert c.author == "Mariana S."
    assert c.rating == 5.0
    assert c.title == "Excelente caimento"
    assert c.text == "A calca veste muito bem, tecido de altissima qualidade."
    assert c.created_at == "2026-10-18T10:00:00"
    assert c.source_provider == "yourviews"


