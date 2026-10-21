"""Exclusive ownership and boot reconciliation contract for Phase 07 Plan 03."""
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.job_manager import JOB_CANCEL_FLAGS, JobManager
from core.models import JobExecutionRecord, JobExecutionStatus
from services.job_repository import JsonJobRepository


def _run_claiming_process(
    claim_dir: str,
    marker_path: str,
    start_barrier: multiprocessing.synchronize.Barrier,
) -> None:
    """Process target proving that the claim surrounds the external side effect."""

    async def run() -> None:
        manager = JobManager(Path(claim_dir))

        async def external_call() -> None:
            with Path(marker_path).open("a", encoding="utf-8") as marker:
                marker.write("external-call\n")
                marker.flush()
            await asyncio.sleep(0.2)

        task = await manager.schedule(
            "category_scan:monitor-1:2026-07-26T12:00:00Z",
            external_call,
            record_id=f"attempt-{multiprocessing.current_process().pid}",
        )
        if task is not None:
            await task
        await manager.shutdown()

    start_barrier.wait(timeout=10)
    asyncio.run(run())


@pytest.mark.asyncio
async def test_claim_uses_logical_cycle_not_attempt_record_id(tmp_path: Path):
    manager_a = JobManager(tmp_path / "claims")
    manager_b = JobManager(tmp_path / "claims")
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def external_call(owner: str) -> None:
        calls.append(owner)
        started.set()
        await release.wait()

    logical_cycle_id = "price_check:monitor-1:2026-07-26T12:00:00Z"
    first, second = await asyncio.gather(
        manager_a.schedule(
            logical_cycle_id,
            lambda: external_call("first"),
            record_id="attempt-1",
        ),
        manager_b.schedule(
            logical_cycle_id,
            lambda: external_call("second"),
            record_id="attempt-2",
        ),
    )

    await asyncio.wait_for(started.wait(), timeout=2)
    assert sum(task is not None for task in (first, second)) == 1
    assert len(calls) == 1

    release.set()
    await asyncio.gather(*(task for task in (first, second) if task is not None))
    await manager_a.shutdown()
    await manager_b.shutdown()


@pytest.mark.asyncio
async def test_distinct_logical_cycles_can_progress_in_parallel(tmp_path: Path):
    manager = JobManager(tmp_path / "claims")
    both_started = asyncio.Event()
    release = asyncio.Event()
    running: set[str] = set()

    async def external_call(cycle: str) -> None:
        running.add(cycle)
        if len(running) == 2:
            both_started.set()
        await release.wait()

    tasks = [
        await manager.schedule(
            f"category_scan:{cycle}:2026-07-26T12:00:00Z",
            lambda cycle=cycle: external_call(cycle),
            record_id=f"record-{cycle}",
        )
        for cycle in ("monitor-a", "monitor-b")
    ]

    await asyncio.wait_for(both_started.wait(), timeout=2)
    assert all(task is not None for task in tasks)
    release.set()
    await asyncio.gather(*(task for task in tasks if task is not None))
    await manager.shutdown()


@pytest.mark.asyncio
async def test_task_is_strongly_referenced_and_exception_is_retrieved(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    manager = JobManager(tmp_path / "claims")
    release = asyncio.Event()

    async def failing_worker() -> None:
        await release.wait()
        raise RuntimeError("worker exploded")

    task = await manager.schedule(
        "search:user-1:manual-1",
        failing_worker,
        record_id="search-record-1",
    )
    assert task is not None
    assert task in manager._tasks

    with caplog.at_level(logging.ERROR):
        release.set()
        for _ in range(50):
            if task.done() and task not in manager._tasks:
                break
            await asyncio.sleep(0.01)

    assert task.done()
    assert task not in manager._tasks
    assert "worker exploded" in caplog.text
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cancel_waits_for_worker_cleanup_before_removing_registration(
    tmp_path: Path,
):
    manager = JobManager(tmp_path / "claims")
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def worker() -> None:
        try:
            started.set()
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned_up.set()

    task = await manager.schedule(
        "search:user-1:manual-cancel",
        worker,
        record_id="cancel-record",
    )
    assert task is not None
    await asyncio.wait_for(started.wait(), timeout=2)
    assert "cancel-record" in JOB_CANCEL_FLAGS

    assert await manager.cancel("cancel-record") is True
    assert cleaned_up.is_set()
    assert task.done()
    assert "cancel-record" not in JOB_CANCEL_FLAGS
    assert "cancel-record" not in manager.active_record_ids
    await manager.shutdown()


def test_two_processes_claim_exactly_one_external_call_for_one_logical_cycle(
    tmp_path: Path,
):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    claim_dir = tmp_path / "claims"
    marker_path = tmp_path / "external-calls.txt"
    processes = [
        context.Process(
            target=_run_claiming_process,
            args=(str(claim_dir), str(marker_path), barrier),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    barrier.wait(timeout=10)
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    assert marker_path.read_text(encoding="utf-8").splitlines() == ["external-call"]

def _durable_job(
    job_id: str,
    logical_cycle_id: str,
    *,
    kind: str = "search",
    input_payload: dict[str, object] | None = None,
) -> JobExecutionRecord:
    return JobExecutionRecord.model_validate(
        {
            "job_id": job_id,
            "kind": kind,
            "logical_cycle_id": logical_cycle_id,
            "input": input_payload or {"query": "camisa", "brands": ["aramis"]},
        }
    )


def _run_reconciling_process(
    claim_dir: str,
    repository_path: str,
    marker_path: str,
    start_barrier: multiprocessing.synchronize.Barrier,
) -> None:
    async def run() -> None:
        repository = JsonJobRepository(Path(repository_path))
        manager = JobManager(Path(claim_dir))

        async def resume(job_id: str, persisted_input: dict[str, object]) -> None:
            current = repository.get(job_id)
            assert current is not None
            assert persisted_input == {"query": "camisa", "brands": ["aramis"]}
            with Path(marker_path).open("a", encoding="utf-8") as marker:
                marker.write(f"{job_id}:{current.attempt}\n")
                marker.flush()
            await asyncio.sleep(0.2)
            assert repository.transition(
                job_id,
                current.attempt,
                {JobExecutionStatus.RUNNING},
                JobExecutionStatus.COMPLETED,
                {"result": {"owner_attempt": current.attempt}},
            ) is not None

        tasks = await manager.reconcile(repository, resume)
        if tasks:
            await asyncio.gather(*tasks)
        await manager.shutdown()

    start_barrier.wait(timeout=10)
    asyncio.run(run())


@pytest.mark.asyncio
async def test_reconcile_marks_interrupted_before_resuming_original_job_and_input(
    tmp_path: Path,
):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(
        _durable_job(
            "job-1",
            "search:user-1:manual-resume",
            input_payload={"query": "camisa", "brands": ["aramis"]},
        )
    )
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.PENDING},
        JobExecutionStatus.RUNNING,
    ) is not None
    manager = JobManager(tmp_path / "claims")
    release = asyncio.Event()
    observed: list[tuple[str, dict[str, object], int, list[JobExecutionStatus]]] = []

    async def resume(job_id: str, persisted_input: dict[str, object]) -> None:
        current = repository.get(job_id)
        assert current is not None
        observed.append(
            (
                job_id,
                persisted_input,
                current.attempt,
                [attempt.status for attempt in current.attempts],
            )
        )
        await release.wait()
        assert repository.transition(
            job_id,
            current.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
            {"result": {"resumed": True}},
        ) is not None

    tasks = await manager.reconcile(repository, resume)
    assert len(tasks) == 1
    for _ in range(100):
        if observed:
            break
        await asyncio.sleep(0.01)

    assert observed == [
        (
            "job-1",
            {"query": "camisa", "brands": ["aramis"]},
            2,
            [JobExecutionStatus.INTERRUPTED, JobExecutionStatus.RUNNING],
        )
    ]
    release.set()
    await asyncio.gather(*tasks)

    finished = repository.get("job-1")
    assert finished is not None
    assert finished.attempt == 2
    assert [attempt.status for attempt in finished.attempts] == [
        JobExecutionStatus.INTERRUPTED,
        JobExecutionStatus.COMPLETED,
    ]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_double_restart_reconcile_schedules_one_resume_in_process(tmp_path: Path):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(_durable_job("job-1", "search:user-1:manual-double"))
    manager = JobManager(tmp_path / "claims")
    calls = 0

    async def resume(job_id: str, persisted_input: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        current = repository.get(job_id)
        assert current is not None
        assert repository.transition(
            job_id,
            current.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
            {"result": persisted_input},
        ) is not None

    first, second = await asyncio.gather(
        manager.reconcile(repository, resume),
        manager.reconcile(repository, resume),
    )
    tasks = [*first, *second]
    await asyncio.gather(*tasks)

    current = repository.get("job-1")
    assert calls == 1
    assert len(tasks) == 1
    assert current is not None
    assert current.attempt == 2
    assert current.status == JobExecutionStatus.COMPLETED
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manual_requests_with_distinct_persisted_identities_are_not_deduplicated(
    tmp_path: Path,
):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(_durable_job("job-a", "search:user-1:manual-a"))
    repository.add(_durable_job("job-b", "search:user-1:manual-b"))
    manager = JobManager(tmp_path / "claims")
    calls: list[str] = []

    async def resume(job_id: str, _persisted_input: dict[str, object]) -> None:
        calls.append(job_id)
        current = repository.get(job_id)
        assert current is not None
        assert repository.transition(
            job_id,
            current.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
        ) is not None

    tasks = await manager.reconcile(repository, resume)
    await asyncio.gather(*tasks)

    assert sorted(calls) == ["job-a", "job-b"]
    assert len(tasks) == 2
    await manager.shutdown()


@pytest.mark.asyncio
async def test_late_old_attempt_callback_cannot_overwrite_resumed_terminal(tmp_path: Path):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(_durable_job("job-1", "search:user-1:manual-late"))
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.PENDING},
        JobExecutionStatus.RUNNING,
    ) is not None
    manager = JobManager(tmp_path / "claims")

    async def resume(job_id: str, _persisted_input: dict[str, object]) -> None:
        current = repository.get(job_id)
        assert current is not None
        assert repository.transition(
            job_id,
            current.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
            {"result": {"winner": "resume"}},
        ) is not None

    tasks = await manager.reconcile(repository, resume)
    await asyncio.gather(*tasks)
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.RUNNING},
        JobExecutionStatus.FAILED,
        {"error": "late old callback"},
    ) is None

    current = repository.get("job-1")
    assert current is not None
    assert current.attempt == 2
    assert current.status == JobExecutionStatus.COMPLETED
    assert current.result == {"winner": "resume"}
    await manager.shutdown()


def test_two_processes_reconcile_one_external_call_and_one_logical_ledger_execution(
    tmp_path: Path,
):
    repository_path = tmp_path / "jobs.json"
    repository = JsonJobRepository(repository_path)
    repository.add(_durable_job("job-1", "search:user-1:boot-shared"))
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.PENDING},
        JobExecutionStatus.RUNNING,
    ) is not None

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    marker_path = tmp_path / "resume-calls.txt"
    processes = [
        context.Process(
            target=_run_reconciling_process,
            args=(
                str(tmp_path / "claims"),
                str(repository_path),
                str(marker_path),
                barrier,
            ),
        )
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    barrier.wait(timeout=10)
    for process in processes:
        process.join(timeout=15)

    assert [process.exitcode for process in processes] == [0, 0]
    assert marker_path.read_text(encoding="utf-8").splitlines() == ["job-1:2"]
    current = JsonJobRepository(repository_path).get("job-1")
    assert current is not None
    assert current.attempt == 2
    assert current.status == JobExecutionStatus.COMPLETED
    assert [attempt.status for attempt in current.attempts] == [
        JobExecutionStatus.INTERRUPTED,
        JobExecutionStatus.COMPLETED,
    ]


# ---------------------------------------------------------------------------
# Plan 07-06 Task 1: multi-kind startup_reconcile orchestration
# ---------------------------------------------------------------------------


def test_register_reconcile_source_rejects_invalid_registration(tmp_path: Path):
    manager = JobManager(tmp_path / "claims")

    with pytest.raises(ValueError):
        manager.register_reconcile_source("", lambda: None)

    with pytest.raises(TypeError):
        manager.register_reconcile_source("search_history", "not-callable")


@pytest.mark.asyncio
async def test_startup_reconcile_all_kinds_are_enumerated_in_sorted_order(
    tmp_path: Path,
):
    manager = JobManager(tmp_path / "claims")
    order: list[str] = []

    def _make(kind: str, result: Any):
        async def _source() -> Any:
            order.append(kind)
            return result

        return _source

    manager.register_reconcile_source("price_check", _make("price_check", ["monitor-1"]))
    manager.register_reconcile_source("search_history", _make("search_history", []))
    manager.register_reconcile_source("category_scan", _make("category_scan", ["scan-1"]))

    results = await manager.startup_reconcile()

    assert order == ["category_scan", "price_check", "search_history"]
    assert results == {
        "category_scan": ["scan-1"],
        "price_check": ["monitor-1"],
        "search_history": [],
    }
    await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_reconcile_kind_with_no_orphans_schedules_nothing(
    tmp_path: Path,
):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    manager = JobManager(tmp_path / "claims")

    async def _search_source() -> Any:
        return await manager.reconcile(repository)

    manager.register_reconcile_source("search_history", _search_source)
    results = await manager.startup_reconcile()

    assert results == {"search_history": []}
    assert manager.active_record_ids == frozenset()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_reconcile_one_kind_failure_does_not_block_others(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
):
    manager = JobManager(tmp_path / "claims")
    calls: list[str] = []

    async def _failing_source() -> Any:
        calls.append("category_scan")
        raise RuntimeError("category scan storage corrupted")

    async def _ok_source() -> Any:
        calls.append("price_check")
        return ["resumed"]

    manager.register_reconcile_source("category_scan", _failing_source)
    manager.register_reconcile_source("price_check", _ok_source)

    with caplog.at_level(logging.ERROR):
        results = await manager.startup_reconcile()

    assert sorted(calls) == ["category_scan", "price_check"]
    assert results["price_check"] == ["resumed"]
    assert results["category_scan"] is None
    assert "kind=category_scan" in caplog.text
    await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_reconcile_double_call_does_not_duplicate_schedule(
    tmp_path: Path,
):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(_durable_job("job-1", "search:user-1:startup-double"))
    manager = JobManager(tmp_path / "claims")
    calls = 0

    async def resume(job_id: str, persisted_input: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        current = repository.get(job_id)
        assert current is not None
        assert repository.transition(
            job_id,
            current.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
            {"result": persisted_input},
        ) is not None

    async def _search_source() -> Any:
        return await manager.reconcile(repository, resume)

    manager.register_reconcile_source("search_history", _search_source)

    first, second = await asyncio.gather(
        manager.startup_reconcile(), manager.startup_reconcile()
    )
    tasks = [*(first.get("search_history") or []), *(second.get("search_history") or [])]
    await asyncio.gather(*tasks)

    current = repository.get("job-1")
    assert calls == 1
    assert len(tasks) == 1
    assert current is not None
    assert current.attempt == 2
    assert current.status == JobExecutionStatus.COMPLETED
    await manager.shutdown()


@pytest.mark.asyncio
async def test_startup_reconcile_invalid_repository_document_does_not_block_other_kinds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
):
    corrupted = tmp_path / "jobs.json"
    corrupted.write_text(
        json.dumps([{"job_id": "broken", "status": "not-a-real-status"}]),
        encoding="utf-8",
    )
    repository = JsonJobRepository(corrupted)
    manager = JobManager(tmp_path / "claims")
    other_calls = 0

    async def _other_source() -> Any:
        nonlocal other_calls
        other_calls += 1
        return []

    async def _corrupted_source() -> Any:
        return await manager.reconcile(repository)

    manager.register_reconcile_source("category_scan", _corrupted_source)
    manager.register_reconcile_source("price_check", _other_source)

    with caplog.at_level(logging.ERROR):
        results = await manager.startup_reconcile()

    assert results["category_scan"] == []
    assert other_calls == 1
    assert "reconcile: repository" in caplog.text
    await manager.shutdown()


# ---------------------------------------------------------------------------
# Plan 07-06 Task 2: app.py lifespan ordering (reconcile -> scheduler -> shutdown)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_reconciles_before_scheduler_start_and_shuts_down_in_order(
    monkeypatch: pytest.MonkeyPatch,
):
    """Spy on every collaborator app.py's lifespan touches to prove the exact
    ordering required by D-01/D-05/D-06: hydrate -> startup_reconcile ->
    start_loaded_monitor_loops -> scheduler job registration -> scheduler.start,
    and on exit: pause the scheduler -> job_manager.shutdown ->
    scheduler.shutdown(wait=True)."""
    import apscheduler.schedulers.asyncio as apscheduler_asyncio_module

    import app as app_module
    from core.job_manager import job_manager
    from services import price_monitor_service

    order: list[Any] = []

    async def _fake_startup_reconcile() -> dict[str, Any]:
        order.append("startup_reconcile")
        return {}

    async def _fake_shutdown() -> None:
        order.append("job_manager.shutdown")

    def _fake_load_monitors() -> None:
        order.append("load_monitors")

    def _fake_start_loaded_monitor_loops() -> None:
        order.append("start_loaded_monitor_loops")

    monkeypatch.setattr(job_manager, "startup_reconcile", _fake_startup_reconcile)
    monkeypatch.setattr(job_manager, "shutdown", _fake_shutdown)
    monkeypatch.setattr(
        price_monitor_service.monitor_service, "load_monitors", _fake_load_monitors
    )
    monkeypatch.setattr(
        price_monitor_service.monitor_service,
        "start_loaded_monitor_loops",
        _fake_start_loaded_monitor_loops,
    )
    monkeypatch.setattr(price_monitor_service.monitor_service, "tasks", {})

    class _FakeScheduler:
        def add_job(self, func: Any, trigger: Any, **kwargs: Any) -> None:
            order.append(("scheduler.add_job", kwargs.get("id")))

        def start(self) -> None:
            order.append("scheduler.start")

        def pause(self) -> None:
            order.append("scheduler.pause")

        def shutdown(self, wait: bool = True) -> None:
            order.append(("scheduler.shutdown", wait))

    monkeypatch.setattr(apscheduler_asyncio_module, "AsyncIOScheduler", _FakeScheduler)

    async with app_module.lifespan(SimpleNamespace()):
        pass

    assert order == [
        "load_monitors",
        "startup_reconcile",
        "start_loaded_monitor_loops",
        ("scheduler.add_job", "category_monitor_job"),
        ("scheduler.add_job", "pre_warm_all_brands_cache"),
        "scheduler.start",
        "scheduler.pause",
        "job_manager.shutdown",
        ("scheduler.shutdown", True),
    ]


@pytest.mark.asyncio
async def test_cancel_and_await_tasks_cancels_pending_and_leaves_none_running():
    """`app.py`'s shutdown cleanup must reach `PriceMonitorService.tasks` too —
    those loops are never registered with `JobManager` (JOB-02 cancellation
    hygiene), so `job_manager.shutdown()` alone would not cancel them."""
    import app as app_module

    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def _worker() -> None:
        try:
            started.set()
            await asyncio.Future()
        finally:
            cleaned_up.set()

    task = asyncio.create_task(_worker())
    await asyncio.wait_for(started.wait(), timeout=2)

    await app_module._cancel_and_await_tasks({"job-1": task})

    assert cleaned_up.is_set()
    assert task.done()
