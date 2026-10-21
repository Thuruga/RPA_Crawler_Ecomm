"""
Phase 01-10 — fechamento de gap D-08 (theme-robust VTEX-IO tile detection +
render-wait sem `networkidle`).

Hermetic: nenhuma chamada de rede real.

Task 1 (D-08a, foxton false-empty-at-scale): `_parse_vtexio_tiles` seleciona
tiles via `_VTEXIO_TILE_SELECTOR_CSS` (união theme-robusta) em vez do antigo
`.vtex-product-summary-2-x-container` hardcoded contra o tema da Hugo Boss.

Task 2 (D-08b, hugoboss/hering render-timeout fragility): `_browser_fallback_products`
troca o wait `networkidle`/45s por um wait de dois tiers: Tier A (`domcontentloaded`
+ `wait_selector` no seletor theme-robusto, rápido) e Tier B (retry tolerante sem
`wait_selector`, preserva o invariante genuine-empty -> EMPTY, nunca BLOCKED).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.vtex_api_scraper import VtexApiClient, _VTEXIO_TILE_SELECTOR_CSS


def _client() -> VtexApiClient:
    return VtexApiClient(brand_name="testbrand")


# ---------------------------------------------------------------------------
# Task 1: _parse_vtexio_tiles theme-robust selector
# ---------------------------------------------------------------------------

_HUGO_BOSS_HTML = """
<html><body>
<div class="vtex-product-summary-2-x-container">
  <a href="https://www.hugoboss.com.br/camisa-slim-fit-azul/p" class="vtex-product-summary-2-x-clearLink">
    <div class="vtex-product-summary-2-x-imageContainer">
      <img src="https://hugoboss.vteximg.com.br/arquivos/ids/1/camisa.jpg" />
    </div>
    <span class="vtex-product-summary-2-x-productNameContainer">Camisa Slim Fit Azul</span>
    <div class="vtex-product-summary-2-x-sellingPriceValue">R$ 599,00</div>
  </a>
</div>
<div class="vtex-product-summary-2-x-container">
  <a href="https://www.hugoboss.com.br/blazer-slim/p" class="vtex-product-summary-2-x-clearLink">
    <div class="vtex-product-summary-2-x-imageContainer">
      <img src="https://hugoboss.vteximg.com.br/arquivos/ids/2/blazer.jpg" />
    </div>
    <span class="vtex-product-summary-2-x-productNameContainer">Blazer Slim</span>
    <div class="vtex-product-summary-2-x-sellingPriceValue">R$ 1 . 460 , 00</div>
  </a>
</div>
</body></html>
"""

# foxton's real live-confirmed theme (FastStore/Store Framework, Next.js CSS
# modules with a build-time hash suffix — 2026-07-11 live inspection).
_FOXTON_HTML = """
<html><body>
<div class="style_product-card--wrapper__eHBJf" data-size="medium">
  <div class="style_product-card--image__oTJqU">
    <a href="/camiseta-interlock-new-verde-oliva-7-07757-2119/p">
      <div class="style_product-card--image--container__GWM5o">
        <img src="/_next/image?url=https%3A%2F%2Ffoxton.vtexassets.com%2Farquivos%2Fids%2F639397%2Ffoto.jpg&w=640&q=90" />
      </div>
    </a>
  </div>
  <div class="style_product-card--body__VLYAZ">
    <div class="style_product-card--title__F51p4">
      <div class="style_product-card--title--content__X8yiA">
        <h3>Camiseta Interlock New</h3>
      </div>
    </div>
    <div class="style_product-card--price__r3P5N" data-mobile="false">
      <span class="styles-module_defaultPrice__Xj9b6">R$&nbsp;298,00</span>
      <span class="styles-module_installment__j6rjp">ou 2x R$&nbsp;149,00 s/ juros</span>
    </div>
  </div>
</div>
<div class="style_product-card--wrapper__eHBJf" data-size="medium">
  <div class="style_product-card--image__oTJqU">
    <a href="/camiseta-logo-camelo-7-04686-2216/p">
      <div class="style_product-card--image--container__GWM5o">
        <img src="/_next/image?url=https%3A%2F%2Ffoxton.vtexassets.com%2Farquivos%2Fids%2F999999%2Ffoto2.jpg&w=640&q=90" />
      </div>
    </a>
  </div>
  <div class="style_product-card--body__VLYAZ">
    <div class="style_product-card--title__F51p4">
      <div class="style_product-card--title--content__X8yiA">
        <h3>Camiseta Logo</h3>
      </div>
    </div>
    <div class="style_product-card--price__r3P5N" data-mobile="false">
      <span class="styles-module_defaultPrice__Xj9b6">R$&nbsp;199,00</span>
    </div>
  </div>
</div>
</body></html>
"""

_EMPTY_STATE_HTML = """
<html><body>
<div class="store-container">
  <p>Nenhum produto encontrado para esta categoria.</p>
</div>
</body></html>
"""

_INCOMPLETE_TILE_HTML = """
<html><body>
<div class="vtex-product-summary-2-x-container">
  <a href="https://www.hugoboss.com.br/produto-sem-preco/p">
    <div class="vtex-product-summary-2-x-imageContainer">
      <img src="https://hugoboss.vteximg.com.br/arquivos/ids/3/foto.jpg" />
    </div>
    <span class="vtex-product-summary-2-x-productNameContainer">Produto Sem Preco</span>
    <!-- sem no de preco -->
  </a>
</div>
<div class="vtex-product-summary-2-x-container">
  <a href="https://www.hugoboss.com.br/camisa-completa/p">
    <div class="vtex-product-summary-2-x-imageContainer">
      <img src="https://hugoboss.vteximg.com.br/arquivos/ids/4/foto.jpg" />
    </div>
    <span class="vtex-product-summary-2-x-productNameContainer">Camisa Completa</span>
    <div class="vtex-product-summary-2-x-sellingPriceValue">R$ 399,00</div>
  </a>
</div>
</body></html>
"""


def test_parse_vtexio_tiles_hugo_boss_regression():
    """The old hardcoded Hugo Boss theme is a subset of the new union — zero regression."""
    client = _client()
    products = client._parse_vtexio_tiles(_HUGO_BOSS_HTML, "www.hugoboss.com.br")

    assert len(products) == 2
    titles = {p.raw_title for p in products}
    assert titles == {"Camisa Slim Fit Azul", "Blazer Slim"}
    prices = {p.price_full for p in products}
    assert prices == {599.0, 1460.0}


def test_parse_vtexio_tiles_foxton_new_theme_parses():
    """foxton's real live-confirmed theme (not `.vtex-product-summary-2-x-container`)
    now parses via the theme-robust selector union — this is the D-08a fix."""
    client = _client()
    products = client._parse_vtexio_tiles(_FOXTON_HTML, "www.foxtonbrasil.com.br")

    assert len(products) == 2
    titles = {p.raw_title for p in products}
    assert titles == {"Camiseta Interlock New", "Camiseta Logo"}
    prices = {p.price_full for p in products}
    assert prices == {298.0, 199.0}


def test_parse_vtexio_tiles_genuinely_empty_yields_zero():
    """A page with no product tiles at all yields exactly 0 — no false positives
    from an over-broad selector."""
    client = _client()
    products = client._parse_vtexio_tiles(_EMPTY_STATE_HTML, "www.foxtonbrasil.com.br")

    assert products == []


def test_parse_vtexio_tiles_incomplete_tile_skipped_not_crashed():
    """A tile missing price (or image) is skipped, not crashed on — one incomplete
    tile never aborts the whole category (existing invariant, preserved)."""
    client = _client()
    products = client._parse_vtexio_tiles(_INCOMPLETE_TILE_HTML, "www.hugoboss.com.br")

    assert len(products) == 1
    assert products[0].raw_title == "Camisa Completa"


def test_vtexio_tile_selector_css_is_pure_css_no_jsonld():
    """Grep-confirmable acceptance criterion: the constant is a valid CSS union,
    no JSON-LD/script logic embedded in it."""
    assert "script" not in _VTEXIO_TILE_SELECTOR_CSS.lower()
    assert "application/ld+json" not in _VTEXIO_TILE_SELECTOR_CSS
    assert '[class*="vtex-product-summary"][class*="container"]' in _VTEXIO_TILE_SELECTOR_CSS


# ---------------------------------------------------------------------------
# Task 2: _browser_fallback_products two-tier render wait (no all-network-quiet)
# ---------------------------------------------------------------------------

_ROOT_QUERY_EMPTY_HTML = "<html><body><div>sem ROOT_QUERY aqui</div></body></html>"


@pytest.mark.asyncio
async def test_browser_fallback_tier_a_fast_happy_path(monkeypatch):
    """Stage-2 Tier A (domcontentloaded + wait_selector) succeeds -> tiles parse,
    and `fetch_html` is NEVER called with an all-network-quiet wait strategy."""
    client = _client()

    fetch_mock = AsyncMock(side_effect=[_ROOT_QUERY_EMPTY_HTML, _FOXTON_HTML])
    monkeypatch.setattr("services.vtex_api_scraper.browser_manager.fetch_html", fetch_mock)

    products = await client._browser_fallback_products(
        "https://www.foxtonbrasil.com.br/roupas/camisetas/camiseta-algodao",
        "www.foxtonbrasil.com.br",
    )

    assert len(products) == 2
    assert fetch_mock.await_count == 2

    # Stage 1 call: no wait_until override implied ("domcontentloaded" default OK)
    # Stage 2 (Tier A) call: must use domcontentloaded + wait_selector, never networkidle.
    tier_a_call = fetch_mock.await_args_list[1]
    assert tier_a_call.kwargs.get("wait_until") == "domcontentloaded"
    assert tier_a_call.kwargs.get("wait_selector") == _VTEXIO_TILE_SELECTOR_CSS

    for c in fetch_mock.await_args_list:
        assert c.kwargs.get("wait_until") != "networkidle"


@pytest.mark.asyncio
async def test_browser_fallback_stage1_failure_falls_through_to_tier_a(monkeypatch):
    """D-08e (Plano 01-12) regression guard: stage 1 (ROOT_QUERY fetch) raising
    (e.g. a slow/heavy page timing out on the default 30s fetch) must NOT abort
    the whole fallback — it must fall through to Tier A/B exactly like an
    empty ROOT_QUERY result already does. Reproduces reserva's real finding:
    Playwright took 45s to reach domcontentloaded on a heavy category page
    (2.5MB+ HTML), well past stage 1's timeout, but Tier A succeeded fine."""
    client = _client()

    fetch_mock = AsyncMock(side_effect=[TimeoutError("stage1 root_query timeout"), _FOXTON_HTML])
    monkeypatch.setattr("services.vtex_api_scraper.browser_manager.fetch_html", fetch_mock)

    products = await client._browser_fallback_products(
        "https://www.usereserva.com/reserva/masculino/leggings/promocao",
        "www.usereserva.com",
    )

    assert len(products) == 2
    assert fetch_mock.await_count == 2


@pytest.mark.asyncio
async def test_browser_fallback_genuine_empty_stays_empty_not_blocked(monkeypatch):
    """Critical zero-regression invariant: Tier A's wait_selector times out (no
    tiles ever appear), the tolerant Tier B retry renders fine but empty ->
    `_parse_vtexio_tiles` -> [] -> `category_result_count_with_tiles_fallback`
    returns 0 (EMPTY), never None (BLOCKED)."""
    client = _client()

    fetch_mock = AsyncMock(side_effect=[_ROOT_QUERY_EMPTY_HTML, TimeoutError("tier a timeout"), _EMPTY_STATE_HTML])
    monkeypatch.setattr("services.vtex_api_scraper.browser_manager.fetch_html", fetch_mock)

    count = await client.category_result_count_with_tiles_fallback(
        "https://www.foxtonbrasil.com.br/roupas/vazia", "/roupas/vazia", "www.foxtonbrasil.com.br"
    )

    assert count == 0


@pytest.mark.asyncio
async def test_browser_fallback_both_tiers_fail_is_blocked(monkeypatch):
    """Genuine block/error invariant: BOTH Tier A and Tier B raise -> the
    exception propagates out of `_browser_fallback_products` ->
    `category_result_count_with_tiles_fallback` returns None (BLOCKED).
    No false-empty."""
    client = _client()

    fetch_mock = AsyncMock(
        side_effect=[_ROOT_QUERY_EMPTY_HTML, TimeoutError("tier a timeout"), TimeoutError("tier b timeout")]
    )
    monkeypatch.setattr("services.vtex_api_scraper.browser_manager.fetch_html", fetch_mock)

    count = await client.category_result_count_with_tiles_fallback(
        "https://www.hugoboss.com.br/categoria-bloqueada", "/categoria-bloqueada", "www.hugoboss.com.br"
    )

    assert count is None
