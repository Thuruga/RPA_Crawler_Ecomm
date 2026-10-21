"""Contrato conservador para o fallback de cor e tamanho."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.attribute_parsing import extract_color_fallback, extract_size_fallback
from services.engines import wake_engine as wake_module
from services.engines.sfcc_parser import parse_pdp, parse_search_tiles
from services.engines.wake_engine import WakeEngine


PHASE01_FIXTURES = Path(__file__).parent / "fixtures" / "phase01"


@pytest.mark.parametrize(
    ("value", "expected_color", "expected_size"),
    [
        ("VERDE / P", "VERDE", "P"),
        ("Azul|GG", "Azul", "GG"),
        ("Preto - 42", "Preto", "42"),
        ("Off White/UN", "Off White", "UN"),
        ("Café / XGG", "Café", "XGG"),
    ],
)
def test_safe_terminal_sizes_are_separated(
    value: str,
    expected_color: str,
    expected_size: str,
) -> None:
    assert extract_color_fallback(value) == expected_color
    assert extract_size_fallback(value) == expected_size


@pytest.mark.parametrize(
    "value",
    [
        "Azul Marinho",
        "Azul-Marinho",
        "P Azul",
        "Azul / Inverno",
        "Edição São João",
        "Modelo G Especial",
    ],
)
def test_clean_and_ambiguous_values_remain_identical(value: str) -> None:
    assert extract_color_fallback(value) == value
    assert extract_size_fallback(value) == value


@pytest.mark.parametrize("value", ["", None])
def test_empty_values_follow_the_existing_normalization_contract(value: object) -> None:
    assert extract_color_fallback(value) == ""  # type: ignore[arg-type]
    assert extract_size_fallback(value) == ""  # type: ignore[arg-type]


def test_fallback_does_not_mutate_callers_raw_values() -> None:
    raw_values = ["VERDE / P", "Azul-Marinho", "Café|M"]
    original = list(raw_values)

    colors = [extract_color_fallback(value) for value in raw_values]
    sizes = [extract_size_fallback(value) for value in raw_values]

    assert colors == ["VERDE", "Azul-Marinho", "Café"]
    assert sizes == ["P", "Azul-Marinho", "M"]
    assert raw_values == original


@pytest.mark.asyncio
async def test_wake_structured_html_attributes_preserve_raw_before_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <input id="257__VERDE / P__">
    <input id="258__VERDE / P__">
    <input id="257__Azul-Marinho__">
    <input id="258__M__">
    """

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def text(self) -> str:
            return html

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

    engine = WakeEngine("unregistered_test_brand_wake")
    product = SimpleNamespace(
        model_dump=lambda: {
            "product_name": "Camisa Wake",
            "url": "https://www.teststore.com.br/produto/camisa-123",
            "price_full": 199.9,
        }
    )

    async def fake_search(_term: str, max_results: int = 1):
        assert max_results == 1
        return SimpleNamespace(error=None, products=[product])

    monkeypatch.setattr(engine, "search", fake_search)
    monkeypatch.setattr(wake_module.aiohttp, "ClientSession", FakeSession)

    result = await engine.get_product_details(
        "https://www.teststore.com.br/produto/camisa-123"
    )

    assert result is not None
    assert result["raw_colors"] == ["VERDE / P", "Azul-Marinho"]
    assert result["available_colors"] == ["Verde", "Azul-marinho"]
    assert result["raw_sizes"] == ["VERDE / P", "M"]
    assert result["available_sizes"] == ["P", "M"]
    assert result["raw_size_grid"] == ["VERDE / P", "M"]
    assert result["size_grid"] == ["P", "M"]


def test_sfcc_pdp_normalizes_only_explicit_jsonld_attributes() -> None:
    product = {
        "@type": "ProductGroup",
        "name": "Camisa SFCC",
        "productID": "PID-SFCC-MIXED",
        "color": ["VERDE / P", "Azul-Marinho"],
        "size": ["VERDE / P", "M"],
        "offers": {"price": 299.9, "availability": "https://schema.org/InStock"},
    }
    html = '<script type="application/ld+json">' + json.dumps(product) + "</script>"

    result = parse_pdp(html, "https://www.teststore.com.br/camisa-sfcc.html")

    assert result is not None
    assert result["raw_colors"] == ["VERDE / P", "Azul-Marinho"]
    assert result["available_colors"] == ["Verde", "Azul-marinho"]
    assert result["raw_sizes"] == ["VERDE / P", "M"]
    assert result["available_sizes"] == ["P", "M"]


def test_sfcc_search_tile_normalizes_only_explicit_data_attributes() -> None:
    html = """
    <div data-producttile="true" data-pid="PID-SFCC-MIXED"
         data-color="VERDE / P" data-size="VERDE / P">
      <h2><a href="/camisa-sfcc.html">Camisa SFCC</a></h2>
      <span class="sales-price">R$ 299,90</span>
    </div>
    """

    [result] = parse_search_tiles(html, "teststore.com.br", "TestBrand")

    assert result["raw_colors"] == ["VERDE / P"]
    assert result["available_colors"] == ["Verde"]
    assert result["raw_sizes"] == ["VERDE / P"]
    assert result["available_sizes"] == ["P"]


def test_sfcc_fixture_without_explicit_attributes_remains_empty() -> None:
    html = (PHASE01_FIXTURES / "sfcc.html").read_text(encoding="utf-8")

    pdp = parse_pdp(html, "https://www.teststore.com.br/camisa-sfcc.html")
    [tile] = parse_search_tiles(html, "teststore.com.br", "TestBrand")

    assert pdp is not None
    for result in (pdp, tile):
        assert result["raw_colors"] == []
        assert result["available_colors"] == []
        assert result["raw_sizes"] == []
        assert result["available_sizes"] == []
