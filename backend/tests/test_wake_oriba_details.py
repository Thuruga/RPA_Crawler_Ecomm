import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from services.engines.wake_engine import WakeEngine
from core.models import RawProductBronze


SAMPLE_ORIBA_PDP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Camisa Tricot Listrada - Oriba</title>
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Início", "item": "https://www.oriba.com.br"},
            {"@type": "ListItem", "position": 2, "name": "Camisa", "item": "https://www.oriba.com.br/camisa"}
        ]
    }
    </script>
</head>
<body>
    <script>
    let product = {
        "id": 151622,
        "name": "Camisa Tricot Listrada",
        "available": true,
        "stock": 10,
        "sku": "022420341003",
        "attribute_selections": {
            "selections": [
                {
                    "attribute_name": "Cor",
                    "values": [
                        {"value": "Bege", "available": true},
                        {"value": "Preto", "available": true},
                        {"value": "Natural", "available": false}
                    ]
                },
                {
                    "attribute_name": "Tamanho",
                    "values": [
                        {"value": "P", "available": true},
                        {"value": "M", "available": true},
                        {"value": "G", "available": false}
                    ]
                }
            ]
        },
        "attributes": [
            {"value": "ALGODAO", "name": "TipoTecido"},
            {"value": "CURTA", "name": "Manga"},
            {"value": "RETO", "name": "Shape"}
        ]
    };
    </script>

    <details class="details-pdp">
        <summary>Descrição</summary>
        <p>Camisa moderna com toque suave e caimento perfeito.</p>
    </details>

    <details class="details-pdp">
        <summary>Composição</summary>
        <p>95% Algodão, 5% Elastano</p>
    </details>

    <details class="details-pdp">
        <summary>Sustentabilidade</summary>
        <p>Algodão certificado BCI.</p>
    </details>
</body>
</html>
"""

SAMPLE_RICHARDS_PDP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Camisa Linho - Richards</title>
</head>
<body>
    <div id="drawerProductInfo">
        Camisa clássica 100% linho puro com modelagem confortável.
    </div>
    <div id="drawerProductComposition">
        100% Linho
    </div>
    <input type="radio" id="257__AZUL" name="color" value="Azul" />
    <label for="257__AZUL">Azul</label>
    <input type="radio" id="257__BRANCO" name="color" value="Branco" />
    <label for="257__BRANCO">Branco</label>

    <input type="radio" id="258__M" name="size" value="M" />
    <label for="258__M">M</label>
    <input type="radio" id="258__G" name="size" value="G" />
    <label for="258__G">G</label>
</body>
</html>
"""


def test_parse_wake_pdp_html_oriba():
    engine = WakeEngine(brand_key="oriba")
    details = engine._parse_wake_pdp_html(SAMPLE_ORIBA_PDP_HTML, "https://www.oriba.com.br/camisa-tricot-listrada-151622")

    assert details["raw_colors"] == ["Bege", "Preto", "Natural"]
    assert details["raw_size_grid"] == ["P", "M", "G"]
    assert details["available_colors"] == ["Bege", "Preto"]
    assert details["available_sizes"] == ["P", "M"]
    assert details["composition"] == "95% Algodão, 5% Elastano"
    assert "Camisa moderna" in details["raw_description"]
    assert details["specifications"]["TipoTecido"] == "ALGODAO"
    assert details["specifications"]["Manga"] == "CURTA"
    assert details["specifications"]["Shape"] == "RETO"
    assert details["specifications"]["Sustentabilidade"] == "Algodão certificado BCI."
    assert details["breadcrumb"] == ["Camisa"]
    assert details["stock_availability"] is True


def test_parse_wake_pdp_html_richards_legacy_fallback():
    engine = WakeEngine(brand_key="richards")
    details = engine._parse_wake_pdp_html(SAMPLE_RICHARDS_PDP_HTML, "https://www.richards.com.br/produto/camisa-linho")

    assert details["available_colors"] == ["Azul", "Branco"]
    assert details["available_sizes"] == ["M", "G"]
    assert details["composition"] == "100% Linho"
    assert "Camisa clássica 100% linho puro" in details["raw_description"]


@pytest.mark.asyncio
async def test_get_product_details_oriba_e2e_mock():
    engine = WakeEngine(brand_key="oriba")

    mock_resp_pdp = MagicMock()
    mock_resp_pdp.status_code = 200
    mock_resp_pdp.text = SAMPLE_ORIBA_PDP_HTML

    mock_resp_gql = MagicMock()
    mock_resp_gql.status_code = 200
    mock_resp_gql.json.return_value = {
        "data": {
            "search": {
                "products": {
                    "edges": [
                        {
                            "node": {
                                "productId": "151622",
                                "name": "Camisa Tricot Listrada",
                                "aliasComplete": "/camisa-tricot-listrada-151622",
                                "available": True,
                                "informations": [
                                    {"title": "Descrição", "value": "Texto descritivo GraphQL"},
                                    {"title": "Composição", "value": "95% Algodão, 5% Elastano"},
                                    {"title": "Referência", "value": "022420341003"}
                                ],
                                "attributes": [
                                    {"name": "Colecao", "value": "2026PV"}
                                ],
                                "images": [{"url": "https://img.oriba.com.br/camisa.jpg"}],
                                "prices": {"price": 460.0}
                            }
                        }
                    ]
                }
            }
        }
    }

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_resp_pdp
    mock_session.post.return_value = mock_resp_gql

    with patch("core.session_manager.SessionManager.get_session", return_value=mock_session):
        with patch.object(engine, "_resolve_token", new_callable=AsyncMock, return_value="test_token"):
            prod = await engine.get_product_details(
                product_url="https://www.oriba.com.br/camisa-tricot-listrada-151622"
            )

            assert isinstance(prod, dict)
            bronze = RawProductBronze(**prod)
            assert bronze.raw_title == "Camisa Tricot Listrada"
            assert bronze.available_colors == ["Bege", "Preto"]
            assert bronze.available_sizes == ["P", "M"]
            assert bronze.raw_colors == ["Bege", "Preto", "Natural"]
            assert bronze.composition == "95% Algodão, 5% Elastano"
            assert bronze.specifications["Colecao"] == "2026PV"
            assert bronze.specifications["Referência"] == "022420341003"
            assert bronze.specifications["TipoTecido"] == "ALGODAO"
            assert bronze.stock_availability is True


SAMPLE_ARAMIS_WF_BUY_HTML = """
<!DOCTYPE html>
<html>
<body>
    <div class="wf-buy">
        <input class="wf-buy__swatch-input" type="radio" data-color-name="Branco" value="Branco" checked />
        <input class="wf-buy__swatch-input" type="radio" data-color-name="Azul" value="Azul" />
        <label class="wf-buy__size-option">
            <input class="wf-buy__size-input" type="radio" value="P" />
        </label>
        <label class="wf-buy__size-option">
            <input class="wf-buy__size-input" type="radio" value="M" />
        </label>
        <label class="wf-buy__size-option is-disabled">
            <input class="wf-buy__size-input" type="radio" value="G" disabled />
        </label>
    </div>
</body>
</html>
"""


def test_parse_wake_pdp_html_wf_buy_aramis():
    engine = WakeEngine(brand_key="aramis")
    details = engine._parse_wake_pdp_html(SAMPLE_ARAMIS_WF_BUY_HTML, "https://www.aramis.com.br/produto/camisa-teste-12345")

    assert details["raw_colors"] == ["Branco", "Azul"]
    assert details["available_colors"] == ["Branco", "Azul"]
    assert details["raw_size_grid"] == ["P", "M", "G"]
    assert details["available_sizes"] == ["P", "M"]
    assert details["size_grid"] == ["P", "M", "G"]


@pytest.mark.asyncio
async def test_get_product_details_aramis_redirect_legacy_url():
    engine = WakeEngine(brand_key="aramis")

    mock_resp_pdp = MagicMock()
    mock_resp_pdp.status_code = 200
    mock_resp_pdp.url = "https://www.aramis.com.br/produto/camisa-manga-longa-slim-cetim-st-hudson-2-branco-165461"
    mock_resp_pdp.text = "<html><body></body></html>"

    mock_resp_gql = MagicMock()
    mock_resp_gql.status_code = 200
    mock_resp_gql.json.return_value = {
        "data": {
            "product": {
                "productId": 165461,
                "productName": "Camisa Manga Longa Slim Cetim St Hudson 2 - Branco",
                "aliasComplete": "/produto/camisa-manga-longa-slim-cetim-st-hudson-2-branco-165461",
                "available": True,
                "informations": [],
                "attributes": [],
                "images": [{"url": "https://img.aramis.com.br/camisa.jpg"}],
                "prices": {"price": 599.9, "listPrice": 599.9},
                "attributeSelections": {
                    "selections": [
                        {
                            "name": "Cor",
                            "values": [
                                {"value": "Branco", "available": True, "selected": True},
                                {"value": "Azul", "available": True, "selected": False},
                            ],
                        },
                        {
                            "name": "Tamanho",
                            "values": [
                                {"value": "P", "available": True, "selected": True},
                                {"value": "M", "available": True, "selected": False},
                                {"value": "G", "available": False, "selected": False},
                            ],
                        },
                    ]
                },
            }
        }
    }

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_resp_pdp
    mock_session.post.return_value = mock_resp_gql

    with patch("core.session_manager.SessionManager.get_session", return_value=mock_session):
        with patch.object(engine, "_resolve_token", new_callable=AsyncMock, return_value="test_token"):
            prod = await engine.get_product_details(
                product_url="https://www.aramis.com.br/camisa-manga-longa-slim-cetim-st-hudson-2-branco-ml-02-0894-001/p"
            )

            assert isinstance(prod, dict)
            bronze = RawProductBronze(**prod)
            assert bronze.raw_title == "Camisa Manga Longa Slim Cetim St Hudson 2 - Branco"
            assert bronze.price_full == 599.9
            assert bronze.available_sizes == ["P", "M"]
            assert bronze.available_colors == ["Branco", "Azul"]
            assert bronze.stock_availability is True

