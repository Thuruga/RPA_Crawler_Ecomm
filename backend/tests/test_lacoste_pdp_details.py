"""Tests for Lacoste PDP details: description, colors, sizes, and composition extraction."""
import json
from unittest.mock import AsyncMock, patch
import pytest
from services.engines.algolia_engine import AlgoliaEngine
from services.engines.factory import engine_factory
from services.engines.sfcc_parser import parse_pdp


_SAMPLE_ALGO_HIT = {
    "objectId": "PH4014-23-031_lacoste-br",
    "productId": "PH4014-23-031",
    "name": "Camisa Polo Lacoste Slim Fit Masculina em Petit piquet Stretch",
    "categoryLabel": "POLOS",
    "categoryName": "SPW/T/MAN/POLOS",
    "genderLabel": ["Masculino"],
    "description": (
        "A camisa polo Slim Fit é a escolha ideal para homens que buscam vestir-se "
        "com elegância e conforto. Com 94% de algodão e 6% de elastano, a Camisa Polo "
        "Slim Fit Masculina em Tecido Petit Piquet é a opção ideal."
    ),
    "bulletpoint1": "Carcela com 3 botões",
    "sleeve": "Mangas curtas",
    "fitEcom": "Slim fit",
    "season": "23E",
    "colorId": "031",
    "colorLabel": "Preto",
    "colors": [
        {"code": "031", "label": "Preto"},
        {"code": "166", "label": "Azul Marinho"},
        {"code": "001", "label": "Branco"},
    ],
    "sizes": ["03-2 - XS", "04-3 - S", "05-4 - M", "06-5 - L"],
    "urlMaster": "/br/lacoste/masculino/vestuario/polos/PH4014-23.html?color=031",
    "imagesHttps": {
        "details": "//image1.lacoste.com/dw/image/v2/AAQM_PRD/on/demandware.static/PH4014_031_20.jpg",
        "flat": "//image1.lacoste.com/dw/image/v2/AAQM_PRD/on/demandware.static/PH4014_031_24.jpg",
    },
    "price": {"min": 699.0, "max": 699.0},
}


def test_lacoste_engine_factory_resolution():
    """EngineFactory must always resolve Lacoste to AlgoliaEngine."""
    engine = engine_factory.get_engine("lacoste")
    assert isinstance(engine, AlgoliaEngine)
    assert engine.algolia_app_id == "CU0IYSHI42"
    assert engine.algolia_origin == "https://www.lacoste.com"


def test_algolia_engine_fallback_credentials():
    """AlgoliaEngine should fall back to valid Lacoste BR credentials even with empty config."""
    brand_dict = {"brand_key": "lacoste", "domain": "lacoste.com.br", "configuration": {}}
    engine = AlgoliaEngine(brand_dict)
    assert engine.algolia_app_id == "CU0IYSHI42"
    assert engine.algolia_api_key == "7e20c005d05fe8f113d8b7e079bf34a1"
    assert engine.algolia_index == "products_br_pt"
    assert engine.algolia_origin == "https://www.lacoste.com"


def test_algolia_hit_composition_and_specs_extraction():
    """Algolia hit parser extracts full composition (not truncated at 'de') and specs."""
    brand_dict = {"brand_key": "lacoste", "domain": "lacoste.com.br"}
    engine = AlgoliaEngine(brand_dict)
    parsed = engine._parse_algolia_hit(_SAMPLE_ALGO_HIT)

    assert parsed["raw_title"] == "Camisa Polo Lacoste Slim Fit Masculina em Petit piquet Stretch"
    assert parsed["composition"] == "94% de algodão e 6% de elastano"
    assert parsed["available_colors"] == ["Preto", "Azul Marinho", "Branco"]
    assert parsed["available_sizes"] == ["03-2 - XS", "04-3 - S", "05-4 - M", "06-5 - L"]
    assert parsed["specifications"] == {
        "Detalhe": "Carcela com 3 botões",
        "Manga": "Mangas curtas",
        "Modelagem": "Slim fit",
        "Coleção": "23E",
    }
    assert "A camisa polo Slim Fit é a escolha ideal" in parsed["raw_description"]


@pytest.mark.asyncio
async def test_algolia_get_pdp_product_with_color_variant():
    """AlgoliaEngine.get_pdp_product prioritizes color-specific queries when color param is present."""
    engine = AlgoliaEngine({"brand_key": "lacoste", "domain": "lacoste.com.br"})
    
    mock_fetch = AsyncMock(return_value={"results": [{"hits": [_SAMPLE_ALGO_HIT]}]})
    engine._fetch_algolia = mock_fetch
    engine._fetch_live_price = AsyncMock(return_value=699.0)

    url = "https://www.lacoste.com/br/lacoste/masculino/vestuario/polos/PH4014-23.html?color=031"
    res = await engine.get_pdp_product(url)

    assert res is not None
    assert res["product_code"] == "PH4014-23-031"
    # Verifies that it first queried PH4014-23-031
    assert mock_fetch.call_args[0][0]["query"] == "PH4014-23-031"


@pytest.mark.asyncio
async def test_algolia_pdp_rejects_sibling_color_returned_for_exact_variant_query():
    """A fuzzy Algolia result must not replace a card's requested colour."""
    engine = AlgoliaEngine({"brand_key": "lacoste", "domain": "lacoste.com.br"})
    sibling = {
        **_SAMPLE_ALGO_HIT,
        "productId": "CH8528-23-HDE",
        "objectID": "CH8528-23-HDE_lacoste-br",
        "urlMaster": "/br/lacoste/masculino/vestuario/camisas/CH8528-23.html?color=HDE",
        "name": "Camisa de Algodão de Mangas Curtas de Caimento Regular",
        "price": {"min": 799.0, "max": 799.0},
    }
    exact = {
        **sibling,
        "productId": "CH8528-23-HBP",
        "objectID": "CH8528-23-HBP_lacoste-br",
        "urlMaster": "/br/lacoste/masculino/vestuario/camisas/CH8528-23.html?color=HBP",
        "price": {"min": 589.0, "max": 589.0},
    }
    engine._fetch_algolia = AsyncMock(
        side_effect=[
            {"results": [{"hits": [sibling]}]},
            {"results": [{"hits": [exact]}]},
        ]
    )
    engine._fetch_live_price = AsyncMock(return_value=None)

    result = await engine.get_pdp_product(
        "https://www.lacoste.com/br/lacoste/masculino/vestuario/camisas/CH8528-23.html?color=HBP"
    )

    assert result is not None
    assert result["product_code"] == "CH8528-23-HBP"
    assert result["url"].endswith("CH8528-23.html?color=HBP")


def test_sfcc_parser_cleans_confec_and_extracts_current_product():
    """SFCC parser cleans trailing 'Confec' CMS truncation and extracts colors from currentProduct."""
    sample_sfcc_html = """
    <html>
      <head>
        <meta property="og:description" content="Camisa Polo Lacoste masculina de alta qualidade. Tradição desde 1933. Confec" />
      </head>
      <body>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org/",
          "@type": "Product",
          "name": "Polo Slim Fit em Mini Piqué Stretch",
          "description": "Camisa Polo Lacoste masculina de alta qualidade. Tradição desde 1933. Confec",
          "image": "https://imagesa1.lacoste.com/PH4014_031.jpg",
          "offers": [{"@type": "Offer", "price": 699.0, "availability": "https://schema.org/InStock"}]
        }
        </script>
        <script>
        window.currentProduct = {
          "id": "PH4014-23-031",
          "name": "Polo Slim Fit em Mini Piqué Stretch",
          "color": {"id": "031", "label": "Preto"},
          "description": {
            "descriptions": [{
              "list": ["Piqué de algodão stretch", "Slim fit, corte ajustado"]
            }]
          }
        };
        </script>
      </body>
    </html>
    """
    parsed = parse_pdp(sample_sfcc_html, "https://www.lacoste.com/br/polo.html")
    assert parsed is not None
    # Truncated word 'Confec' was cleaned up
    assert not parsed["raw_description"].endswith("Confec")
    assert "Tradição desde 1933." in parsed["raw_description"]
    # Bullet points from currentProduct added
    assert "• Piqué de algodão stretch" in parsed["raw_description"]
    # Color was extracted from window.currentProduct
    assert "Preto" in parsed["available_colors"]
