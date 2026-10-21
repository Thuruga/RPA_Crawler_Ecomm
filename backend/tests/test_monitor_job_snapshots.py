"""HTTP integration for JOB-01 authenticated snapshots (Phase 07 Plan 12).

Category scan (Task 1) and price-check `check_runs` (Task 2) already persist
their full attempt/run lifecycle durably (Plan 07-05/07-06). This suite
proves the *existing* authenticated monitor routes expose that lifecycle
honestly over HTTP: X-API-Key gates every snapshot, nullable progress and
INTERRUPTED survive the response-model round-trip, and a missing monitor/job
never leaks another monitor's content nor invents a job.

100% hermetic: no real category_scans.json/price_monitors.json I/O — both
`list_category_scans` (routes_monitor) and `monitor_service.monitors` (routes_product)
are monkeypatched per-test.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import api_router
import api.routes_monitor as routes_monitor
import api.routes_product as routes_product
from config import settings
from core.models import (
    CategoryScanOrigin,
    CategoryScanStatus,
    PriceCheckRun,
    PriceCheckState,
)

API_KEY = "test-monitor-snapshot-key"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", API_KEY)
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def _scan_row(**overrides) -> dict:
    row = {
        "scan_id": "scan-1",
        "monitor_id": "monitor-1",
        "brand": "aramis",
        "category_url": "https://example.test/camisas",
        "origin": CategoryScanOrigin.MANUAL.value,
        "status": CategoryScanStatus.RUNNING.value,
        "kind": "category_scan",
        "input": {},
        "attempt": 1,
        "progress": {
            "stage": "listing",
            "completed_units": 2,
            "total_units": None,
            "percent": None,
            "message": None,
        },
        "attempts": [],
        "created_at": "2026-07-26T20:00:00+00:00",
        "started_at": "2026-07-26T20:00:01+00:00",
        "updated_at": "2026-07-26T20:00:02+00:00",
        "completed_at": None,
        "error_message": None,
        "product_count": 2,
        "logical_cycle_id": "category_scan:monitor-1:scan-1",
        "scheduled_for": None,
    }
    row.update(overrides)
    return row


def _async_rows(rows: list[dict]):
    """Async stand-in for `list_category_scans`, which the route awaits.

    The service function is `async def` now (it reads scans from Postgres), so
    a plain lambda double makes the route await a list and blow up.
    """

    async def _list(monitor_id=None):
        return rows

    return _list


class TestCategoryScanSnapshots:
    """Task 1: GET /monitor/category/{monitor_id}/scans."""

    def test_missing_header_rejects_before_returning_snapshot(self, client, monkeypatch):
        async def _unexpected(monitor_id=None):
            raise AssertionError("list_category_scans must not run without a valid header")

        monkeypatch.setattr(routes_monitor, "list_category_scans", _unexpected)
        response = client.get("/monitor/category/monitor-1/scans")
        # IAM-01: requisições sem credenciais válidas na api_router agora retornam 401 (UNAUTHENTICATED)
        assert response.status_code == 401

    def test_invalid_header_rejects_before_returning_snapshot(self, client, monkeypatch):
        calls: list[str | None] = []

        async def _spy(monitor_id=None):
            calls.append(monitor_id)
            return [_scan_row()]

        monkeypatch.setattr(routes_monitor, "list_category_scans", _spy)
        response = client.get(
            "/monitor/category/monitor-1/scans", headers={"X-API-Key": "wrong-key"}
        )
        # IAM-01: chave inválida sem token Bearer retorna 401 (UNAUTHENTICATED)
        assert response.status_code == 401
        assert calls == []

    def test_valid_header_returns_full_category_scan_snapshot(self, client, monkeypatch):
        monkeypatch.setattr(
            routes_monitor, "list_category_scans", _async_rows([_scan_row()])
        )
        response = client.get(
            "/monitor/category/monitor-1/scans", headers={"X-API-Key": API_KEY}
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        scan = body[0]
        assert scan["status"] == "running"
        assert scan["attempt"] == 1
        assert scan["progress"]["completed_units"] == 2
        assert scan["progress"]["total_units"] is None
        assert scan["progress"]["percent"] is None
        assert scan["logical_cycle_id"] == "category_scan:monitor-1:scan-1"
        assert scan["created_at"] is not None

    def test_interrupted_status_and_result_survive_serialization(self, client, monkeypatch):
        row = _scan_row(
            status=CategoryScanStatus.INTERRUPTED.value,
            product_count=5,
            error_message=None,
        )
        monkeypatch.setattr(
            routes_monitor, "list_category_scans", _async_rows([row])
        )
        response = client.get(
            "/monitor/category/monitor-1/scans", headers={"X-API-Key": API_KEY}
        )
        assert response.status_code == 200
        scan = response.json()[0]
        assert scan["status"] == "interrupted"
        assert scan["status"] != "failed"
        assert scan["product_count"] == 5

    def test_sanitized_error_survives_serialization(self, client, monkeypatch):
        row = _scan_row(
            status=CategoryScanStatus.FAILED.value,
            error_message="anti-bot bloqueou a listagem",
        )
        monkeypatch.setattr(
            routes_monitor, "list_category_scans", _async_rows([row])
        )
        response = client.get(
            "/monitor/category/monitor-1/scans", headers={"X-API-Key": API_KEY}
        )
        assert response.status_code == 200
        assert response.json()[0]["error_message"] == "anti-bot bloqueou a listagem"

    def test_nonexistent_monitor_returns_empty_list_without_inventing_job(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            routes_monitor, "list_category_scans", _async_rows([])
        )
        response = client.get(
            "/monitor/category/does-not-exist/scans", headers={"X-API-Key": API_KEY}
        )
        assert response.status_code == 200
        assert response.json() == []


def _price_config(**overrides):
    from core.models import PriceMonitorConfig

    base = dict(job_id="job-1", url="https://example.test/produto", brand="aramis")
    base.update(overrides)
    return PriceMonitorConfig(**base)


class TestPriceCheckRunSnapshots:
    """Task 2: GET /monitors and GET /monitor/history/{job_id}."""

    def test_list_monitors_missing_header_rejects_before_returning_snapshot(self, client):
        response = client.get("/monitors")
        # IAM-01: requisições sem credenciais válidas retornam 401
        assert response.status_code == 401

    def test_list_monitors_invalid_header_rejects_before_returning_snapshot(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            routes_product.monitor_service, "monitors", {"job-1": _price_config()}
        )
        response = client.get("/monitors", headers={"X-API-Key": "wrong-key"})
        # IAM-01: chave inválida sem token Bearer retorna 401
        assert response.status_code == 401

    def test_list_monitors_includes_check_runs_with_nullable_in_flight_progress(
        self, client, monkeypatch
    ):
        in_flight_run = PriceCheckRun(
            logical_cycle_id="price_check:job-1:2026-07-26T20:10:00+00:00",
            scheduled_for=datetime(2026, 7, 26, 20, 10, tzinfo=timezone.utc),
            status=None,
        )
        config = _price_config(check_runs=[in_flight_run])
        monkeypatch.setattr(routes_product.monitor_service, "monitors", {"job-1": config})
        response = client.get("/monitors", headers={"X-API-Key": API_KEY})
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        [wire_run] = body[0]["check_runs"]
        assert wire_run["run_id"] == in_flight_run.run_id
        assert wire_run["status"] is None
        assert wire_run["completed_at"] is None

    def test_history_detail_keeps_ok_blocked_error_stale_interrupted_distinct(
        self, client, monkeypatch
    ):
        terminal_states = [
            PriceCheckState.OK,
            PriceCheckState.BLOCKED,
            PriceCheckState.ERROR,
            PriceCheckState.STALE,
            PriceCheckState.INTERRUPTED,
        ]
        runs = [
            PriceCheckRun(
                logical_cycle_id=f"price_check:job-1:cycle-{index}",
                scheduled_for=datetime(2026, 7, 26, 20, index, tzinfo=timezone.utc),
                status=state,
                error="sanitized failure" if state == PriceCheckState.ERROR else None,
            )
            for index, state in enumerate(terminal_states)
        ]
        config = _price_config(check_runs=runs)
        monkeypatch.setattr(routes_product.monitor_service, "monitors", {"job-1": config})
        response = client.get("/monitor/history/job-1", headers={"X-API-Key": API_KEY})
        assert response.status_code == 200
        body = response.json()
        statuses = [run["status"] for run in body["check_runs"]]
        assert statuses == ["ok", "blocked", "error", "stale", "interrupted"]
        error_run = next(run for run in body["check_runs"] if run["status"] == "error")
        assert error_run["error"] == "sanitized failure"

    def test_history_detail_404_for_unknown_job_does_not_leak_other_monitor(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            routes_product.monitor_service,
            "monitors",
            {"job-1": _price_config(url="https://example.test/secret-produto")},
        )
        response = client.get(
            "/monitor/history/unknown-job", headers={"X-API-Key": API_KEY}
        )
        assert response.status_code == 404
        assert "secret-produto" not in response.text
        assert "job-1" not in response.text

    def test_ledger_does_not_apply_implicit_retention(self, client, monkeypatch):
        runs = [
            PriceCheckRun(
                logical_cycle_id=f"price_check:job-1:cycle-{index}",
                scheduled_for=datetime(2026, 7, 26, 20, index, tzinfo=timezone.utc),
                status=PriceCheckState.OK,
            )
            for index in range(12)
        ]
        config = _price_config(check_runs=runs)
        monkeypatch.setattr(routes_product.monitor_service, "monitors", {"job-1": config})
        response = client.get("/monitor/history/job-1", headers={"X-API-Key": API_KEY})
        assert response.status_code == 200
        assert len(response.json()["check_runs"]) == 12
