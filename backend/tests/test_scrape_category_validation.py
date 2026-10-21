"""
CR-02 regression (01-REVIEW.md): ScrapeCategoryRequest.resolved_url() must
reject a request with neither category_path nor custom_url via a clean
ValueError/400, not an uncaught AttributeError/500.
"""
import pytest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes_category import ScrapeCategoryRequest
from services.category_mapping import resolve_category_for_brands


def test_resolved_url_raises_value_error_when_both_params_missing():
    request = ScrapeCategoryRequest(brand="aramis")
    with pytest.raises(ValueError, match="category_path ou custom_url"):
        request.resolved_url()


def test_dynamic_mapping_with_complete_url_is_not_prefixed_with_host(monkeypatch):
    """Discovery-cache rows may persist a complete source URL."""
    import services.category_mapping as category_mapping

    brand = SimpleNamespace(
        domain="www.richards.com.br",
        mappings=[
            SimpleNamespace(
                canonical_slug="/masculino/calcas",
                vtex_fq_path="https://www.richards.com.br/masculino/calcas",
                label="Calças",
            )
        ],
    )
    monkeypatch.setattr(category_mapping.brand_service, "get_brand", lambda _: brand)

    resolved = resolve_category_for_brands("/masculino/calcas", ["richards"])

    assert resolved["richards"]["url"] == "https://www.richards.com.br/masculino/calcas"


def test_scrape_category_endpoint_returns_400_not_500():
    import api.routes_category as routes_category

    app = FastAPI()
    app.include_router(routes_category.router)
    client = TestClient(app)

    response = client.post("/scrape-category", json={"brand": "aramis"})

    assert response.status_code == 400, (
        f"esperava 400 (validacao limpa), obtido {response.status_code}: {response.text}"
    )
    assert "category_path ou custom_url" in response.json()["detail"]


@pytest.mark.parametrize("brand", ["dafiti", "privalia", "amazon", "mercado_livre", "netshoes"])
def test_scrape_category_endpoint_rejects_marketplaces_with_400(brand):
    import api.routes_category as routes_category

    app = FastAPI()
    app.include_router(routes_category.router)
    client = TestClient(app)

    response = client.post("/scrape-category", json={"brand": brand, "custom_url": "https://example.com/cat"})

    assert response.status_code == 400
    assert "Marketplaces não suportam varredura por categoria" in response.json()["detail"]


@pytest.mark.parametrize("brand", ["dafiti", "privalia", "amazon", "mercado_livre", "netshoes"])
def test_create_category_monitor_endpoint_rejects_marketplaces_with_400(brand):
    import api.routes_monitor as routes_monitor

    app = FastAPI()
    app.include_router(routes_monitor.router)
    client = TestClient(app)

    response = client.post("/monitor/category", json={"brand": brand, "url": "https://example.com/cat"})

    assert response.status_code == 400
    assert "Marketplaces não suportam monitor de categoria" in response.json()["detail"]

