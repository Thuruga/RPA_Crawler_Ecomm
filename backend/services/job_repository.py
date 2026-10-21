"""Attempt-aware JSON repository with cross-process locking and atomic publish."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from filelock import FileLock
from pydantic import TypeAdapter

from core.models import (
    JobAttemptRecord,
    JobExecutionRecord,
    JobExecutionStatus,
    JobProgress,
)


_JOB_DOCUMENT = TypeAdapter(list[JobExecutionRecord])
_TERMINAL = {
    JobExecutionStatus.COMPLETED,
    JobExecutionStatus.FAILED,
    JobExecutionStatus.INTERRUPTED,
}
_ALLOWED_TRANSITIONS = {
    JobExecutionStatus.PENDING: {
        JobExecutionStatus.RUNNING,
        JobExecutionStatus.FAILED,
        JobExecutionStatus.INTERRUPTED,
    },
    JobExecutionStatus.RUNNING: _TERMINAL,
}
_SENSITIVE_RESULT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "traceback",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sanitize_public_error(value: Any) -> str | None:
    if value is None:
        return None
    first_line = str(value).splitlines()[0].strip()
    return " ".join(first_line.split())[:500] or None


def _sanitize_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if str(key).casefold() in _SENSITIVE_RESULT_KEYS
                else _sanitize_result(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_result(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_result(item) for item in value]
    return value


class JsonJobRepository:
    """Validate and replace one canonical JSON job document per transaction."""

    def __init__(self, path: Path | str, *, lock_timeout: float = 30.0):
        self.path = Path(path).resolve()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self._file_lock = FileLock(str(self.lock_path), timeout=lock_timeout)
        self._thread_lock = threading.RLock()

    def list_jobs(self) -> list[JobExecutionRecord]:
        with self._transaction():
            return [
                JobExecutionRecord.model_validate(item.model_dump(mode="python"))
                for item in self._read_unlocked()
            ]

    def get(self, job_id: str) -> JobExecutionRecord | None:
        with self._transaction():
            record = self._find(self._read_unlocked(), job_id)
            if record is None:
                return None
            return JobExecutionRecord.model_validate(record.model_dump(mode="python"))

    def add(self, record: JobExecutionRecord | dict[str, Any]) -> JobExecutionRecord:
        candidate = JobExecutionRecord.model_validate(record)
        with self._transaction():
            records = self._read_unlocked()
            if self._find(records, candidate.job_id) is not None:
                raise ValueError(f"job already exists: {candidate.job_id}")
            records.append(candidate)
            self._write_unlocked(records)
            return JobExecutionRecord.model_validate(candidate.model_dump(mode="python"))

    def replace(self, record: JobExecutionRecord | dict[str, Any]) -> JobExecutionRecord:
        candidate = JobExecutionRecord.model_validate(record)
        with self._transaction():
            records = self._read_unlocked()
            index = self._index(records, candidate.job_id)
            if index is None:
                records.append(candidate)
            else:
                records[index] = candidate
            self._write_unlocked(records)
            return JobExecutionRecord.model_validate(candidate.model_dump(mode="python"))

    def transition(
        self,
        job_id: str,
        attempt: int,
        expected_statuses: Iterable[JobExecutionStatus | str],
        next_status: JobExecutionStatus | str,
        patch: dict[str, Any] | None = None,
    ) -> JobExecutionRecord | None:
        expected = {self._status(value) for value in expected_statuses}
        target = self._status(next_status)
        changes = dict(patch or {})

        with self._transaction():
            records = self._read_unlocked()
            index = self._index(records, job_id)
            if index is None:
                return None
            current = records[index]
            if current.attempt != attempt or current.status not in expected:
                return None
            if current.status in _TERMINAL:
                return None
            if target not in _ALLOWED_TRANSITIONS.get(current.status, set()):
                raise ValueError(
                    f"invalid job transition: {current.status.value}->{target.value}"
                )

            now = _utc_now()
            data = current.model_dump(mode="python")
            data["status"] = target
            data["updated_at"] = now

            if "progress" in changes:
                data["progress"] = JobProgress.model_validate(changes["progress"])
            if "input" in changes:
                data["input"] = dict(changes["input"])
            if "result" in changes:
                data["result"] = _sanitize_result(changes["result"])
            if "error" in changes:
                data["error"] = _sanitize_public_error(changes["error"])

            if target == JobExecutionStatus.RUNNING:
                data["started_at"] = current.started_at or now
                data["completed_at"] = None
                data["result"] = None
                data["error"] = None
            elif target in _TERMINAL:
                data["completed_at"] = now
                if target == JobExecutionStatus.COMPLETED:
                    data["error"] = None
                else:
                    data["result"] = None
                    data["error"] = _sanitize_public_error(data.get("error"))

            attempts = list(data.get("attempts") or [])
            attempt_index = next(
                (
                    position
                    for position, item in enumerate(attempts)
                    if int(item.get("attempt", 0)) == attempt
                ),
                None,
            )
            attempt_data = JobAttemptRecord(
                attempt=attempt,
                status=target,
                progress=data["progress"],
                created_at=current.attempts[-1].created_at
                if current.attempts
                else current.created_at,
                started_at=data.get("started_at"),
                updated_at=now,
                completed_at=data.get("completed_at"),
                result=data.get("result"),
                error=data.get("error"),
            ).model_dump(mode="python")
            if attempt_index is None:
                attempts.append(attempt_data)
            else:
                attempts[attempt_index] = attempt_data
            data["attempts"] = attempts

            updated = JobExecutionRecord.model_validate(data)
            records[index] = updated
            self._write_unlocked(records)
            return JobExecutionRecord.model_validate(updated.model_dump(mode="python"))

    def start_new_attempt(
        self,
        job_id: str,
        *,
        previous_attempt: int,
        input: dict[str, Any] | None = None,
    ) -> JobExecutionRecord | None:
        with self._transaction():
            records = self._read_unlocked()
            index = self._index(records, job_id)
            if index is None:
                return None
            current = records[index]
            if (
                current.attempt != previous_attempt
                or current.status != JobExecutionStatus.INTERRUPTED
            ):
                return None

            now = _utc_now()
            next_attempt = previous_attempt + 1
            attempts = [
                item.model_dump(mode="python")
                for item in current.attempts
            ]
            attempts.append(
                JobAttemptRecord(
                    attempt=next_attempt,
                    status=JobExecutionStatus.PENDING,
                    created_at=now,
                    updated_at=now,
                ).model_dump(mode="python")
            )
            data = current.model_dump(mode="python")
            data.update(
                {
                    "attempt": next_attempt,
                    "status": JobExecutionStatus.PENDING,
                    "progress": JobProgress(),
                    "started_at": None,
                    "updated_at": now,
                    "completed_at": None,
                    "result": None,
                    "error": None,
                    "attempts": attempts,
                }
            )
            if input is not None:
                data["input"] = dict(input)

            resumed = JobExecutionRecord.model_validate(data)
            records[index] = resumed
            self._write_unlocked(records)
            return JobExecutionRecord.model_validate(resumed.model_dump(mode="python"))

    def _read_unlocked(self) -> list[JobExecutionRecord]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return _JOB_DOCUMENT.validate_python(raw)

    def _write_unlocked(self, records: list[JobExecutionRecord]) -> None:
        validated = _JOB_DOCUMENT.validate_python(
            [record.model_dump(mode="python") for record in records]
        )
        payload = [
            record.model_dump(mode="json", exclude_none=False)
            for record in validated
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _transaction(self):
        repository = self

        class Transaction:
            def __enter__(self):
                repository._thread_lock.acquire()
                try:
                    repository._file_lock.acquire()
                except BaseException:
                    repository._thread_lock.release()
                    raise
                return repository

            def __exit__(self, exc_type, exc, traceback):
                repository._file_lock.release()
                repository._thread_lock.release()
                return False

        return Transaction()

    @staticmethod
    def _status(value: JobExecutionStatus | str) -> JobExecutionStatus:
        if isinstance(value, JobExecutionStatus):
            return value
        return JobExecutionStatus(str(value).casefold())

    @staticmethod
    def _find(
        records: list[JobExecutionRecord], job_id: str
    ) -> JobExecutionRecord | None:
        return next((record for record in records if record.job_id == job_id), None)

    @staticmethod
    def _index(records: list[JobExecutionRecord], job_id: str) -> int | None:
        return next(
            (
                index
                for index, record in enumerate(records)
                if record.job_id == job_id
            ),
            None,
        )
