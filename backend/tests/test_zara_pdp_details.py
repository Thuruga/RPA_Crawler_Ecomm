"""Tests for Zara PDP description, composition, and specifications extraction."""
import json
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from services.engines.zara_parser import (
    parse_product_detail,
    _extract_composition,
    _extract_description,
    _extract_specifications,
)
from core.models import RawProductBronze


_SAMPLE_ZARA_HTML = """
<!DOCTYPE html>
<html>
<head>
  <title>CAMISA AMPLA BOXY FIT | ZARA Brasil</title>
  <meta name="description" content="Camisa ampla boxy fit confeccionada em tecido compacto. Gola com lapela.">
</head>
<body>
  <div class="product-grid-product" data-productid="ZARA-BOXY-7484">
    <button class="product-color-extended-name__reference">7484/390/251</button>
  </div>

  <script type="application/ld+json">
  {
    "@context": "https://schema.org/",
    "@type": "ProductGroup",
    "name": "CAMISA AMPLA BOXY FIT",
    "url": "https://www.zara.com/br/pt/camisa-ampla-boxy-fit-p07484390.html",
    "sku": "07484390",
    "description": "Camisa ampla boxy fit confeccionada em tecido compacto. Gola com lapela e manga ampla com acabamento em punho com botão. Bolsos laterais na costura. Fecho frontal de abotoamento.",
    "material": "POLIÉSTER/VISCOSE/ELASTANO",
    "image": ["https://static.zara.net/photos/camisa_boxy.jpg"],
    "additionalProperty": [
      {
        "@type": "PropertyValue",
        "propertyID": "Composition",
        "name": "EXTERIOR",
        "value": "75% POLIÉSTER, 19% VISCOSE, 6% ELASTANO"
      },
      {
        "@type": "PropertyValue",
        "propertyID": "Composition",
        "name": "FORRO",
        "value": "100% POLIÉSTER"
      }
    ],
    "hasVariant": [
      {
        "@type": "Product",
        "color": "Branco-marfim",
        "size": "P",
        "sku": "07484390-P",
        "offers": {
          "@type": "Offer",
          "price": "439.00",
          "priceCurrency": "BRL",
          "availability": "https://schema.org/InStock"
        }
      },
      {
        "@type": "Product",
        "color": "Branco-marfim",
        "size": "M",
        "sku": "07484390-M",
        "offers": {
          "@type": "Offer",
          "price": "439.00",
          "priceCurrency": "BRL",
          "availability": "https://schema.org/InStock"
        }
      }
    ]
  }
  </script>
</body>
</html>
"""


def test_zara_pdp_extracts_real_description_and_composition():
    product = parse_product_detail(
        _SAMPLE_ZARA_HTML,
        "https://www.zara.com/br/pt/camisa-ampla-boxy-fit-p07484390.html",
        brand="Zara",
        base_url="https://www.zara.com",
    )
    assert product is not None
    assert product["raw_title"] == "CAMISA AMPLA BOXY FIT"
    # Descrição real, e NÃO apenas a repetição do título
    assert product["raw_description"].startswith("Camisa ampla boxy fit confeccionada em tecido compacto.")
    assert "Bolsos laterais na costura." in product["raw_description"]

    # Composição multi-partes
    assert product["composition"] is not None
    assert "EXTERIOR: 75% POLIÉSTER, 19% VISCOSE, 6% ELASTANO" in product["composition"]
    assert "FORRO: 100% POLIÉSTER" in product["composition"]

    # Especificações
    assert product["specifications"] is not None
    assert product["specifications"]["Referência"] == "7484/390/251"
    assert "Composição" in product["specifications"]


def test_zara_pdp_fallback_to_material_when_no_additional_property():
    html = """
    <html><body>
    <script type="application/ld+json">
    {
      "@type": "Product",
      "name": "Calça Jeans Zara",
      "image": ["https://static.zara.net/photos/calca.jpg"],
      "material": "100% ALGODÃO",
      "description": "Calça jeans com lavagem média.",
      "offers": {"price": "299.00"}
    }
    </script>
    </body></html>
    """
    product = parse_product_detail(
        html,
        "https://www.zara.com/br/pt/calca-jeans-p1.html",
        brand="Zara",
    )
    assert product is not None
    assert product["composition"] == "100% ALGODÃO"
    assert product["raw_description"] == "Calça jeans com lavagem média."


def test_zara_pdp_fallback_to_name_when_description_missing():
    html = """
    <html><body>
    <script type="application/ld+json">
    {
      "@type": "Product",
      "name": "Camiseta Básica",
      "image": ["https://static.zara.net/photos/camiseta.jpg"],
      "offers": {"price": "99.00"}
    }
    </script>
    </body></html>
    """
    product = parse_product_detail(
        html,
        "https://www.zara.com/br/pt/camiseta-basica-p1.html",
        brand="Zara",
    )
    assert product is not None
    assert product["raw_title"] == "Camiseta Básica"
    assert product["raw_description"] == "Camiseta Básica"
    assert product["composition"] is None


def test_zara_pdp_extracts_from_view_payload():
    payload = {
        "product": {
            "name": "BLAZER ESTRUTURADO",
            "seo": {
                "description": "Blazer estruturado com acabamento refinado."
            },
            "detail": {
                "displayReference": "5521/400",
                "detailedComposition": {
                    "parts": [
                        {
                            "description": "PRINCIPAL",
                            "components": [
                                {"material": "LÃ", "percentage": "90%"},
                                {"material": "CASHMERE", "percentage": "10%"}
                            ]
                        }
                    ]
                }
            },
            "sectionName": "MAN",
            "familyName": "BLAZER"
        }
    }
    html = f"""
    <html><body>
    <script>
      window.zara = {{}};
      window.zara.viewPayload = {json.dumps(payload)};
    </script>
    <script type="application/ld+json">
    {{
      "@type": "Product",
      "name": "BLAZER ESTRUTURADO",
      "image": ["https://static.zara.net/photos/blazer.jpg"],
      "offers": {{"price": "899.00"}}
    }}
    </script>
    </body></html>
    """
    product = parse_product_detail(
        html,
        "https://www.zara.com/br/pt/blazer-p1.html",
        brand="Zara",
    )
    assert product is not None
    assert product["raw_description"] == "Blazer estruturado com acabamento refinado."
    assert product["composition"] == "PRINCIPAL: 90% LÃ, 10% CASHMERE"
    assert product["specifications"]["Referência"] == "5521/400"
    assert product["specifications"]["Família"] == "BLAZER"
    assert product["specifications"]["Seção"] == "MAN"


@pytest.mark.asyncio
async def test_routes_search_product_details_enriches_shallow_scan_prod():
    import api.routes_search as routes_search
    from types import SimpleNamespace
    from datetime import datetime, timezone

    fake_brand = SimpleNamespace(
        brand_key="zara", domain="www.zara.com", is_active=True, engine="zara"
    )

    # Produto no DB apenas com dados de categoria (rasos)
    shallow_scan_prod = SimpleNamespace(
        id=123,
        raw_title="CAMISA AMPLA BOXY FIT",
        raw_description="CAMISA AMPLA BOXY FIT",  # igual ao título
        image_url="https://static.zara.net/img.jpg",
        price_full=439.0,
        price_discount=None,
        available_colors=["Branco"],
        available_sizes=["P", "M"],
        breadcrumb=["Masculino", "Camisas"],
        composition=None,  # sem composição
        specifications="{}",
        stock_availability=True,
        captured_at=datetime.now(timezone.utc),
    )

    # Produto retornado pelo engine ao vivo (rico)
    rich_engine_product = RawProductBronze(
        url="https://www.zara.com/br/pt/camisa-ampla-boxy-fit-p07484390.html",
        brand="zara",
        raw_title="CAMISA AMPLA BOXY FIT",
        raw_description="Camisa ampla boxy fit confeccionada em tecido compacto...",
        composition="EXTERIOR: 75% POLIÉSTER, 19% VISCOSE, 6% ELASTANO",
        specifications={"Referência": "7484/390"},
        price_full=439.0,
        image_url="https://static.zara.net/img.jpg",
        available_colors=["Branco"],
        available_sizes=["P", "M"],
        stock_availability=True,
    )

    fake_engine = MagicMock()
    fake_engine.get_pdp_product = AsyncMock(return_value=rich_engine_product)

    fake_db = MagicMock()
    fake_db.scan_products.find_first = AsyncMock(return_value=shallow_scan_prod)
    fake_db.scan_products.update = AsyncMock()
    fake_db.price_monitors.find_first = AsyncMock(return_value=None)

    request = routes_search.ProductDetailsRequest(
        brand="zara",
        url="https://www.zara.com/br/pt/camisa-ampla-boxy-fit-p07484390.html",
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=fake_brand), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=fake_engine), \
         patch("core.db.db", fake_db):
        res = await routes_search.product_details(request)

    assert res["state"] == "ok"
    prod = res["product"]
    # Verifica que o resultado foi enriquecido com a descrição e composição completas
    assert prod["raw_description"] == "Camisa ampla boxy fit confeccionada em tecido compacto..."
    assert prod["composition"] == "EXTERIOR: 75% POLIÉSTER, 19% VISCOSE, 6% ELASTANO"
    assert prod["specifications"]["Referência"] == "7484/390"

    # Verifica que o banco foi atualizado com os dados enriquecidos
    fake_db.scan_products.update.assert_awaited_once()
    call_kwargs = fake_db.scan_products.update.await_args.kwargs
    assert call_kwargs["where"] == {"id": 123}
    assert call_kwargs["data"]["composition"] == "EXTERIOR: 75% POLIÉSTER, 19% VISCOSE, 6% ELASTANO"
    assert call_kwargs["data"]["raw_description"] == "Camisa ampla boxy fit confeccionada em tecido compacto..."
