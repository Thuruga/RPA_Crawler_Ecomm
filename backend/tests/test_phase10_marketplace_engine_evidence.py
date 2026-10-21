import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from backend.services.engines.netshoes_engine import NetshoesEngine
from backend.services.engines.mercado_livre_engine import MercadoLivreEngine

@pytest.mark.asyncio
async def test_netshoes_search_fallback_jsonld():
    engine = NetshoesEngine()
    html = """<html><body>
    <script type="application/ld+json">
    {
      "@type": "ItemList",
      "itemListElement": [
        {
          "@type": "ListItem",
          "position": 1,
          "item": {
            "@type": "Product",
            "name": "Camiseta Teste",
            "url": "https://www.netshoes.com.br/camiseta-teste",
            "image": "https://img.netshoes.com.br/teste.jpg",
            "sku": "12345",
            "offers": {
              "@type": "Offer",
              "price": "99.90"
            }
          }
        }
      ]
    }
    </script>
    </body></html>"""
    
    with patch("curl_cffi.requests.AsyncSession.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_get.return_value = mock_resp
        
        result = await engine.search("camiseta")
        assert result.error is None
        assert len(result.products) >= 1
        p = result.products[0]
        assert p.product_name == "Camiseta Teste"
        assert p.url == "https://www.netshoes.com.br/camiseta-teste"
        assert p.price_full == 99.90
        assert p.product_code == "12345"

@pytest.mark.asyncio
async def test_netshoes_search_fallback_cards():
    engine = NetshoesEngine()
    html = """<html><body>
    <div class="item-card">
        <a href="/camiseta-teste-card" title="Camiseta Teste Card">
            <img src="//img.netshoes.com.br/testecard.jpg" />
        </a>
        <div class="item-card__price">
            <span class="haveInstallments">
                <span class="price">R$ 89,90</span>
            </span>
        </div>
    </div>
    </body></html>"""
    
    with patch("curl_cffi.requests.AsyncSession.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_get.return_value = mock_resp
        
        result = await engine.search("camiseta")
        assert result.error is None
        assert len(result.products) >= 1
        p = result.products[0]
        assert p.product_name == "Camiseta Teste Card"
        assert p.url == "https://www.netshoes.com.br/camiseta-teste-card"
        assert p.price_full == 89.90
        assert p.product_code is None # Not available in simple card

@pytest.mark.asyncio
async def test_netshoes_search_challenge_page():
    engine = NetshoesEngine()
    html = """<html><body>
    <h1>Access Denied</h1>
    <p>You don't have permission to access this resource.</p>
    </body></html>"""
    
    with patch("curl_cffi.requests.AsyncSession.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_get.return_value = mock_resp
        
        result = await engine.search("camiseta")
        assert result.error is not None
        assert "challenge" in result.error.lower() or "blocked" in result.error.lower() or "denied" in result.error.lower()

@pytest.mark.asyncio
async def test_mercado_livre_pdp_parser_jsonld():
    engine = MercadoLivreEngine()
    html = """<html><body>
    <script type="application/ld+json">
    {
      "@type": "Product",
      "name": "Camiseta ML",
      "image": "https://img.ml.com/ml.jpg",
      "sku": "MLB12345",
      "offers": {
        "@type": "Offer",
        "price": "59.90",
        "priceCurrency": "BRL"
      }
    }
    </script>
    </body></html>"""
    
    with patch("curl_cffi.requests.AsyncSession.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_get.return_value = mock_resp
        
        result = await engine.get_pdp_product("https://produto.mercadolivre.com.br/MLB-12345")
        assert result is not None
        assert result.get("raw_title") == "Camiseta ML"
        assert result.get("price_full") == 59.90
        assert result.get("product_code") == "MLB12345"

@pytest.mark.asyncio
async def test_mercado_livre_pdp_parser_dom_fallback():
    engine = MercadoLivreEngine()
    html = """<html><body>
    <h1 class="ui-pdp-title">Camiseta ML DOM</h1>
    <div class="ui-pdp-price__second-line">
        <span class="andes-money-amount__fraction">49</span><span class="andes-money-amount__cents">99</span>
    </div>
    </body></html>"""
    
    with patch("curl_cffi.requests.AsyncSession.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_get.return_value = mock_resp
        
        result = await engine.get_pdp_product("https://produto.mercadolivre.com.br/MLB-12345")
        assert result is not None
        assert result.get("raw_title") == "Camiseta ML DOM"
        assert result.get("price_full") == 49.99

@pytest.mark.asyncio
async def test_mercado_livre_shipping_parser():
    engine = MercadoLivreEngine()
    # Mocking calculate_shipping_advanced which runs the playwright/graphql flows
    with patch.object(engine, "_run_playwright_shipping", return_value={"is_free_shipping": False, "shipping_price": 15.90}):
        res = await engine.calculate_shipping_advanced("https://produto.mercadolivre.com.br/MLB-12345", "01310100")
        assert res is not None
        assert res["shipping_price"] == 15.90

@pytest.mark.asyncio
@patch("backend.core.browser_manager.BrowserManager.fetch_html", return_value="<html><body><h1>Playwright</h1></body></html>")
async def test_mercado_livre_pdp_parser_missing_info_fails(mock_bm):
    engine = MercadoLivreEngine()
    html = """<html><body>
    <h1>Nothing useful here</h1>
    </body></html>"""
    
    with patch("curl_cffi.requests.AsyncSession.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_get.return_value = mock_resp
        
        result = await engine.get_pdp_product("https://produto.mercadolivre.com.br/MLB-12345")
        assert result is None

