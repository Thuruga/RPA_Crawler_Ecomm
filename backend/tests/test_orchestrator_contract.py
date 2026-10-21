import pytest
from core.models import RawProductBronze
from services.product_contract import extract_category_product_item
from core.pricing import calculate_price_averages


def test_extract_category_product_item_from_dict():
    raw_dict = {
        "url": "https://www.aramis.com.br/camiseta-tencel-soft-preto/p",
        "brand": "aramis",
        "raw_title": "Camiseta Manga Curta Tencel Soft Preto",
        "price_full": 329.0,
        "price_discount": 229.0,
        "price_discount_is_delta": False,
        "stock_availability": True,
        "image_url": "https://aramis.vtexassets.com/img.jpg",
        "sku_id": "123456",
        "product_code": "987",
    }

    item = extract_category_product_item(raw_dict, default_brand="Aramis")

    assert item["product_name"] == "Camiseta Manga Curta Tencel Soft Preto"
    assert item["price"] == 229.0
    assert item["price_original"] == 329.0
    assert item["url"] == "https://www.aramis.com.br/camiseta-tencel-soft-preto/p"
    assert item["image_url"] == "https://aramis.vtexassets.com/img.jpg"
    assert item["sku"] == "123456"
    assert item["brand"] == "aramis"
    assert item["is_available"] is True


def test_extract_category_product_item_without_discount():
    raw_dict = {
        "url": "https://www.aramis.com.br/camiseta-twist/p",
        "brand": "aramis",
        "raw_title": "Camiseta Manga Curta Twist Algodao",
        "price_full": 199.0,
        "price_discount": None,
        "stock_availability": True,
        "image_url": "https://aramis.vtexassets.com/twist.jpg",
    }

    item = extract_category_product_item(raw_dict, default_brand="Aramis")

    assert item["product_name"] == "Camiseta Manga Curta Twist Algodao"
    assert item["price"] == 199.0
    assert item["price_original"] == 199.0
    assert item["image_url"] == "https://aramis.vtexassets.com/twist.jpg"
    assert item["is_available"] is True


def test_extract_category_product_item_from_model():
    model = RawProductBronze(
        url="https://www.aramis.com.br/polo-piquet/p",
        brand="aramis",
        raw_title="Polo Piquet Everyday",
        price_full=279.0,
        price_discount=199.0,
        stock_availability=False,
        image_url="https://aramis.vtexassets.com/polo.jpg",
        sku_id="POLO-001",
    )

    item = extract_category_product_item(model, default_brand="Aramis")

    assert item["product_name"] == "Polo Piquet Everyday"
    assert item["price"] == 199.0
    assert item["price_original"] == 279.0
    assert item["is_available"] is False
    assert item["sku"] == "POLO-001"


def test_calculate_price_averages_includes_count_key():
    products = [
        {"brand": "Aramis", "price": 100.0, "price_full": 150.0, "stock_availability": True},
        {"brand": "Aramis", "price": 200.0, "price_full": 200.0, "stock_availability": True},
        {"brand": "Aramis", "price": 300.0, "price_full": 300.0, "stock_availability": False},
    ]

    res = calculate_price_averages(products)

    assert "count" in res
    assert res["count"] == 2
    assert res["total_items"] == 2
    assert res["average_price"] == 150.0
    assert res["brand_averages"]["aramis"]["count"] == 2
