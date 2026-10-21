"""Durable job lifecycle contract for Phase 07 Plan 02."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from core.models import (
    CategoryScanOrigin,
    CategoryScanRecord,
    JobExecutionRecord,
    JobExecutionStatus,
    JobProgress,
    SearchHistory,
)
from services import job_repository
from services.job_repository import JsonJobRepository


def _job(job_id: str, **changes: object) -> JobExecutionRecord:
    payload = {
        "job_id": job_id,
        "kind": "search",
        "input": {"query": f"query-{job_id}", "brands": ["aramis"]},
    }
    payload.update(changes)
    return JobExecutionRecord.model_validate(payload)


def test_legacy_models_receive_additive_job_defaults_without_losing_fields():
    record = JobExecutionRecord.model_validate(
        {
            "job_id": "legacy-1",
            "query": "camisa",
            "status": "RUNNING",
            "results": [{"sku": "sku-1"}],
        }
    )

    dumped = record.model_dump(mode="json")
    assert dumped["query"] == "camisa"
    assert dumped["results"] == [{"sku": "sku-1"}]
    assert record.kind == "legacy"
    assert record.input == {}
    assert record.attempt == 1
    assert record.status == JobExecutionStatus.RUNNING

    search = SearchHistory(job_id="search-1", query="camisa")
    assert search.attempt == 1
    assert search.progress.completed_units == 0
    assert search.input == {"query": "camisa", "type": "search", "brands": []}

    scan = CategoryScanRecord(
        monitor_id="monitor-1",
        brand="aramis",
        category_url="https://example.test/camisas",
        origin=CategoryScanOrigin.MANUAL,
    )
    assert scan.attempt == 1
    assert scan.progress.total_units is None
    assert scan.input["monitor_id"] == "monitor-1"


def test_progress_without_total_keeps_percent_null_and_invalid_percentages_fail():
    progress = JobProgress(stage="scraping", completed_units=3)
    assert progress.total_units is None
    assert progress.percent is None

    with pytest.raises(ValidationError):
        JobProgress(completed_units=1, percent=10)

    with pytest.raises(ValidationError):
        JobProgress(completed_units=1, total_units=2, percent=101)

    with pytest.raises(ValidationError):
        JobProgress(completed_units=3, total_units=2, percent=100)


def test_repository_round_trip_persists_reproducible_input_progress_and_utc_timestamps(
    tmp_path: Path,
):
    path = tmp_path / "jobs.json"
    repository = JsonJobRepository(path)
    repository.add(_job("job-1"))

    running = repository.transition(
        "job-1",
        attempt=1,
        expected_statuses={JobExecutionStatus.PENDING},
        next_status=JobExecutionStatus.RUNNING,
        patch={
            "progress": {
                "stage": "brands",
                "completed_units": 2,
                "total_units": 4,
                "percent": 50,
                "message": "2 of 4 brands",
            }
        },
    )
    completed = repository.transition(
        "job-1",
        attempt=1,
        expected_statuses={JobExecutionStatus.RUNNING},
        next_status=JobExecutionStatus.COMPLETED,
        patch={"result": {"products": [{"sku": "sku-1"}]}},
    )

    reloaded = JsonJobRepository(path).get("job-1")
    assert running is not None
    assert completed is not None
    assert reloaded is not None
    assert reloaded.input == {"query": "query-job-1", "brands": ["aramis"]}
    assert reloaded.progress.percent == 50
    assert reloaded.result == {"products": [{"sku": "sku-1"}]}
    assert reloaded.error is None
    assert reloaded.started_at is not None
    assert reloaded.completed_at is not None
    for timestamp in (
        reloaded.created_at,
        reloaded.started_at,
        reloaded.updated_at,
        reloaded.completed_at,
    ):
        assert timestamp is not None
        assert timestamp.utcoffset() == timedelta(0)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk[0]["input"]["query"] == "query-job-1"
    assert on_disk[0]["status"] == "completed"


def test_terminal_is_absorbing_and_old_attempt_callback_cannot_mutate_current_terminal(
    tmp_path: Path,
):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(_job("job-1"))
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.PENDING},
        JobExecutionStatus.RUNNING,
    )
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.RUNNING},
        JobExecutionStatus.INTERRUPTED,
        {"error": "worker stopped"},
    )

    resumed = repository.start_new_attempt("job-1", previous_attempt=1)
    assert resumed is not None
    assert resumed.attempt == 2
    assert resumed.status == JobExecutionStatus.PENDING
    assert resumed.attempts[0].status == JobExecutionStatus.INTERRUPTED

    assert repository.transition(
        "job-1",
        2,
        {JobExecutionStatus.PENDING},
        JobExecutionStatus.RUNNING,
    )
    assert repository.transition(
        "job-1",
        2,
        {JobExecutionStatus.RUNNING},
        JobExecutionStatus.COMPLETED,
        {"result": {"ok": True}},
    )
    assert repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.RUNNING},
        JobExecutionStatus.FAILED,
        {"error": "late callback"},
    ) is None
    assert repository.transition(
        "job-1",
        2,
        {JobExecutionStatus.COMPLETED},
        JobExecutionStatus.FAILED,
        {"error": "double terminal"},
    ) is None

    current = repository.get("job-1")
    assert current is not None
    assert current.attempt == 2
    assert current.status == JobExecutionStatus.COMPLETED
    assert current.result == {"ok": True}
    assert [attempt.status for attempt in current.attempts] == [
        JobExecutionStatus.INTERRUPTED,
        JobExecutionStatus.COMPLETED,
    ]


def test_public_error_is_sanitized_and_traceback_is_not_persisted(tmp_path: Path):
    repository = JsonJobRepository(tmp_path / "jobs.json")
    repository.add(_job("job-1"))
    repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.PENDING},
        JobExecutionStatus.RUNNING,
    )
    failed = repository.transition(
        "job-1",
        1,
        {JobExecutionStatus.RUNNING},
        JobExecutionStatus.FAILED,
        {
            "error": (
                "ValueError: upstream rejected request\n"
                "Traceback (most recent call last):\n"
                '  File "C:/secret/worker.py", line 10, in run'
            )
        },
    )

    assert failed is not None
    assert failed.error == "ValueError: upstream rejected request"
    assert "\n" not in failed.error
    assert "C:/secret" not in failed.error
    assert len(failed.error) <= 500


def test_replace_failure_keeps_previous_json_parseable(tmp_path: Path, monkeypatch):
    path = tmp_path / "jobs.json"
    repository = JsonJobRepository(path)
    repository.add(_job("original"))
    before = json.loads(path.read_text(encoding="utf-8"))

    monkeypatch.setattr(
        job_repository.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("fixture failure")),
    )
    with pytest.raises(OSError, match="fixture failure"):
        repository.transition(
            "original",
            1,
            {JobExecutionStatus.PENDING},
            JobExecutionStatus.RUNNING,
        )

    assert json.loads(path.read_text(encoding="utf-8")) == before


def test_two_repository_instances_do_not_lose_updates_for_distinct_jobs(tmp_path: Path):
    path = tmp_path / "jobs.json"
    seed = JsonJobRepository(path)
    seed.add(_job("job-a"))
    seed.add(_job("job-b"))
    barrier = Barrier(2)

    def move_to_running(job_id: str) -> None:
        repository = JsonJobRepository(path)
        barrier.wait(timeout=5)
        result = repository.transition(
            job_id,
            1,
            {JobExecutionStatus.PENDING},
            JobExecutionStatus.RUNNING,
            {"progress": {"stage": job_id, "completed_units": 1}},
        )
        assert result is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(move_to_running, "job-a"),
            executor.submit(move_to_running, "job-b"),
        ]
        for future in futures:
            future.result(timeout=10)

    jobs = {job.job_id: job for job in JsonJobRepository(path).list_jobs()}
    assert set(jobs) == {"job-a", "job-b"}
    assert jobs["job-a"].status == JobExecutionStatus.RUNNING
    assert jobs["job-b"].status == JobExecutionStatus.RUNNING
    assert jobs["job-a"].progress.stage == "job-a"
    assert jobs["job-b"].progress.stage == "job-b"
