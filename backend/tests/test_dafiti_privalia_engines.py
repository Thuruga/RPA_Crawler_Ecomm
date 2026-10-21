from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from core.models import RawProductBronze

from services.engines.factory import engine_factory
from services.engines.dafiti_engine import DafitiEngine, parse_dafiti_products
from services.engines.privalia_engine import PrivaliaEngine, parse_privalia_products


DAFITI_SAMPLE_HTML = """
<html>
<body>
  <div class="catalog-detail">
    <div class="product-box">
      <a class="product-box-link" href="/camisa-polo-azul-123.html" title="Camisa Polo Masculina Azul">
        <span class="product-box-brand">Reserva</span>
        <h2 class="product-box-title">Camisa Polo Azul</h2>
        <img class="product-image" src="https://dafitistatic.com/p1.jpg" />
        <span class="product-box-price-from">R$ 299,90</span>
        <span class="product-box-price-to">R$ 199,90</span>
      </a>
    </div>
    <div class="product-box">
      <a class="product-box-link" href="/calca-jeans-456.html" title="Calça Jeans Skinny">
        <span class="product-box-brand">Aramis</span>
        <h2 class="product-box-title">Calça Jeans Skinny</h2>
        <img class="product-image" src="https://dafitistatic.com/p2.jpg" />
        <span class="product-box-price-from">R$ 400,00</span>
        <span class="product-box-price-to">R$ 300,00</span>
      </a>
    </div>
    <div class="product-box">
      <a class="product-box-link" href="/bermuda-chino-789.html" title="Bermuda Chino">
        <span class="product-box-brand">Dafiti Joy</span>
        <h2 class="product-box-title">Bermuda Chino</h2>
        <img class="product-image" src="https://dafitistatic.com/p3.jpg" />
        <span class="product-box-price-to">R$ 100,00</span>
      </a>
    </div>
  </div>
</body>
</html>
"""

PRIVALIA_SAMPLE_HTML = """
<html>
<body>
  <div class="pr-catalog">
    <div class="product-card">
      <a class="product-link" href="/product/camisa-social-branca-001">
        <span class="product-brand">Aramis</span>
        <h3 class="product-name">Camisa Social Slim Branca</h3>
        <img class="product-image" src="https://privalia.com/img1.jpg" />
        <span class="price-old">R$ 399,00</span>
        <span class="price-new">R$ 199,50</span>
      </a>
    </div>
    <div class="product-card">
      <a class="product-link" href="/product/polo-piquet-002">
        <span class="product-brand">Lacoste</span>
        <h3 class="product-name">Camisa Polo Piquet</h3>
        <img class="product-image" src="https://privalia.com/img2.jpg" />
        <span class="price-old">R$ 500,00</span>
        <span class="price-new">R$ 350,00</span>
      </a>
    </div>
  </div>
</body>
</html>
"""


DAFITI_PDP_HTML = """
<html><body>
  <div class="seller-text"><a>Aramis</a></div>
  <h1>Calca Aramis Jeans Slim Basica Blue</h1>
  <div class="catalog-detail-price">
    <span class="catalog-detail-price-special">R$ 599,90</span>
    <span class="catalog-detail-price-value">R$ 234,24</span>
  </div>
  <div class="stock-available"><div class="stock-available-message">Produto Esgotado</div></div>
  <img class="gallery-preview-img" src="/p/aramis-calca.jpg" />
  <div id="desc-detail">Calca Jeans Slim Basica Blue Azul</div>
</body></html>
"""


PRIVALIA_PDP_HTML = """
<html><body>
  <div class="bp-product-detail-container">
    <img class="update-icon" src="/front/get/img/update-brands.svg" />
    <div class="bp-brand">Aramis</div>
    <h1 class="bp-product-name">Camisa Aramis Regular Micro Xadrez</h1>
    <div class="bp-price-container">
      <span class="bp-product-price bp-original-price">R$ 449,90 20%OFF</span>
      <span class="bp-price-with-discount">R$ 359,92 no Pix</span>
    </div>
    <span class="bp-stock-badge">10 left</span>
    <img class="bp-main-image" src="https://privalia.vtexassets.com/image.jpg" />
    <div class="bp-product-detail-tab-content">100% Algodão</div>
  </div>
</body></html>
"""

PRIVALIA_RICH_PDP_HTML = """
<html>
<head>
  <meta itemprop="description" content="Ignored head description" />
</head>
<body>
  <div class="bp-product-detail-container">
    <div class="bp-brand">Aramis</div>
    <h1 class="bp-product-name">Camisa Aramis Regular Micro Xadrez Bolso Verde Com Branco</h1>
    <div class="bp-price-container">
      <span class="bp-product-price bp-original-price">R$ 449,90<span class="bp-price-discount">20%OFF</span></span>
      <span class="bp-price-with-discount">R$ 359,92<small>no Pix</small></span>
      <div class="bp-payment-row">
        <span class="avista-text">à vista R$ 449,90</span>
        <span class="bp-installment">ou 5x de R$ 89,98* sem juros</span>
      </div>
    </div>
    <div class="bp-option-selector">
      <header class="bp-selector-header">Tamanho : P</header>
      <ul class="bp-option-list">
        <li class="bp-option-li">
          <input type="radio" id="size-P-0" name="product-size" class="bp-option-input" value="P" />
          <label class="bp-option-label selected" for="size-P-0">P <span class="bp-stock-badge">10 left</span></label>
        </li>
      </ul>
    </div>
    <img class="bp-main-image" src="https://privalia.vtexassets.com/image.jpg" />
    <div class="bp-product-detail-tab-content">
      <article>
        <p>
          Principais especificações do produto:<br/><br/>
          Com Bolso: Sim<br/><br/>
          Composição: 100% Algodão<br/><br/>
          Comprimento da Manga: Manga Curta<br/><br/>
          Cor: Verde<br/><br/>
          Estampa: Xadrez<br/><br/>
          Estilo: Casual<br/><br/>
          Indicado Para: Dia a dia<br/><br/>
          Modelagem: Regular<br/><br/>
          Tipo de Fechamento: Botão<br/><br/>
          Tipo de Gola: Gola Polo<br/><br/>
          Tipo de Manga: Curta<br/><br/>
          A Camisa Aramis Regular Micro Xadrez é a escolha definitiva para o homem que busca aliar sofisticação e bem-estar.
        </p>
      </article>
    </div>
    <article class="bp-specifications-table-container">
      <table class="bp-specifications-table">
        <tbody>
          <tr><td class="bp-spec-name">Composição</td><td class="bp-spec-values">100% Algodão</td></tr>
          <tr><td class="bp-spec-name">Comprimento da Manga</td><td class="bp-spec-values">Manga Curta</td></tr>
          <tr><td class="bp-spec-name">Punho com Botão</td><td class="bp-spec-values">Não</td></tr>
        </tbody>
      </table>
    </article>
  </div>
</body>
</html>
"""


def test_engine_factory_registration():
    dafiti_engine = engine_factory.get_engine("dafiti")
    assert isinstance(dafiti_engine, DafitiEngine)
    assert dafiti_engine.brand_key == "dafiti"

    privalia_engine = engine_factory.get_engine("privalia")
    assert isinstance(privalia_engine, PrivaliaEngine)
    assert privalia_engine.brand_key == "privalia"


def test_dafiti_html_parsing():
    products = parse_dafiti_products(DAFITI_SAMPLE_HTML)
    assert len(products) == 3

    p1 = products[0]
    assert "Camisa Polo Azul" in p1["product_name"]
    assert p1["price"] == 199.90
    assert p1["price_full"] == 299.90
    assert p1["price_discount"] == 199.90
    assert "camisa-polo-azul-123.html" in p1["url"]
    assert p1["image_url"] == "https://dafitistatic.com/p1.jpg"

    p3 = products[2]
    assert p3["price"] == 100.00
    assert p3["price_full"] == 100.00


@pytest.mark.asyncio
async def test_dafiti_search_with_averages():
    engine = DafitiEngine()
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = DAFITI_SAMPLE_HTML
        result = await engine.search("camisa polo", max_results=10)

        assert result.brand_key == "dafiti"
        assert len(result.products) == 3
        assert result.total_found == 3
        # avg effective: (199.90 + 300.00 + 100.00) / 3 = 199.97
        # avg original: (299.90 + 400.00 + 100.00) / 3 = 266.63
        assert result.average_price == 199.97
        assert result.average_price_original == 266.63


def test_privalia_html_parsing():
    products = parse_privalia_products(PRIVALIA_SAMPLE_HTML)
    assert len(products) == 2

    p1 = products[0]
    assert "Aramis" in p1["brand"]
    assert "Camisa Social Slim Branca" in p1["product_name"]
    assert p1["price"] == 199.50
    assert p1["price_full"] == 399.00
    assert p1["price_discount"] == 199.50
    assert "camisa-social-branca-001" in p1["url"]


@pytest.mark.asyncio
async def test_privalia_search_with_averages():
    engine = PrivaliaEngine()
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = PRIVALIA_SAMPLE_HTML
        result = await engine.search("camisa aramis", max_results=5)

        assert result.brand_key == "privalia"
        assert len(result.products) == 2
        assert result.total_found == 2
        # avg effective: (199.50 + 350.00) / 2 = 274.75
        # avg original: (399.00 + 500.00) / 2 = 449.50
        assert result.average_price == 274.75
        assert result.average_price_original == 449.50


@pytest.mark.asyncio
async def test_dafiti_pdp_maps_live_dom_to_monitor_contract():
    engine = DafitiEngine()
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = DAFITI_PDP_HTML
        payload = await engine.get_pdp_product("https://www.dafiti.com.br/product.html")

    product = RawProductBronze.model_validate(payload)
    assert product.brand == "Aramis"
    assert product.raw_title == "Calca Aramis Jeans Slim Basica Blue"
    assert product.price_full == 599.90
    assert product.price_discount == 234.24
    assert product.stock_availability is False
    assert product.image_url == "https://www.dafiti.com.br/p/aramis-calca.jpg"
    mock_fetch.assert_awaited_once()
    assert mock_fetch.await_args.kwargs["wait_selector"] == "h1, .catalog-detail-price, .catalog-detail-price-value"


@pytest.mark.asyncio
async def test_dafiti_pdp_prioritizes_datalayer_over_dom():
    engine = DafitiEngine()
    html_with_datalayer = """
    <html><body>
      <script>
        dataLayer.push({
            'pageName': 'Static.Calca-Aramis-Jeans-Slim-Basica-Blue-15163473html',
            'original_price': "599.90",
            'price': "339.90"
        });
      </script>
      <h1>Calca Aramis Jeans Slim Basica Blue</h1>
      <div class="seller-text"><a>Aramis</a></div>
      <div class="catalog-detail-price">
        <!-- DOM selector missing discount element (race condition simulation) -->
        <span class="catalog-detail-price-special">R$ 599,90</span>
      </div>
      <img class="gallery-preview-img" src="/p/aramis-calca.jpg" />
    </body></html>
    """
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = html_with_datalayer
        payload = await engine.get_pdp_product("https://www.dafiti.com.br/product.html")

    product = RawProductBronze.model_validate(payload)
    assert product.price_full == 599.90
    assert product.price_discount == 339.90


@pytest.mark.asyncio
async def test_dafiti_pdp_extracts_from_container_when_discount_class_missing():
    engine = DafitiEngine()
    html_container_fallback = """
    <html><body>
      <h1>Calca Aramis Jeans Slim Basica Blue</h1>
      <div class="seller-text"><a>Aramis</a></div>
      <div class="catalog-detail-price">
        <div class="catalog-detail-price-line">De R$ 599,90 -43%</div>
        <div class="catalog-detail-price-line">Por R$ 339,90</div>
      </div>
      <img class="gallery-preview-img" src="/p/aramis-calca.jpg" />
    </body></html>
    """
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = html_container_fallback
        payload = await engine.get_pdp_product("https://www.dafiti.com.br/product.html")

    product = RawProductBronze.model_validate(payload)
    assert product.price_full == 599.90
    assert product.price_discount == 339.90



@pytest.mark.asyncio
async def test_privalia_pdp_maps_live_dom_to_monitor_contract():
    engine = PrivaliaEngine()
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = PRIVALIA_PDP_HTML
        payload = await engine.get_pdp_product("https://br.privalia.com/lojas/pdp/productId/559005")

    product = RawProductBronze.model_validate(payload)
    assert product.brand == "Aramis"
    assert product.raw_title == "Camisa Aramis Regular Micro Xadrez"
    assert product.price_full == 449.90
    assert product.price_discount is None
    assert product.stock_availability is True
    assert product.image_url == "https://privalia.vtexassets.com/image.jpg"
    mock_fetch.assert_awaited_once()
    assert mock_fetch.await_args.kwargs["wait_selector"] == ".bp-product-name, .bp-price-container, .bp-main-image"


@pytest.mark.asyncio
async def test_dafiti_kids_fashion_filter():
    mixed_html = """
    <html><body>
      <div class="product-box">
        <a class="product-box-link" href="/vestido-menina.html">
          <h2 class="product-box-title">Vestido Infantil Floral Menina</h2>
          <img class="product-image" src="https://example.com/vestido.jpg" />
          <span class="product-box-price-to">R$ 89,90</span>
        </a>
      </div>
      <div class="product-box">
        <a class="product-box-link" href="/camisa-menino.html">
          <h2 class="product-box-title">Camisa Polo Infantil Menino</h2>
          <img class="product-image" src="https://example.com/camisa.jpg" />
          <span class="product-box-price-to">R$ 99,90</span>
        </a>
      </div>
    </body></html>
    """
    engine = DafitiEngine()
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = mixed_html
        result = await engine.search("polo infantil menino", max_results=10)

        assert len(result.products) == 1
        assert "Camisa Polo Infantil Menino" in result.products[0].product_name


@pytest.mark.asyncio
async def test_privalia_rich_pdp_extracts_specifications_composition_sizes_and_pix_offer():
    engine = PrivaliaEngine()
    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = PRIVALIA_RICH_PDP_HTML
        payload = await engine.get_pdp_product("https://br.privalia.com/lojas/pdp/productId/559005")

    product = RawProductBronze.model_validate(payload)
    assert product.brand == "Aramis"
    assert product.raw_title == "Camisa Aramis Regular Micro Xadrez Bolso Verde Com Branco"
    assert product.price_full == 449.90
    assert product.price_discount is None
    assert product.composition == "100% Algodão"
    assert product.available_colors == ["Verde"]
    assert product.available_sizes == ["P"]
    assert product.specifications is not None
    assert product.specifications.get("Com Bolso") == "Sim"
    assert product.specifications.get("Comprimento da Manga") == "Manga Curta"
    assert product.specifications.get("Punho com Botão") == "Não"
    assert "A Camisa Aramis Regular Micro Xadrez é a escolha definitiva" in (product.raw_description or "")

    assert product.conditional_offers is not None
    assert len(product.conditional_offers) == 1
    offer = product.conditional_offers[0]
    assert offer.condition_label == "Desconto Pix (20%)"
    assert offer.condition_type == "payment_method"
    assert offer.payment_method == "PIX"
    assert offer.price == Decimal("359.92")
    assert offer.discount_value == Decimal("20")
    assert offer.discount_unit == "percentage"


@pytest.mark.asyncio
async def test_privalia_vtex_api_search_success():
    engine = PrivaliaEngine()
    mock_products = [
        {
            "product_name": "Camisa Polo Slim Masculina",
            "title": "Camisa Polo Slim Masculina",
            "raw_title": "Camisa Polo Slim Masculina",
            "brand": "Aramis",
            "price": 150.00,
            "price_discount": 150.00,
            "price_full": 300.00,
            "url": "https://brandsplace.privalia.com/camisa-polo-123/p",
            "image_url": "https://privalia.vteximg.com.br/camisa.jpg",
            "available": True,
        },
        {
            "product_name": "Camisa Linho Masculina",
            "title": "Camisa Linho Masculina",
            "raw_title": "Camisa Linho Masculina",
            "brand": "Reserva",
            "price": 250.00,
            "price_discount": None,
            "price_full": 250.00,
            "url": "https://brandsplace.privalia.com/camisa-linho-456/p",
            "image_url": "https://privalia.vteximg.com.br/camisa2.jpg",
            "available": True,
        },
    ]

    with patch.object(engine, "_search_vtex_api", new_callable=AsyncMock) as mock_api:
        mock_api.return_value = mock_products
        result = await engine.search("camisa", max_results=5)

        assert result.brand_key == "privalia"
        assert result.total_found == 2
        assert len(result.products) == 2
        assert result.products[0].product_name == "Camisa Polo Slim Masculina"
        assert result.products[0].price_full == 300.00
        assert result.products[0].price_discount == 150.00
        assert result.average_price == 200.00
        assert result.average_price_original == 275.00
        mock_api.assert_awaited_once_with(query="camisa", max_results=5, sort=None)


def test_parse_privalia_products_vtex_io_gallery_item():
    vtex_io_html = """
    <div class="vtex-search-result-3-x-galleryItem">
      <section class="privalia-product-summary-0-x-container">
        <a class="privalia-product-summary-0-x-clearLink" href="/camisa-linho-602027/p">
          <span class="vtex-product-summary-2-x-brandName">Busa Linho Masculina</span>
          <img class="product-image" src="https://privalia.vteximg.com.br/p.jpg" />
          <span class="privalia-product-summary-0-x-customProductSummaryPriceShelf__sellingPrice">
            R$ 199,90
          </span>
          <span class="price-old">R$ 299,90</span>
        </a>
      </section>
    </div>
    """
    products = parse_privalia_products(vtex_io_html)
    assert len(products) == 1
    p = products[0]
    assert "Busa Linho Masculina" in p["product_name"]
    assert p["price"] == 199.90
    assert p["price_full"] == 299.90
    assert p["price_discount"] == 199.90
    assert p["url"] == "https://brandsplace.privalia.com/camisa-linho-602027/p"


def test_filter_mens_fashion_rejects_female_items_and_retains_menswear():
    """Valida rejeição de itens femininos (pantalona, SKU APF, categoria, URL, gênero)."""
    from services.engines.base_engine import BaseEngine

    items = [
        # Itens femininos do screenshot reportado pelo usuário
        {
            "title": "Conjunto Camisa Manga Curta e Calça Pantalona Marrom Salvatore",
            "url": "https://brandsplace.privalia.com/conjunto-salvatore/p",
        },
        {
            "title": "GAP4.8 Camisa GAP Bolso Preta",
            "sku": "GA056APF12XYZ",
            "url": "https://www.dafiti.com.br/Camisa-GAP-Bolso-Preta.html",
        },
        # Item com categoria feminina
        {
            "title": "Camisa Casual Básica",
            "categories": ["/Moda Feminina/Camisas/"],
            "url": "https://brandsplace.privalia.com/camisa-fem/p",
        },
        # Item com URL feminina
        {
            "title": "Camisa Manga Longa Preta",
            "url": "https://www.dafiti.com.br/feminino/roupas/camisa-preta.html",
        },
        # Item com gender feminino explícito
        {
            "title": "Camisa Estampada Floral",
            "gender": "feminino",
        },
        # Itens masculinos legítimos que DEVEM ser mantidos
        {
            "title": "Camisa Polo Aramis Manga Curta Masculina",
            "sku": "AR123APM456",
            "categories": ["/Moda Masculina/Polos/"],
            "url": "https://brandsplace.privalia.com/camisa-polo-masculina/p",
        },
        {
            "title": "Camisa Social Slim Fit Algodão",
            "sku": "DU789APM000",
            "url": "https://www.dafiti.com.br/Camisa-Social-Slim-Fit.html",
        },
        {
            "title": "Calça Alfaiataria Masculina Marrom",
            "gender": "masculino",
        },
    ]

    filtered = BaseEngine.filter_mens_fashion(items)
    assert len(filtered) == 3
    titles = [p["title"] for p in filtered]
    assert "Camisa Polo Aramis Manga Curta Masculina" in titles
    assert "Camisa Social Slim Fit Algodão" in titles
    assert "Calça Alfaiataria Masculina Marrom" in titles
    assert "Conjunto Camisa Manga Curta e Calça Pantalona Marrom Salvatore" not in titles
    assert "GAP4.8 Camisa GAP Bolso Preta" not in titles


@pytest.mark.asyncio
async def test_dafiti_gender_query_and_sku_filtering():
    """Valida injeção de &gender=masculino / &gender=menino e filtragem de SKU APF."""
    engine = DafitiEngine()

    html_with_female_and_male = """
    <html><body>
      <div class="product-box" data-sku="GA056APF12XYZ">
        <a class="product-box-link" href="/camisa-gap-feminina.html">
          <h2 class="product-box-title">Camisa GAP Bolso Preta</h2>
          <img class="product-image" src="https://example.com/gap.jpg" />
          <span class="product-box-price-to">R$ 159,99</span>
        </a>
      </div>
      <div class="product-box" data-sku="AR001APM999">
        <a class="product-box-link" href="/camisa-aramis-masculina.html">
          <h2 class="product-box-title">Camisa Aramis Masculina Manga Curta</h2>
          <img class="product-image" src="https://example.com/aramis.jpg" />
          <span class="product-box-price-to">R$ 249,99</span>
        </a>
      </div>
    </body></html>
    """

    with patch("core.browser_manager.BrowserManager.fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = html_with_female_and_male

        # Busca normal (masculino)
        result = await engine.search("camisa", max_results=10)
        assert len(result.products) == 1
        assert "Camisa Aramis Masculina Manga Curta" in result.products[0].product_name

        # Verifica que URL enviada continha gender=masculino
        called_url = mock_fetch.call_args_list[0][0][0]
        assert "gender=masculino" in called_url

        # Busca infantil (menino)
        await engine.search("camisa infantil menino", max_results=10)
        called_kids_url = mock_fetch.call_args_list[1][0][0]
        assert "gender=menino" in called_kids_url


@pytest.mark.asyncio
async def test_privalia_vtex_api_fq_and_female_category_rejection():
    """Valida uso de fq=C:2040 e exclusão de itens com categoria feminina na VTEX API."""
    engine = PrivaliaEngine()

    vtex_api_response = [
        # Produto feminino que não deve ser aceito
        {
            "productName": "Conjunto Camisa Manga Curta Salvatore",
            "brand": "Salvatore",
            "link": "https://brandsplace.privalia.com/conjunto-salvatore/p",
            "categories": ["/Moda Feminina/Conjuntos/"],
            "items": [
                {
                    "sellers": [
                        {"commertialOffer": {"Price": 249.99, "ListPrice": 349.99, "AvailableQuantity": 5}}
                    ]
                }
            ],
        },
        # Produto masculino que deve ser aceito
        {
            "productName": "Camisa Polo Piquet Masculina",
            "brand": "Polo Club",
            "link": "https://brandsplace.privalia.com/polo-piquet/p",
            "categories": ["/Moda Masculina/Polos/"],
            "items": [
                {
                    "sellers": [
                        {"commertialOffer": {"Price": 129.90, "ListPrice": 199.90, "AvailableQuantity": 10}}
                    ]
                }
            ],
        },
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = vtex_api_response

    mock_session = AsyncMock()
    mock_session.get.return_value = mock_resp

    with patch("core.session_manager.SessionManager.get_session", new_callable=AsyncMock) as mock_get_session:
        mock_get_session.return_value = mock_session

        # 1. Teste busca masculina
        products = await engine._search_vtex_api("camisa")
        assert len(products) == 1
        assert products[0]["product_name"] == "Camisa Polo Piquet Masculina"

        # Verifica URL chamada na API VTEX com fq=C:2040
        called_api_url = mock_session.get.call_args_list[0][0][0]
        assert "fq=C:2040" in called_api_url

        # 2. Teste busca infantil com fq=C:2042
        await engine._search_vtex_api("camisa menino infantil")
        called_kids_api_url = mock_session.get.call_args_list[1][0][0]
        assert "fq=C:2042" in called_kids_api_url




