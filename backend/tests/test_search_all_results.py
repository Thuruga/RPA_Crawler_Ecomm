"""Contract tests for the comparative-search result limit."""
from types import SimpleNamespace

import pytest

from api.routes_search import SearchRequest
from core.models import BrandSearchResult
from services.engines.factory import EngineFactory
from services.search_history_service import SearchHistoryService
from services.job_repository import JsonJobRepository


def test_search_request_keeps_legacy_default_and_allows_unlimited_or_large_limit():
    assert SearchRequest(query="camisa").max_per_brand == 10
    assert SearchRequest(query="camisa", max_per_brand=None).max_per_brand is None
    assert SearchRequest(query="camisa", max_per_brand=500).max_per_brand == 500
    with pytest.raises(Exception):
        SearchRequest(query="camisa", max_per_brand=0)


@pytest.mark.asyncio
async def test_factory_forwards_none_without_coercion(monkeypatch):
    received = []

    class Engine:
        async def search(self, **kwargs):
            received.append(kwargs["max_results"])
            return BrandSearchResult(brand_key="brand", brand_name="Brand")

    monkeypatch.setattr(EngineFactory, "get_engine", staticmethod(lambda _: Engine()))
    results = await EngineFactory().search_all_brands("camisa", brands=["brand"], max_per_brand=None)
    assert len(results) == 1
    assert received == [None]


@pytest.mark.asyncio
async def test_resumed_job_preserves_explicit_unlimited_limit(tmp_path, monkeypatch):
    service = SearchHistoryService(JsonJobRepository(tmp_path / "history.json"))
    created = service.create_job(job_id="all", query="camisa", brands=["brand"], max_per_brand=None)
    received = []

    async def search_all_brands(**kwargs):
        received.append(kwargs["max_per_brand"])
        return []

    monkeypatch.setattr("services.engines.factory.engine_factory.search_all_brands", search_all_brands)
    # The service imports the singleton lazily, so patch its module-global as well.
    monkeypatch.setattr("api.routes_search.engine_factory.search_all_brands", search_all_brands)
    await service.run_search_job("all", created.input)
    assert created.input["max_per_brand"] is None
    assert received == [None]
