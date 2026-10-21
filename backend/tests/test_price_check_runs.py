"""W0-03 price-check cycle lifecycle: one durable run per pass, claim-first
scheduling, orphan resume and honest boot rehydration for Phase 07 Plan 05
(JOB-01/JOB-02, D-02)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.job_manager import JobManager
from core.models import (
    ConditionalOfferResult,
    PriceCheckRun,
    PriceCheckState,
    PriceMonitorConfig,
    PriceMonitorStatus,
)
from services import price_monitor_service
from services.engines.factory import engine_factory


def _service(monkeypatch, tmp_path: Path) -> price_monitor_service.PriceMonitorService:
    monkeypatch.setattr(price_monitor_service, "STORAGE_FILE", tmp_path / "price_monitors.json")
    return price_monitor_service.PriceMonitorService()


def _monitor_config(job_id: str = "job-1", **overrides) -> PriceMonitorConfig:
    now = datetime.now(timezone.utc)
    defaults = dict(
        job_id=job_id,
        url="https://example.test/produto",
        brand="aramis",
        interval_minutes=10,
        start_time=now.isoformat(),
        active=True,
        status=PriceMonitorStatus.ACTIVE,
        next_check_due_at=now,
    )
    defaults.update(overrides)
    return PriceMonitorConfig(**defaults)


def _hydrate_from_storage(service: price_monitor_service.PriceMonitorService) -> None:
    """Hydrate `service.monitors` from the monkeypatched JSON STORAGE_FILE.

    The service method hydrates from Postgres now (`load_monitors_async`), but
    these tests are about the boot *split* -- hydration alone must not create
    tasks, `start_loaded_monitor_loops` must -- not about which backend the
    rows come from. The module-level `load_monitors` still reads the file, so
    go through it and keep the database out of the test.
    """
    # Pass the path explicitly: `load_monitors`' default argument was bound to
    # the real STORAGE_FILE at import time, so relying on it would read the
    # production data file straight past the monkeypatch.
    for config in price_monitor_service.load_monitors(price_monitor_service.STORAGE_FILE):
        if config.next_check_due_at is None:
            config.next_check_due_at = price_monitor_service._parse_iso_datetime(
                config.start_time
            )
        service.monitors[config.job_id] = config


def _fake_product(**overrides) -> dict:
    payload = {
        "url": "https://example.test/produto",
        "brand": "aramis",
        "raw_title": "Camisa Teste",
        "price_full": 199.9,
    }
    payload.update(overrides)
    return payload


def _patch_engine(monkeypatch, engine) -> None:
    # Patch the class (see test_category_scan_reconciliation.py for why the
    # instance must never be shadowed): `get_engine` is a `@staticmethod`.
    monkeypatch.setattr(type(engine_factory), "get_engine", staticmethod(lambda brand: engine))


def _patch_offers(monkeypatch) -> None:
    monkeypatch.setattr(
        price_monitor_service,
        "collect_conditional_offers_for_monitor",
        AsyncMock(return_value=ConditionalOfferResult(status="none_detected", probe_level="full")),
    )


@pytest.mark.asyncio
async def test_ok_blocked_error_produce_distinct_terminal_states(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)

    # OK
    service_ok = _service(monkeypatch, tmp_path)
    config_ok = _monitor_config(job_id="job-ok")
    service_ok.monitors[config_ok.job_id] = config_ok
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=_fake_product())))
    result_ok = await service_ok.run_price_cycle(config_ok.job_id, manager=JobManager(tmp_path / "claims-ok"))
    assert result_ok == "ok"
    assert config_ok.check_runs[0].status == PriceCheckState.OK
    # last_success_at só é carimbado quando o ciclo realmente resolve o preço
    # (last_status == ok); nunca em blocked/error (ver abaixo).
    assert config_ok.last_success_at is not None
    assert config_ok.last_success_at == config_ok.last_checked_at

    # BLOCKED (engine returns None -> unreadable product)
    service_blocked = _service(monkeypatch, tmp_path)
    config_blocked = _monitor_config(job_id="job-blocked")
    service_blocked.monitors[config_blocked.job_id] = config_blocked
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=None)))
    result_blocked = await service_blocked.run_price_cycle(
        config_blocked.job_id, manager=JobManager(tmp_path / "claims-blocked")
    )
    assert result_blocked == "blocked"
    assert config_blocked.check_runs[0].status == PriceCheckState.BLOCKED
    assert config_blocked.last_success_at is None

    # ERROR (engine raises)
    service_error = _service(monkeypatch, tmp_path)
    config_error = _monitor_config(job_id="job-error")
    service_error.monitors[config_error.job_id] = config_error

    async def _raise(_url):
        raise RuntimeError("boom")

    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=_raise))
    result_error = await service_error.run_price_cycle(
        config_error.job_id, manager=JobManager(tmp_path / "claims-error")
    )
    assert result_error == "error"
    assert config_error.check_runs[0].status == PriceCheckState.ERROR
    assert config_error.last_success_at is None

    assert {result_ok, result_blocked, result_error} == {"ok", "blocked", "error"}


@pytest.mark.asyncio
async def test_cancellation_persists_interrupted_never_error_and_advances_cursor(
    monkeypatch, tmp_path: Path
):
    _patch_offers(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-cancel")
    service.monitors[config.job_id] = config
    due_at = config.next_check_due_at

    started = asyncio.Event()

    async def _hang(_url):
        started.set()
        await asyncio.Future()  # never resolves; only cancellation ends this

    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=_hang))
    manager = JobManager(tmp_path / "claims")

    task = asyncio.create_task(service.run_price_cycle(config.job_id, manager=manager))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The interruption itself is always recorded faithfully on the durable run.
    assert config.check_runs[0].status == PriceCheckState.INTERRUPTED
    assert config.active_cycle_id is None
    # WR-04: a fresh monitor (never yet completed a cycle) has no prior
    # last_status to preserve, so it stays None rather than being clobbered
    # to "interrupted" — see test below for the case where a prior real
    # status DOES exist and must survive the interruption.
    assert config.last_status is None
    assert config.next_check_due_at == due_at + timedelta(minutes=config.interval_minutes)


@pytest.mark.asyncio
async def test_cancellation_preserves_prior_last_status_and_error(monkeypatch, tmp_path: Path):
    """WR-04 regression: cancelling an in-flight cycle must never clobber the
    monitor's last known real terminal status (ok/stale/blocked/error) —
    `deriveIndividualLifecycle` (frontend) has no mapping for 'interrupted'
    and silently falls back to "Pendente", losing the last known real status
    the moment a monitor is stopped mid-cycle."""
    _patch_offers(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(
        job_id="job-cancel-preserve",
        last_status=PriceCheckState.BLOCKED.value,
        last_error="Bloqueado pela proteção anti-bot.",
    )
    service.monitors[config.job_id] = config
    due_at = config.next_check_due_at

    started = asyncio.Event()

    async def _hang(_url):
        started.set()
        await asyncio.Future()  # never resolves; only cancellation ends this

    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=_hang))
    manager = JobManager(tmp_path / "claims")

    task = asyncio.create_task(service.run_price_cycle(config.job_id, manager=manager))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert config.check_runs[0].status == PriceCheckState.INTERRUPTED
    assert config.active_cycle_id is None
    # The prior real status/error survive the interruption untouched.
    assert config.last_status == PriceCheckState.BLOCKED.value
    assert config.last_error == "Bloqueado pela proteção anti-bot."
    assert config.next_check_due_at == due_at + timedelta(minutes=config.interval_minutes)


@pytest.mark.asyncio
async def test_two_schedulers_same_due_at_produce_one_external_call_and_one_run(
    monkeypatch, tmp_path: Path
):
    _patch_offers(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-dual")
    service.monitors[config.job_id] = config
    calls: list[str] = []

    async def _get_pdp_product(_url):
        calls.append("call")
        return _fake_product()

    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=_get_pdp_product))

    claim_dir = tmp_path / "claims"
    manager_a = JobManager(claim_dir)
    manager_b = JobManager(claim_dir)

    first, second = await asyncio.gather(
        service.run_price_cycle(config.job_id, manager=manager_a),
        service.run_price_cycle(config.job_id, manager=manager_b),
    )

    assert sorted([first, second]) == ["already_claimed", "ok"]
    assert len(calls) == 1
    assert len(config.check_runs) == 1



@pytest.mark.asyncio
async def test_run_price_cycle_aborts_before_external_io_when_claim_persistence_fails(
    monkeypatch, tmp_path: Path
):
    _patch_offers(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-persist-fail")
    service.monitors[config.job_id] = config
    engine = SimpleNamespace(get_pdp_product=AsyncMock(return_value=_fake_product()))
    _patch_engine(monkeypatch, engine)

    # Claim persistence is a database write now, so fail that rather than the
    # old `atomic_write_json` file path -- the guarantee under test is
    # unchanged: if persisting the claim fails, abort before any external I/O.
    async def fail_persist(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "_save_monitors_async", fail_persist)
    manager = JobManager(tmp_path / "claims-persist-fail")
    due_at = config.next_check_due_at
    logical_cycle_id = f"price_check:{config.job_id}:{due_at.isoformat()}"

    with pytest.raises(OSError, match="disk full"):
        await service.run_price_cycle(config.job_id, manager=manager)

    assert engine.get_pdp_product.await_count == 0
    assert config.active_cycle_id is None
    claim = await manager.try_claim(logical_cycle_id)
    assert claim is not None
    await claim.release()

@pytest.mark.asyncio
async def test_resume_orphan_blocks_normal_tick_until_terminal_then_advances_cursor(
    monkeypatch, tmp_path: Path
):
    _patch_offers(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-resume")
    due_at = config.next_check_due_at
    logical_cycle_id = f"price_check:{config.job_id}:{due_at.isoformat()}"
    config.active_cycle_id = logical_cycle_id
    config.check_runs.append(PriceCheckRun(logical_cycle_id=logical_cycle_id, scheduled_for=due_at))
    service.monitors[config.job_id] = config

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _get_pdp_product(_url):
        calls.append("call")
        started.set()
        await release.wait()
        return _fake_product()

    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=_get_pdp_product))
    manager = JobManager(tmp_path / "claims")

    resume_task = asyncio.create_task(service.resume_price_cycle(config.job_id, manager=manager))
    await asyncio.wait_for(started.wait(), timeout=2)

    # The normal tick must observe the orphan's claim, not create a second run
    # nor make a second external call, and must not advance the cursor.
    observing = await service.run_price_cycle(config.job_id, manager=manager)
    assert observing == "observing"
    assert len(calls) == 1
    assert config.next_check_due_at == due_at

    release.set()
    result = await resume_task

    assert result == "ok"
    assert config.active_cycle_id is None
    assert config.next_check_due_at == due_at + timedelta(minutes=config.interval_minutes)
    assert len(config.check_runs) == 1
    assert config.check_runs[0].status == PriceCheckState.OK


@pytest.mark.asyncio
async def test_ledger_grows_without_retention_cap(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-ledger")
    service.monitors[config.job_id] = config
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=_fake_product())))

    manager = JobManager(tmp_path / "claims")
    for _ in range(5):
        result = await service.run_price_cycle(config.job_id, manager=manager)
        assert result == "ok"

    assert len(config.check_runs) == 5
    assert len({run.logical_cycle_id for run in config.check_runs}) == 5


@pytest.mark.asyncio
async def test_discount_metadata_change_does_not_notify_same_effective_price(
    monkeypatch, tmp_path: Path
):
    """A discount/original-price refresh must not say that the public price changed."""
    _patch_offers(monkeypatch)
    notifications: list[dict] = []
    monkeypatch.setattr(
        price_monitor_service.notification_store,
        "add_notification",
        lambda **kwargs: notifications.append(kwargs),
    )

    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(
        job_id="job-same-effective-price",
        last_price=234.24,
        last_price_original=280.0,
        last_price_discount=45.76,
        product_name="Calça Aramis Jeans Slim Basica Blue",
    )
    service.monitors[config.job_id] = config
    _patch_engine(
        monkeypatch,
        SimpleNamespace(
            get_pdp_product=AsyncMock(
                return_value=_fake_product(
                    price_full=300.0,
                    price_discount=234.24,
                )
            )
        ),
    )

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.OK
    assert error is None
    assert config.last_price == 234.24
    assert len(config.history) == 1
    assert notifications == []


@pytest.mark.asyncio
async def test_effective_price_change_notifies_the_exact_monitor_with_clear_label(
    monkeypatch, tmp_path: Path
):
    _patch_offers(monkeypatch)
    notifications: list[dict] = []
    monkeypatch.setattr(
        price_monitor_service.notification_store,
        "add_notification",
        lambda **kwargs: notifications.append(kwargs),
    )

    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(
        job_id="monitor/job 1",
        brand="aramis",
        last_price=249.90,
        product_name="Camisa Aramis Manga Longa Slim",
    )
    service.monitors[config.job_id] = config
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=_fake_product(price_full=199.90))))

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.OK
    assert error is None
    assert notifications == [{
        "title": "Alteração de Preço",
        "message": "aramis: Camisa Aramis Manga Longa Slim mudou de R$ 249.90 para R$ 199.90",
        "tab": "monitor",
        "route_kind": "monitor",
        "route_monitor_id": "monitor/job 1",
    }]


def test_load_monitors_recreates_only_active_status_never_legacy_active_bool(
    monkeypatch, tmp_path: Path
):
    """D-02/D-05/D-06 boot bug fix: a row with legacy `active: true` but an
    explicit `status: paused` must never be recreated as a running task."""
    storage = tmp_path / "price_monitors.json"
    monkeypatch.setattr(price_monitor_service, "STORAGE_FILE", storage)
    now = datetime.now(timezone.utc).isoformat()
    data = {
        "job-active": {
            "job_id": "job-active",
            "url": "https://example.test/a",
            "brand": "aramis",
            "active": True,
            "status": "active",
            "start_time": now,
        },
        "job-paused-legacy-active-true": {
            "job_id": "job-paused-legacy-active-true",
            "url": "https://example.test/b",
            "brand": "aramis",
            "active": True,
            "status": "paused",
            "start_time": now,
        },
    }
    storage.write_text(json.dumps(data), encoding="utf-8")

    async def _assert_and_cleanup():
        service = price_monitor_service.PriceMonitorService()
        # Plan 07-06: hydration only hydrates; task creation now happens
        # in the separate start_loaded_monitor_loops(), called after boot
        # reconciliation for the other job kinds.
        _hydrate_from_storage(service)
        assert service.tasks == {}
        service.start_loaded_monitor_loops()
        active_job_ids = set(service.tasks.keys())
        for task in service.tasks.values():
            task.cancel()
        await asyncio.gather(*service.tasks.values(), return_exceptions=True)
        return active_job_ids

    active_job_ids = asyncio.run(_assert_and_cleanup())
    assert active_job_ids == {"job-active"}


@pytest.mark.asyncio
async def test_start_loaded_monitor_loops_resumes_orphan_before_normal_tick_with_blocked_io(
    monkeypatch, tmp_path: Path,
):
    """Plan 07-06: the split boot path (load_monitors -> start_loaded_monitor_loops,
    called separately from app.py's lifespan after job_manager.startup_reconcile())
    must preserve the exact D-07 guarantee Plan 07-05 proved for the old, monolithic
    load_monitors — the resumed orphan holds the claim for its persisted
    active_cycle_id while a normal tick attempting the same cycle only observes,
    never makes a second external call."""
    _patch_offers(monkeypatch)
    storage = tmp_path / "price_monitors.json"
    monkeypatch.setattr(price_monitor_service, "STORAGE_FILE", storage)

    fresh_manager = JobManager(tmp_path / "claims")
    monkeypatch.setattr(price_monitor_service, "default_job_manager", fresh_manager)

    due_at = datetime.now(timezone.utc)
    logical_cycle_id = f"price_check:job-orphan:{due_at.isoformat()}"
    row = _monitor_config(
        job_id="job-orphan",
        next_check_due_at=due_at,
        active_cycle_id=logical_cycle_id,
        check_runs=[PriceCheckRun(logical_cycle_id=logical_cycle_id, scheduled_for=due_at)],
    ).model_dump(mode="json")
    storage.write_text(json.dumps({"job-orphan": row}), encoding="utf-8")

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _get_pdp_product(_url):
        calls.append("external-call")
        started.set()
        await release.wait()
        return _fake_product()

    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=_get_pdp_product))

    service = price_monitor_service.PriceMonitorService()
    _hydrate_from_storage(service)
    assert service.tasks == {}  # hydration alone must never create a task

    service.start_loaded_monitor_loops()
    assert set(service.tasks.keys()) == {"job-orphan"}

    await asyncio.wait_for(started.wait(), timeout=2)

    # A normal tick racing the exact same cycle while the boot-created resume
    # task holds the claim must only observe — never a second external call.
    observing = await service.run_price_cycle("job-orphan", manager=fresh_manager)
    assert observing == "observing"
    assert calls == ["external-call"]

    release.set()
    config = service.monitors["job-orphan"]
    for _ in range(200):
        if config.check_runs[0].status is not None:
            break
        await asyncio.sleep(0.01)

    assert config.active_cycle_id is None
    assert config.check_runs[0].status == PriceCheckState.OK

    # The resumed task falls through into the normal recurring loop, which
    # then sleeps until the freshly-advanced next_check_due_at — cancel it
    # rather than let the test wait out that interval.
    task = service.tasks["job-orphan"]
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await fresh_manager.shutdown()


def test_legacy_row_without_status_migrates_from_active_bool():
    active_row = PriceMonitorConfig.model_validate(
        {"job_id": "legacy-active", "url": "https://example.test/p", "brand": "aramis", "active": True}
    )
    paused_row = PriceMonitorConfig.model_validate(
        {"job_id": "legacy-paused", "url": "https://example.test/p", "brand": "aramis", "active": False}
    )
    assert active_row.status == PriceMonitorStatus.ACTIVE
    assert paused_row.status == PriceMonitorStatus.PAUSED
