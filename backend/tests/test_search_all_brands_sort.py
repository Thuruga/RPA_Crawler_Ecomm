"""Garantia end-to-end: search_all_brands reordena TODA marca no fan-in.

Simula engines que IGNORAM `sort` (como Shopify/Wake/SFCC/Mercado Livre hoje):
devolvem os produtos em ordem "nativa". O ponto único de fan-in deve, ainda
assim, entregar cada lista ordenada por preço quando o usuário pede price_asc/
price_desc — e preservar a ordem nativa para relevance/recent/top_selling.
"""
import pytest

from core.models import BrandSearchResult, SearchProductResult
from services.engines.factory import EngineFactory, engine_factory


class _StubEngine:
    """Engine mínimo que devolve produtos na ordem nativa, ignorando `sort`."""

    def __init__(self, brand_key, products):
        self.brand_key = brand_key
        self._products = products

    async def search(
        self,
        query,
        max_results=10,
        sort=None,
        zipcode=None,
        include_shipping=False,
    ):
        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=self.brand_key,
            products=list(self._products),
        )


def _p(name, full):
    return SearchProductResult(brand="b", product_name=name, url=f"http://x/{name}", price_full=full)


def _patch_engines(monkeypatch, catalog):
    monkeypatch.setattr(
        EngineFactory,
        "get_engine",
        staticmethod(lambda brand_key: _StubEngine(brand_key, catalog[brand_key])),
    )


@pytest.mark.asyncio
async def test_price_asc_reorders_every_brand(monkeypatch):
    catalog = {
        "branda": [_p("a3", 300.0), _p("a1", 100.0), _p("a2", 200.0)],
        "brandb": [_p("b2", 250.0), _p("b1", 150.0)],
    }
    _patch_engines(monkeypatch, catalog)

    results = await engine_factory.search_all_brands(
        query="camisa", brands=["branda", "brandb"], sort="price_asc"
    )

    by_key = {r.brand_key: r for r in results}
    assert [p.product_name for p in by_key["branda"].products] == ["a1", "a2", "a3"]
    assert [p.product_name for p in by_key["brandb"].products] == ["b1", "b2"]


@pytest.mark.asyncio
async def test_price_desc_reorders_every_brand(monkeypatch):
    catalog = {"branda": [_p("a1", 100.0), _p("a3", 300.0), _p("a2", 200.0)]}
    _patch_engines(monkeypatch, catalog)

    results = await engine_factory.search_all_brands(
        query="camisa", brands=["branda"], sort="price_desc"
    )

    assert [p.product_name for p in results[0].products] == ["a3", "a2", "a1"]


@pytest.mark.asyncio
async def test_relevance_preserves_native_order(monkeypatch):
    catalog = {"branda": [_p("a3", 300.0), _p("a1", 100.0), _p("a2", 200.0)]}
    _patch_engines(monkeypatch, catalog)

    results = await engine_factory.search_all_brands(
        query="camisa", brands=["branda"], sort="relevance"
    )

    # relevance = ordem nativa do engine (não reordena client-side).
    assert [p.product_name for p in results[0].products] == ["a3", "a1", "a2"]
