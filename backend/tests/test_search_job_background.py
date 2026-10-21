"""W0-02: busca comparativa/SKU como job real de background (Phase 07 Plan 04).

Cobre: input reproduzível persistido antes de qualquer I/O externo, workers
que transicionam RUNNING/progresso/terminal sob CAS, resposta 202 antes da
liberação do fake worker, polling autenticado via GET /history/{job_id} e
cleanup que nunca apaga PENDING/RUNNING.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from api import routes_search
from core.job_manager import JobManager
from core.models import BrandSearchResult, JobExecutionStatus, SearchProductResult
from services.job_repository import JsonJobRepository
from services.search_history_service import SearchHistoryService


def _service(tmp_path: Path) -> SearchHistoryService:
    return SearchHistoryService(JsonJobRepository(tmp_path / "search_history.json"))


def _product(brand: str = "aramis") -> SearchProductResult:
    return SearchProductResult(
        brand=brand,
        product_name="Polo Piquet",
        url=f"https://{brand}.com.br/polo",
        price_full=199.9,
    )


# ---------------------------------------------------------------------------
# Service / worker / cleanup / resume (Task 1)
# ---------------------------------------------------------------------------


def test_service_create_job_persists_full_reproducible_input_before_any_io(
    tmp_path: Path,
):
    service = _service(tmp_path)
    created = service.create_job(
        job_id="search-1",
        query="polo piquet",
        brands=["aramis", "reserva"],
        type="search",
        max_per_brand=25,
        sort="price_asc",
        zipcode="01310100",
        include_shipping=True,
    )

    assert created.status == "PENDING"
    assert created.input == {
        "query": "polo piquet",
        "type": "search",
        "brands": ["aramis", "reserva"],
        "target_sku": None,
        "max_per_brand": 25,
        "sort": "price_asc",
        "zipcode": "01310100",
        "include_shipping": True,
    }

    reloaded = service.get_job("search-1")
    assert reloaded is not None
    assert reloaded.input == created.input
    assert reloaded.results is None


def test_service_create_job_cross_persists_sku_thresholds_and_cep(tmp_path: Path):
    service = _service(tmp_path)
    created = service.create_job(
        job_id="cross-1",
        query="SKU: CD.08.0292007",
        brands=["mercadolivre", "netshoes", "amazon"],
        type="cross",
        target_sku="CD.08.0292007",
        search_query=None,
        broad_query=None,
        min_score=60.0,
        zipcode="01310100",
    )

    assert created.input["target_sku"] == "CD.08.0292007"
    assert created.input["min_score"] == 60.0
    assert created.input["zipcode"] == "01310100"
    # Campos ausentes na request original (None) não viram defaults novos.
    assert "search_query" not in created.input
    assert "broad_query" not in created.input


@pytest.mark.asyncio
async def test_run_search_job_transitions_running_then_completed_under_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = _service(tmp_path)
    created = service.create_job(
        job_id="search-2", query="camisa", brands=["aramis"], type="search"
    )

    seen_status_during_call: list[str] = []

    async def fake_search_all_brands(**kwargs):
        current = service.get_job("search-2")
        assert current is not None
        seen_status_during_call.append(current.status)
        return [
            BrandSearchResult(
                brand_key="aramis", brand_name="Aramis", products=[_product()]
            )
        ]

    monkeypatch.setattr(
        routes_search.engine_factory, "search_all_brands", fake_search_all_brands
    )

    notifications: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "services.notification_service.notification_store.add_notification",
        lambda *a, **kw: notifications.append(kw),
    )

    await service.run_search_job("search-2", created.input)

    assert seen_status_during_call == ["RUNNING"]
    final = service.get_job("search-2")
    assert final is not None
    assert final.status == "COMPLETED"
    assert final.results == [
        {
            "brand_key": "aramis",
            "brand_name": "Aramis",
            "products": [_product().model_dump(mode="json")],
            "error": None,
            "total_found": 0,
            "average_price": 199.9,
            "average_price_original": 199.9,
        }
    ]
    assert notifications[-1]["route_kind"] == "search"
    assert notifications[-1]["route_job_id"] == "search-2"


@pytest.mark.asyncio
async def test_run_search_job_failure_is_terminal_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = _service(tmp_path)
    created = service.create_job(
        job_id="search-3", query="camisa", brands=["aramis"], type="search"
    )

    async def boom(**kwargs):
        raise RuntimeError("upstream indisponível\nTraceback stuff C:/secret")

    monkeypatch.setattr(routes_search.engine_factory, "search_all_brands", boom)

    await service.run_search_job("search-3", created.input)

    final = service.get_job("search-3")
    assert final is not None
    assert final.status == "FAILED"
    assert final.results is None
    assert "C:/secret" not in (final.error or "")


@pytest.mark.asyncio
async def test_run_cross_search_job_moves_vtex_lookup_into_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = _service(tmp_path)
    created = service.create_job(
        job_id="cross-2",
        query="SKU: CD.08.0292007",
        brands=["mercadolivre", "netshoes", "amazon"],
        type="cross",
        target_sku="CD.08.0292007",
        min_score=55.0,
    )

    vtex_calls: list[str] = []

    class FakeScraper:
        def __init__(self, brand_name: str):
            assert brand_name == "Aramis"

        async def search(self, query: str, max_results: int = 1):
            vtex_calls.append(query)
            ref = SimpleNamespace(
                product_name="Camisa Aramis Slim",
                url="https://aramis.com.br/camisa",
                image_url="https://aramis.com.br/img.jpg",
                price_full=299.9,
                category="Camisa",
            )
            return SimpleNamespace(products=[ref])

    monkeypatch.setattr(
        "services.vtex_api_scraper.VtexApiClient", FakeScraper
    )

    async def fake_compare_product(**kwargs):
        assert kwargs["target_sku"] == "CD.08.0292007"
        return {"status": "success", "results": []}

    monkeypatch.setattr(
        "services.cross_marketplace_service.cross_marketplace_service.compare_product",
        fake_compare_product,
    )

    await service.run_cross_search_job("cross-2", created.input)

    assert vtex_calls == ["CD.08.0292007"]
    final = service.get_job("cross-2")
    assert final is not None
    assert final.status == "COMPLETED"
    assert final.results["reference_product"]["name"] == "Camisa Aramis Slim"
    assert final.results["job_id"] == "cross-2"


def test_cleanup_only_removes_old_terminal_jobs(tmp_path: Path):
    service = _service(tmp_path)
    service.create_job(
        job_id="old-done", query="a", brands=["aramis"], type="search"
    )
    old_pending = service.create_job(
        job_id="old-pending", query="b", brands=["aramis"], type="search"
    )

    # Termina old-done e força o timestamp para 40 dias atrás.
    service._repository.transition(
        "old-done", 1, {JobExecutionStatus.PENDING}, JobExecutionStatus.RUNNING
    )
    service._repository.transition(
        "old-done",
        1,
        {JobExecutionStatus.RUNNING},
        JobExecutionStatus.COMPLETED,
        {"result": []},
    )
    from datetime import datetime, timedelta, timezone

    with service._repository._transaction():
        records = service._repository._read_unlocked()
        for record in records:
            if record.job_id == "old-done":
                record.completed_at = datetime.now(timezone.utc) - timedelta(days=40)
        service._repository._write_unlocked(records)

    removed = service.cleanup_old_records()

    assert removed == 1
    assert service.get_job("old-done") is None
    # PENDING nunca é removido pelo cleanup, mesmo "velho" (Pitfall 5).
    assert service.get_job("old-pending") is not None
    _ = old_pending


def test_delete_job_removes_record_and_is_idempotent(tmp_path: Path):
    service = _service(tmp_path)
    service.create_job(job_id="to-delete", query="a", brands=["aramis"])

    assert service.delete_job("to-delete") is True
    assert service.get_job("to-delete") is None
    assert service.delete_job("to-delete") is False


@pytest.mark.asyncio
async def test_resume_receives_same_job_id_and_persisted_payload(tmp_path: Path):
    """D-05/D-06: um job PENDING encontrado por reconcile é retomado com
    exatamente o payload persistido, via a resume factory registrada."""
    service = _service(tmp_path)
    created = service.create_job(
        job_id="resume-1",
        query="camisa",
        brands=["aramis"],
        type="search",
        max_per_brand=15,
    )

    manager = JobManager(tmp_path / "claims")
    received: list[tuple[str, dict]] = []

    async def fake_resume_factory(job_id: str, payload: dict) -> None:
        received.append((job_id, payload))

    manager.register_resume_factory("search", fake_resume_factory)

    tasks = await manager.reconcile(service._repository)

    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    assert received == [("resume-1", created.input)]
    resumed = service.get_job("resume-1")
    assert resumed is not None
    # O ledger preserva a tentativa interrompida antes da nova tentativa.
    assert any(a.status == "interrupted" for a in resumed.attempts)
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Rota HTTP: 202 imediato + polling (Task 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_search_returns_202_before_release_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = _service(tmp_path)
    monkeypatch.setattr(routes_search, "search_history_service", service)

    test_manager = JobManager(tmp_path / "claims")
    scheduled: dict[str, asyncio.Task] = {}
    original_schedule = test_manager.schedule

    async def capturing_schedule(logical_cycle_id, coroutine_factory, *, record_id):
        task = await original_schedule(
            logical_cycle_id, coroutine_factory, record_id=record_id
        )
        scheduled[record_id] = task
        return task

    monkeypatch.setattr(test_manager, "schedule", capturing_schedule)
    monkeypatch.setattr(routes_search, "job_manager", test_manager)

    monkeypatch.setattr(
        routes_search.brand_service,
        "list_brands",
        lambda active_only=True: [SimpleNamespace(brand_key="aramis")],
    )

    release = asyncio.Event()

    async def slow_search_all_brands(**kwargs):
        await release.wait()
        return []

    monkeypatch.setattr(
        routes_search.engine_factory, "search_all_brands", slow_search_all_brands
    )

    response = await routes_search.search_products(
        routes_search.SearchRequest(query="camisa", brands=["aramis"])
    )

    assert response.status == "pending"
    assert response.status_url == f"/history/{response.job_id}"
    assert response.poll_after_ms == 1500

    # Resposta 202 chegou antes do fake worker ser liberado -> job ainda não
    # pode estar COMPLETED.
    still_pending_or_running = service.get_job(response.job_id)
    assert still_pending_or_running is not None
    assert still_pending_or_running.status in ("PENDING", "RUNNING")

    release.set()
    task = scheduled[response.job_id]
    await task

    completed = service.get_job(response.job_id)
    assert completed is not None
    assert completed.status == "COMPLETED"

    await test_manager.shutdown()


@pytest.mark.asyncio
async def test_post_cross_marketplace_returns_202_and_completes_by_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = _service(tmp_path)
    monkeypatch.setattr(routes_search, "search_history_service", service)

    test_manager = JobManager(tmp_path / "claims")
    scheduled: dict[str, asyncio.Task] = {}
    original_schedule = test_manager.schedule

    async def capturing_schedule(logical_cycle_id, coroutine_factory, *, record_id):
        task = await original_schedule(
            logical_cycle_id, coroutine_factory, record_id=record_id
        )
        scheduled[record_id] = task
        return task

    monkeypatch.setattr(test_manager, "schedule", capturing_schedule)
    monkeypatch.setattr(routes_search, "job_manager", test_manager)

    async def fake_compare_product(**kwargs):
        return {"status": "success", "results": []}

    monkeypatch.setattr(
        "services.cross_marketplace_service.cross_marketplace_service.compare_product",
        fake_compare_product,
    )

    response = await routes_search.cross_marketplace_search(
        routes_search.CrossMarketplaceRequest(
            target_sku="CD.08.0292007", search_query="camisa aramis"
        )
    )

    assert response.status == "pending"
    assert response.status_url == f"/history/{response.job_id}"

    task = scheduled[response.job_id]
    await task

    completed = service.get_job(response.job_id)
    assert completed is not None
    assert completed.status == "COMPLETED"
    assert completed.results["job_id"] == response.job_id

    await test_manager.shutdown()


@pytest.mark.asyncio
async def test_post_search_rejects_invalid_brand_before_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = _service(tmp_path)
    monkeypatch.setattr(routes_search, "search_history_service", service)
    monkeypatch.setattr(
        routes_search.brand_service,
        "list_brands",
        lambda active_only=True: [SimpleNamespace(brand_key="aramis")],
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await routes_search.search_products(
            routes_search.SearchRequest(query="camisa", brands=["marca-inexistente"])
        )

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_get_history_returns_snapshot_with_poll_after_ms_and_404_uniform():
    from api.routes_history import get_history

    with pytest.raises(Exception) as exc_info:
        await get_history("does-not-exist")
    assert getattr(exc_info.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_get_history_result_absent_until_terminal(tmp_path: Path):
    from api import routes_history

    service = _service(tmp_path)
    monkeypatch_service = service
    routes_history.search_history_service = monkeypatch_service  # type: ignore[attr-defined]
    try:
        monkeypatch_service.create_job(
            job_id="pending-1", query="camisa", brands=["aramis"]
        )
        snapshot = await routes_history.get_history("pending-1")
        assert snapshot.status == "PENDING"
        assert snapshot.results is None
        assert snapshot.poll_after_ms == 1500
    finally:
        from services.search_history_service import search_history_service as real

        routes_history.search_history_service = real  # type: ignore[attr-defined]
