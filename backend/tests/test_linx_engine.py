import pytest
from pathlib import Path
from bs4 import BeautifulSoup
from unittest.mock import AsyncMock
from core.models import BrandSearchResult
from services.engines.linx_engine import LinxEngine

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "linx_card.html"

@pytest.fixture
def sample_card_soup():
    content = FIXTURE_PATH.read_text(encoding="utf-8")
    soup = BeautifulSoup(content, "html.parser")
    return soup.find("li", class_="purchasable")

def test_get_engine_name():
    engine = LinxEngine()
    assert engine.get_engine_name() == "LinxCommerce"

def test_parse_linx_card_prices(sample_card_soup):
    engine = LinxEngine()
    product = engine.parse_product_card(sample_card_soup, base_domain="www.brooksfield.com.br")
    assert product is not None
    assert product.current_price == 399.92
    assert product.original_price == 499.90
    assert product.cash_price == 387.92
    assert product.installment_count == 10
    assert product.installment_value == 39.99

def test_parse_linx_card_variations(sample_card_soup):
    engine = LinxEngine()
    product = engine.parse_product_card(sample_card_soup, base_domain="www.brooksfield.com.br")
    assert product is not None
    assert "Marinho" in product.available_colors or "Mescla Cinza" in product.available_colors
    assert "P" in product.available_sizes or "M" in product.available_sizes or "G" in product.available_sizes

def test_parse_linx_stock_and_ids(sample_card_soup):
    engine = LinxEngine()
    product = engine.parse_product_card(sample_card_soup, base_domain="www.brooksfield.com.br")
    assert product is not None
    assert product.product_id == "2474208"
    assert product.title == "Polo Manga Longa Suedine"
    assert product.brand == "brooksfield"
    assert product.stock == 2
    assert product.url == "https://www.brooksfield.com.br/polo-manga-longa-suedine-pmreboa23005-p2474208?pp=/44.6862/&v=2474216"
    assert product.image_url.startswith("https://d1g58dq00jknii.cloudfront.net/")


@pytest.mark.asyncio
async def test_search_returns_a_brand_result_when_the_store_has_no_html():
    """search_all_brands requires one BrandSearchResult per selected brand."""
    engine = LinxEngine(brand_key="brooksfield")
    engine._fetch_html = AsyncMock(return_value=None)

    result = await engine.search("camisa")

    assert isinstance(result, BrandSearchResult)
    assert result.brand_key == "brooksfield"
    assert result.products == []
