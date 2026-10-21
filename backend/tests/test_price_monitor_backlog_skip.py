"""Backlog skipping and bounded check_runs ledger for the price monitor.

A monitor whose cursor fell behind (server down, paused, machine asleep) used
to replay every missed slot -- one live scrape and one `check_runs` entry per
slot. A 10-minute monitor idle for eight weeks came back with an ~8200-slot
backlog and ground through it at roughly one request per second, saturating
the event loop until the process stopped answering requests at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.job_manager import JobManager
from core.models import PriceMonitorConfig, PriceMonitorStatus
from services import price_monitor_service
from services.engines.factory import engine_factory
from services.price_monitor_service import (
    CHECK_RUNS_HISTORY_LIMIT,
    _advance_due_at,
)


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


# ---------------------------------------------------------------------------
# _advance_due_at
# ---------------------------------------------------------------------------


def test_advance_due_at_keeps_plain_interval_step_when_next_slot_is_future():
    """The ordinary case is untouched: cursor + interval, nothing clever."""
    due_at = datetime.now(timezone.utc)
    assert _advance_due_at(due_at, 10) == due_at + timedelta(minutes=10)


def test_advance_due_at_skips_backlog_instead_of_replaying_missed_slots():
    """An eight-week-old cursor lands ahead of now, not 8000 slots behind it."""
    interval_minutes = 10
    due_at = datetime.now(timezone.utc) - timedelta(weeks=8)

    advanced = _advance_due_at(due_at, interval_minutes)

    assert advanced > datetime.now(timezone.utc)
    # A single call is enough -- no need to iterate the backlog away.
    assert advanced - datetime.now(timezone.utc) <= timedelta(minutes=interval_minutes)


def test_advance_due_at_stays_on_the_original_slot_grid():
    """Skipping must not drift the grid: `logical_cycle_id` depends on it."""
    interval_minutes = 10
    due_at = datetime.now(timezone.utc) - timedelta(days=13, minutes=3)

    advanced = _advance_due_at(due_at, interval_minutes)

    offset = advanced - due_at
    assert offset % timedelta(minutes=interval_minutes) == timedelta(0)


def test_advance_due_at_treats_non_positive_interval_as_one_minute():
    """Mirrors the previous `max(1, interval_minutes)` guard."""
    due_at = datetime.now(timezone.utc)
    assert _advance_due_at(due_at, 0) == due_at + timedelta(minutes=1)


def test_advance_due_at_is_idempotent_once_ahead_of_now():
    """Applying it to an already-future cursor just adds one more interval."""
    due_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert _advance_due_at(due_at, 10) == due_at + timedelta(minutes=10)


# ---------------------------------------------------------------------------
# bounded check_runs ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_runs_ledger_is_bounded_and_keeps_the_newest_runs(
    monkeypatch, tmp_path: Path
):
    """A long-lived monitor must not grow its ledger without limit."""
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-ledger")
    service.monitors[config.job_id] = config

    # Pre-fill the ledger to exactly the cap with terminal runs, so the next
    # append is the one that must evict.
    filler_ids = []
    for index in range(CHECK_RUNS_HISTORY_LIMIT):
        cycle_id = f"price_check:job-ledger:filler-{index}"
        filler_ids.append(cycle_id)
        config.check_runs.append(
            price_monitor_service.PriceCheckRun(
                logical_cycle_id=cycle_id,
                scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=index + 1),
            )
        )
    assert len(config.check_runs) == CHECK_RUNS_HISTORY_LIMIT

    async def _boom(_url):
        raise RuntimeError("boom")

    # Patch the *class*, not the `engine_factory` singleton: `get_engine` is a
    # @staticmethod, so setting it on the instance leaves a permanent
    # instance-level shadow after monkeypatch "restores" it, silently breaking
    # every later test that patches the class (see test_category_scan_
    # reconciliation._patch_engine for the same note).
    monkeypatch.setattr(
        type(engine_factory),
        "get_engine",
        staticmethod(lambda brand: SimpleNamespace(get_pdp_product=_boom)),
    )

    await service.run_price_cycle(
        config.job_id, manager=JobManager(tmp_path / "claims-ledger")
    )

    assert len(config.check_runs) == CHECK_RUNS_HISTORY_LIMIT
    # The oldest filler was evicted; the newest run survived.
    surviving = [run.logical_cycle_id for run in config.check_runs]
    assert filler_ids[0] not in surviving
    assert filler_ids[-1] in surviving
