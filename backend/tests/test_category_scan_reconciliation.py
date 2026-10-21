"""W0-03 category scan lifecycle: durable progress, CAS terminals, claim-first
scheduling and boot reconciliation for Phase 07 Plan 05 (JOB-01/JOB-02)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.job_manager import JobManager
from core.models import CategoryScanOrigin, CategoryScanStatus
from services import category_monitor_service as category_service
from services.engines.factory import engine_factory


def _redirect_category_repository(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(category_service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(category_service, "MONITORS_FILE", tmp_path / "monitors.json")
    monkeypatch.setattr(category_service, "SCANS_FILE", tmp_path / "scans.json")
    category_service._deleted_monitor_ids.clear()


def _monitor(monitor_id: str = "monitor-1", brand: str = "aramis") -> dict:
    return {
        "id": monitor_id,
        "url": "https://example.test/camisas",
        "brand": brand,
        "status": "active",
    }


class _FakeEngine:
    def __init__(self, products: list[dict], *, on_yield=None, error: Exception | None = None):
        self._products = products
        self._on_yield = on_yield
        self._error = error

    async def run_bulk_scrape(self, category_url: str):
        for product in self._products:
            yield product
            # Fires only after the consumer has fully processed the yielded
            # item (appended it + persisted progress), so hooks observe the
            # state *after* this product rather than before it.
            if self._on_yield is not None:
                await self._on_yield(product)
        if self._error is not None:
            raise self._error


def _patch_engine(monkeypatch, engine: _FakeEngine) -> None:
    # Patch the *class*, not the singleton instance: `get_engine` is a
    # `@staticmethod`, so setting it directly on the instance would leave a
    # permanent instance-level shadow behind once monkeypatch restores the
    # original function on teardown (`getattr` resolves it from the class,
    # so monkeypatch's "restore" re-sets it as an instance attribute) —
    # silently breaking every other test that patches the class afterward.
    monkeypatch.setattr(type(engine_factory), "get_engine", staticmethod(lambda brand: engine))


@pytest.mark.asyncio
async def test_streaming_persists_stage_and_completed_units_without_fabricating_percent(
    monkeypatch, tmp_path: Path
):
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = _monitor()
    observed_progress: list[dict] = []

    async def on_yield(_product):
        scans = category_service.list_category_scans(monitor["id"])
        if scans:
            observed_progress.append(dict(scans[0]["progress"]))

    engine = _FakeEngine(
        [{"url": f"https://example.test/p{i}"} for i in range(3)],
        on_yield=on_yield,
    )
    _patch_engine(monkeypatch, engine)
    manager = JobManager(tmp_path / "claims")

    result = await category_service.run_category_scan(
        monitor,
        origin=CategoryScanOrigin.MANUAL,
        scan_id="scan-progress-1",
        manager=manager,
    )

    assert result == "completed"
    assert [entry["completed_units"] for entry in observed_progress] == [1, 2, 3]
    for entry in observed_progress:
        assert entry["stage"] == "scraping"
        assert entry["total_units"] is None
        assert entry["percent"] is None

    final = category_service.list_category_scans(monitor["id"])[0]
    assert final["status"] == CategoryScanStatus.COMPLETED.value
    assert final["progress"]["completed_units"] == 3
    assert final["progress"]["total_units"] is None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_terminal_statuses_are_absorbing_per_attempt(monkeypatch, tmp_path: Path):
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = _monitor()
    engine = _FakeEngine([], error=RuntimeError("boom"))
    _patch_engine(monkeypatch, engine)
    manager = JobManager(tmp_path / "claims")

    result = await category_service.run_category_scan(
        monitor, origin=CategoryScanOrigin.MANUAL, scan_id="scan-terminal-1", manager=manager,
    )
    assert result == "failed"

    scans = category_service.list_category_scans(monitor["id"])
    assert scans[0]["status"] == CategoryScanStatus.FAILED.value
    assert scans[0]["error_message"] is not None

    # A late direct CAS attempt against the same attempt cannot override the terminal.
    overwritten = category_service._transition_category_scan(
        "scan-terminal-1",
        1,
        {CategoryScanStatus.RUNNING},
        CategoryScanStatus.COMPLETED,
        {"product_count": 99},
    )
    assert overwritten is None
    scans_after = category_service.list_category_scans(monitor["id"])
    assert scans_after[0]["status"] == CategoryScanStatus.FAILED.value
    assert scans_after[0]["product_count"] == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_late_delete_does_not_resurrect_monitor_or_overwrite_terminal(
    monkeypatch, tmp_path: Path
):
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = _monitor(monitor_id="monitor-delete")
    release = asyncio.Event()
    saw_delete = asyncio.Event()

    async def on_yield(product):
        if product["url"].endswith("p1"):
            category_service._deleted_monitor_ids.add(monitor["id"])
            saw_delete.set()
            await release.wait()

    engine = _FakeEngine(
        [{"url": "https://example.test/p1"}, {"url": "https://example.test/p2"}],
        on_yield=on_yield,
    )
    _patch_engine(monkeypatch, engine)
    manager = JobManager(tmp_path / "claims")

    release.set()
    result = await category_service.run_category_scan(
        monitor, origin=CategoryScanOrigin.MANUAL, scan_id="scan-delete-1", manager=manager,
    )
    assert result == "interrupted"

    scans = category_service.list_category_scans(monitor["id"])
    assert scans[0]["status"] == CategoryScanStatus.INTERRUPTED.value
    # Deleting again / a late terminal callback must not resurrect COMPLETED.
    late = category_service._transition_category_scan(
        "scan-delete-1", 1, {CategoryScanStatus.RUNNING}, CategoryScanStatus.COMPLETED,
    )
    assert late is None
    await manager.shutdown()


@pytest.mark.asyncio
async def test_orphan_pending_scan_is_interrupted_and_resumed_with_same_scan_id_and_input(
    monkeypatch, tmp_path: Path
):
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = _monitor(monitor_id="monitor-orphan")
    category_service.create_category_monitor(monitor["url"], monitor["brand"])
    monitors = category_service.load_all_monitored_categories()
    monitor["id"] = monitors[0]["id"]

    slot = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    logical_cycle_id = category_service._category_scan_logical_cycle_id(
        monitor["id"], origin=CategoryScanOrigin.SCHEDULED, slot_start_utc=slot,
    )
    orphan = category_service._get_or_create_category_scan(
        monitor,
        origin=CategoryScanOrigin.SCHEDULED,
        logical_cycle_id=logical_cycle_id,
        scan_id=None,
        scheduled_for=slot,
    )
    category_service._transition_category_scan(
        orphan.scan_id, 1, {CategoryScanStatus.PENDING}, CategoryScanStatus.RUNNING,
    )

    engine = _FakeEngine([{"url": "https://example.test/p1"}])
    _patch_engine(monkeypatch, engine)
    manager = JobManager(tmp_path / "claims")

    resumed_ids = await category_service.reconcile_orphaned_category_scans(manager=manager)

    assert resumed_ids == [orphan.scan_id]
    final = next(
        row for row in category_service.list_category_scans(monitor["id"])
        if row["scan_id"] == orphan.scan_id
    )
    assert final["status"] == CategoryScanStatus.COMPLETED.value
    assert final["attempt"] == 2
    assert [attempt["status"] for attempt in final["attempts"]] == [
        CategoryScanStatus.INTERRUPTED.value,
        CategoryScanStatus.COMPLETED.value,
    ]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_two_schedulers_same_slot_produce_one_external_call_and_one_logical_scan(
    monkeypatch, tmp_path: Path
):
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = _monitor(monitor_id="monitor-dual")
    calls: list[str] = []

    async def on_yield(_product):
        calls.append("call")

    engine = _FakeEngine([{"url": "https://example.test/p1"}], on_yield=on_yield)
    _patch_engine(monkeypatch, engine)

    slot = datetime(2026, 7, 26, 12, 10, tzinfo=timezone.utc)
    claim_dir = tmp_path / "claims"
    manager_a = JobManager(claim_dir)
    manager_b = JobManager(claim_dir)

    first, second = await asyncio.gather(
        category_service.run_category_scan(
            monitor, origin=CategoryScanOrigin.SCHEDULED, scheduled_for=slot, manager=manager_a,
        ),
        category_service.run_category_scan(
            monitor, origin=CategoryScanOrigin.SCHEDULED, scheduled_for=slot, manager=manager_b,
        ),
    )

    assert sorted([first, second]) == ["already_claimed", "completed"]
    assert len(calls) == 1
    scans = category_service.list_category_scans(monitor["id"])
    assert len(scans) == 1
    await manager_a.shutdown()
    await manager_b.shutdown()


@pytest.mark.asyncio
async def test_manual_origin_reuses_persisted_scan_id_as_stable_identity(
    monkeypatch, tmp_path: Path
):
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = _monitor(monitor_id="monitor-manual")
    engine = _FakeEngine([{"url": "https://example.test/p1"}])
    _patch_engine(monkeypatch, engine)
    manager = JobManager(tmp_path / "claims")

    result = await category_service.run_category_scan(
        monitor, origin=CategoryScanOrigin.MANUAL, scan_id="manual-scan-1", manager=manager,
    )

    assert result == "completed"
    scans = category_service.list_category_scans(monitor["id"])
    assert scans[0]["scan_id"] == "manual-scan-1"
    assert scans[0]["logical_cycle_id"] == "category_scan:monitor-manual:manual-scan-1"
    await manager.shutdown()


def test_slot_start_floors_to_ten_minute_utc_boundary():
    instant = datetime(2026, 7, 26, 12, 47, 33, tzinfo=timezone.utc)
    slot = category_service._category_scan_slot_start(instant)
    assert slot == datetime(2026, 7, 26, 12, 40, tzinfo=timezone.utc)
