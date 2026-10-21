import pytest
from core.pricing import calculate_price_averages
from core.models import ComparisonResult, BrandSearchResult, SearchProductResult


def test_calculate_price_averages_basic():
    products = [
        {"brand": "Aramis", "price": 100.0, "price_full": 150.0, "is_available": True},
        {"brand": "Aramis", "price": 200.0, "price_full": 200.0, "is_available": True},
        {"brand": "Reserva", "price": 300.0, "price_full": 400.0, "is_available": True},
    ]

    res = calculate_price_averages(products)

    # Global averages: (100 + 200 + 300) / 3 = 200.0
    assert res["average_price"] == 200.0
    # Global original: (150 + 200 + 400) / 3 = 750 / 3 = 250.0
    assert res["average_price_original"] == 250.0
    assert res["total_items"] == 3

    # Per-brand averages:
    assert "aramis" in res["brand_averages"]
    assert res["brand_averages"]["aramis"]["average_price"] == 150.0
    assert res["brand_averages"]["aramis"]["average_price_original"] == 175.0
    assert res["brand_averages"]["aramis"]["count"] == 2

    assert "reserva" in res["brand_averages"]
    assert res["brand_averages"]["reserva"]["average_price"] == 300.0
    assert res["brand_averages"]["reserva"]["average_price_original"] == 400.0
    assert res["brand_averages"]["reserva"]["count"] == 1


def test_calculate_price_averages_excludes_zero_and_out_of_stock():
    products = [
        {"brand": "Aramis", "price": 100.0, "price_full": 120.0, "is_available": True},
        {"brand": "Aramis", "price": 0.0, "price_full": 100.0, "is_available": True},
        {"brand": "Aramis", "price": -50.0, "price_full": 50.0, "is_available": True},
        {"brand": "Reserva", "price": 200.0, "price_full": 250.0, "is_available": False},
        {"brand": "Reserva", "price": None, "price_full": 250.0, "is_available": True},
    ]

    res = calculate_price_averages(products)

    assert res["total_items"] == 1
    assert res["average_price"] == 100.0
    assert res["average_price_original"] == 120.0
    assert "aramis" in res["brand_averages"]
    assert "reserva" not in res["brand_averages"]


def test_calculate_price_averages_empty_list():
    res = calculate_price_averages([])

    assert res["average_price"] is None
    assert res["average_price_original"] is None
    assert res["total_items"] == 0
    assert res["brand_averages"] == {}


def test_calculate_price_averages_with_pydantic_models():
    p1 = SearchProductResult(
        brand="Aramis",
        product_name="Polo 1",
        url="https://aramis.com.br/1",
        price_full=150.0,
        price_discount=100.0,
        available=True,
    )
    p2 = SearchProductResult(
        brand="Aramis",
        product_name="Polo 2",
        url="https://aramis.com.br/2",
        price_full=200.0,
        available=True,
    )

    res = calculate_price_averages([p1, p2])

    assert res["total_items"] == 2
    # p1 effective is 100.0, p2 effective is 200.0 -> avg = 150.0
    assert res["average_price"] == 150.0
    # p1 original is 150.0, p2 original is 200.0 -> avg = 175.0
    assert res["average_price_original"] == 175.0


def test_search_model_serialization():
    brand_res = BrandSearchResult(
        brand_key="aramis",
        brand_name="Aramis",
        products=[],
        average_price=150.0,
        average_price_original=180.0,
    )

    comp = ComparisonResult(
        query="camisa",
        brands_searched=["aramis"],
        results=[brand_res],
        average_price=150.0,
        average_price_original=180.0,
        brand_averages={"aramis": {"average_price": 150.0, "average_price_original": 180.0, "count": 1}},
    )

    data = comp.model_dump(mode="json")
    assert data["average_price"] == 150.0
    assert data["average_price_original"] == 180.0
    assert data["brand_averages"]["aramis"]["average_price"] == 150.0
    assert data["results"][0]["average_price"] == 150.0
    assert data["results"][0]["average_price_original"] == 180.0
