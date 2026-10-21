"""
Breadcrumb (trilha de categorias) + POST /product/details — Quick task 260712-iiz.

Cobre:
  - VTEX `parse_product_dict` deriva `breadcrumb` a partir de `p.categories[0]`
    (mesmo split já usado para category/sub_category) — trilha completa quando
    presente, [] quando ausente.
  - Shopify `_map_to_bronze` deriva `breadcrumb` best-effort de `category`
    (um único segmento) — [] quando ausente.
  - `POST /product/details` (api.routes_search.product_details): marca válida
    + URL do domínio -> 200 com produto rico (incluindo breadcrumb); URL fora
    do domínio -> 400; `get_pdp_product` retornando None (bloqueio/anti-bot)
    -> 200 com state="unavailable" (nunca 500); exceção do engine -> 200 com
    state="error" (nunca 500).

100% hermético: nenhuma chamada de rede real. `VtexApiClient._get_color_family`
e `services.review_service.get_single_review` são monkeypatchados para não
tocar rede; o endpoint é exercitado chamando a coroutine da rota diretamente
(mesmo padrão de `test_phase01_api_category_contract.py`), sem TestClient/
X-API-Key.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import api.routes_search as routes_search
from services.vtex_api_scraper import VtexApiClient
from services.shopify_api_client import ShopifyApiClient


# ---------------------------------------------------------------------------
# Helpers — payload mínimo válido de VtexProduct
# ---------------------------------------------------------------------------
def _vtex_product_dict(categories: Optional[list] = None) -> Dict[str, Any]:
    return {
        "productId": "123",
        "productName": "Camisa Casual Slim",
        "brand": "Marca",
        "brandId": 1,
        "linkText": "camisa-casual-slim",
        "categoryId": "1",
        "categories": categories if categories is not None else ["/Masculino/Camisas/Casual/"],
        "categoriesIds": [],
        "link": "https://www.example.com/camisa-casual-slim",
        "description": "Uma camisa casual de algodão",
        "allSpecifications": [],
        "items": [
            {
                "itemId": "sku-1",
                "name": "Camisa",
                "nameComplete": "Camisa Casual Slim - Azul - P",
                "images": [{"imageUrl": "https://www.example.com/img/camisa.jpg"}],
                "sellers": [
                    {
                        "sellerId": "1",
                        "sellerName": "Loja Oficial",
                        "sellerDefault": True,
                        "commertialOffer": {
                            "Price": 149.90,
                            "ListPrice": 199.90,
                            "PriceWithoutDiscount": 199.90,
                            "RewardValue": 0.0,
                            "AvailableQuantity": 10,
                        },
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# VTEX — breadcrumb via parse_product_dict
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vtex_parse_product_dict_breadcrumb_full_trail():
    """p.categories == ['/Masculino/Calças/Jeans/'] -> breadcrumb completa."""
    client = VtexApiClient("marca")
    data = _vtex_product_dict(categories=["/Masculino/Calças/Jeans/"])

    with patch.object(VtexApiClient, "_get_color_family", return_value=[]), \
         patch("services.vtex_api_scraper.get_single_review", return_value=(None, None)):
        product = await client.parse_product_dict(data, data["link"], "www.example.com")

    assert product is not None
    assert product.breadcrumb == ["Masculino", "Calças", "Jeans"]
    # Aditivo: category/sub_category continuam sendo os dois primeiros segmentos.
    assert product.category == "Masculino"
    assert product.sub_category == "Calças"


@pytest.mark.asyncio
async def test_vtex_parse_product_dict_breadcrumb_empty_when_no_categories():
    """Sem categorias -> breadcrumb == [] (nunca placeholder)."""
    client = VtexApiClient("marca")
    data = _vtex_product_dict(categories=[])

    with patch.object(VtexApiClient, "_get_color_family", return_value=[]), \
         patch("services.vtex_api_scraper.get_single_review", return_value=(None, None)):
        product = await client.parse_product_dict(data, data["link"], "www.example.com")

    assert product is not None
    assert product.breadcrumb == []
    assert product.category is None


# ---------------------------------------------------------------------------
# Shopify — breadcrumb via _map_to_bronze
# ---------------------------------------------------------------------------
def _shopify_product_dict() -> Dict[str, Any]:
    return {
        "id": 999,
        "handle": "camisa-shopify",
        "title": "Camisa Shopify",
        "body_html": "<p>Descrição</p>",
        "vendor": "Marca",
        "product_type": "Camisas",
        "tags": [],
        "options": [],
        "images": [{"src": "https://www.example.com/img/shopify.jpg"}],
        "variants": [
            {"price": "129.90", "compare_at_price": None, "available": True}
        ],
    }


def test_shopify_map_to_bronze_breadcrumb_single_segment():
    """category='Camisas' -> breadcrumb == ['Camisas'] (best-effort, 1 segmento)."""
    client = ShopifyApiClient("marca")
    product = client._map_to_bronze(_shopify_product_dict(), category="Camisas")

    assert product is not None
    assert product.breadcrumb == ["Camisas"]
    assert product.category == "Camisas"


def test_shopify_map_to_bronze_breadcrumb_empty_when_no_category():
    """category=None -> breadcrumb == [] (honesto, sem trilha inventada)."""
    client = ShopifyApiClient("marca", "www.example.com")
    product = client._map_to_bronze(_shopify_product_dict(), category=None)

    assert product is not None
    assert product.breadcrumb == []
    assert product.category is None


# ---------------------------------------------------------------------------
# POST /product/details — api.routes_search.product_details
# ---------------------------------------------------------------------------
_FAKE_BRAND = SimpleNamespace(
    brand_key="exemplo", domain="www.example.com", is_active=True, engine="vtex"
)


class _FakeEngineOk:
    async def get_pdp_product(self, url: str) -> Dict[str, Any]:
        return {
            "url": url,
            "brand": "exemplo",
            "raw_title": "Camisa Casual Slim",
            "raw_description": "Uma camisa casual de algodão",
            "price_full": 149.90,
            "price_discount": 50.0,
            "price_discount_is_delta": True,
            "stock_availability": True,
            "image_url": "https://www.example.com/img/camisa.jpg",
            "available_colors": ["Azul", "Preto"],
            "available_sizes": ["P", "M", "G"],
            "composition": "100% algodão",
            "specifications": {"Material": "Algodão"},
            "breadcrumb": ["Masculino", "Camisas", "Casual"],
        }


class _FakeEngineUnavailable:
    async def get_pdp_product(self, _url: str) -> Optional[Dict[str, Any]]:
        return None


class _FakeEngineRaises:
    async def get_pdp_product(self, _url: str):
        raise RuntimeError("bloqueio anti-bot simulado")


@pytest.mark.asyncio
async def test_product_details_valid_brand_and_url_returns_rich_product():
    request = routes_search.ProductDetailsRequest(
        brand="exemplo", url="https://www.example.com/camisa-casual-slim"
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=_FakeEngineOk()):
        result = await routes_search.product_details(request)

    assert result["state"] == "ok"
    product = result["product"]
    assert product["available_colors"] == ["Azul", "Preto"]
    assert product["available_sizes"] == ["P", "M", "G"]
    assert product["raw_description"] == "Uma camisa casual de algodão"
    assert product["composition"] == "100% algodão"
    assert product["specifications"] == {"Material": "Algodão"}
    assert product["breadcrumb"] == ["Masculino", "Camisas", "Casual"]
    assert product["price_full"] == 149.90
    assert product["stock_availability"] is True


@pytest.mark.asyncio
async def test_product_details_uses_cached_scan_when_live_pdp_is_unavailable():
    """A falha da leitura ao vivo nao pode descartar um snapshot valido."""
    request = routes_search.ProductDetailsRequest(
        brand="exemplo", url="https://www.example.com/camisa-casual-slim"
    )
    cached = SimpleNamespace(
        id=321,
        raw_title="Camisa Casual Slim",
        raw_description="Uma camisa casual de algodão",
        image_url="https://www.example.com/img/camisa.jpg",
        price_full=199.90,
        price_discount=149.90,
        available_colors=["Azul"],
        available_sizes=["M"],
        breadcrumb=["Masculino", "Camisas"],
        composition="100% algodão",
        specifications='{"Material": "Algodão"}',
        stock_availability=True,
        product_code="123",
        review_product_id="123",
        captured_at=datetime.now(timezone.utc),
    )
    fake_db = MagicMock()
    fake_db.scan_products.find_first = AsyncMock(return_value=cached)
    fake_db.price_monitors.find_first = AsyncMock(return_value=None)

    with patch.object(routes_search.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=_FakeEngineUnavailable()), \
         patch("core.db.db", fake_db):
        result = await routes_search.product_details(request)

    assert result["state"] == "ok"
    assert result["product"]["raw_title"] == "Camisa Casual Slim"
    assert result["product"]["price_discount"] == 149.90
    assert result["product"]["available_sizes"] == ["M"]
    assert result["product"]["stock_availability"] is True


@pytest.mark.asyncio
async def test_product_details_url_outside_brand_domain_returns_400():
    request = routes_search.ProductDetailsRequest(
        brand="exemplo", url="https://www.outro-dominio.com/produto"
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=_FakeEngineOk()):
        with pytest.raises(HTTPException) as exc_info:
            await routes_search.product_details(request)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_product_details_engine_returns_none_is_unavailable_not_500():
    request = routes_search.ProductDetailsRequest(
        brand="exemplo", url="https://www.example.com/produto-bloqueado"
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=_FakeEngineUnavailable()):
        result = await routes_search.product_details(request)

    assert result == {"state": "unavailable", "product": None}


@pytest.mark.asyncio
async def test_product_details_engine_exception_degrades_to_error_not_500():
    request = routes_search.ProductDetailsRequest(
        brand="exemplo", url="https://www.example.com/produto-com-erro"
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=_FakeEngineRaises()):
        result = await routes_search.product_details(request)

    assert result == {"state": "error", "product": None}


@pytest.mark.asyncio
async def test_product_details_virtual_marketplace_no_brand_record_allowed():
    """Mercado Livre/Netshoes/Amazon não têm registro em brand_service — engine
    resolvido direto pela chave conhecida, sem checagem de domínio de marca."""
    request = routes_search.ProductDetailsRequest(
        brand="mercado_livre", url="https://www.mercadolivre.com.br/produto-x"
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=None), \
         patch.object(routes_search.engine_factory, "get_engine", return_value=_FakeEngineOk()):
        result = await routes_search.product_details(request)

    assert result["state"] == "ok"


@pytest.mark.asyncio
async def test_product_details_unknown_brand_key_returns_400():
    """Marca desconhecida (nem registro real, nem marketplace virtual) -> 400,
    nunca busca um host arbitrário sob uma marca inexistente (T-iiz-01)."""
    request = routes_search.ProductDetailsRequest(
        brand="marca-que-nao-existe", url="https://www.evil.example/produto"
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await routes_search.product_details(request)

    assert exc_info.value.status_code == 400
