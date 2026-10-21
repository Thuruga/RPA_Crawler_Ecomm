"""Process-safe ownership and lifecycle management for durable jobs."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from filelock import AsyncFileLock, Timeout

from core.models import JobExecutionStatus


logger = logging.getLogger(__name__)

# Compatibility seam used by existing routes/services while ownership moves here.
JOB_CANCEL_FLAGS: Dict[str, asyncio.Event] = {}

CoroutineFactory = Callable[[], Awaitable[Any]]
ResumeFactory = Callable[[str, dict[str, Any]], Awaitable[Any]]
_LOGICAL_CYCLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,511}$")


@dataclass(slots=True)
class JobClaim:
    """An acquired logical-cycle claim held until the worker has fully exited."""

    logical_cycle_id: str
    lock: AsyncFileLock
    manager: "JobManager"
    released: bool = False

    async def release(self) -> None:
        await self.manager._release_claim(self)


class JobManager:
    """Own background tasks and coordinate one executor per logical cycle."""

    def __init__(self, claim_dir: Path | str | None = None):
        default_dir = Path(__file__).resolve().parents[1] / "data" / ".job-claims"
        self.claim_dir = Path(claim_dir or default_dir).resolve()
        self._claims: dict[str, JobClaim] = {}
        self._claim_guard = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._tasks_by_record: dict[str, asyncio.Task[Any]] = {}
        self._record_by_task: dict[asyncio.Task[Any], str] = {}
        self._resume_factories: dict[str, ResumeFactory] = {}
        self._reconciled: set[tuple[str, str, int]] = set()
        self._reconcile_guard = asyncio.Lock()
        self._reconcile_sources: dict[str, Callable[[], Awaitable[Any]]] = {}
        self._startup_reconcile_guard = asyncio.Lock()
        self._closing = False

    @property
    def active_record_ids(self) -> frozenset[str]:
        return frozenset(self._tasks_by_record)

    @staticmethod
    def _validate_logical_cycle_id(logical_cycle_id: str) -> str:
        if not isinstance(logical_cycle_id, str):
            raise TypeError("logical_cycle_id must be a string")
        if logical_cycle_id != logical_cycle_id.strip():
            raise ValueError("logical_cycle_id must not contain surrounding whitespace")
        if not _LOGICAL_CYCLE_ID.fullmatch(logical_cycle_id):
            raise ValueError("logical_cycle_id must be a canonical persisted identity")
        return logical_cycle_id

    def _claim_path(self, logical_cycle_id: str) -> Path:
        digest = hashlib.sha256(logical_cycle_id.encode("utf-8")).hexdigest()
        return self.claim_dir / f"{digest}.lock"

    async def try_claim(self, logical_cycle_id: str) -> JobClaim | None:
        """Acquire a non-blocking process-local and cross-process claim."""

        logical_cycle_id = self._validate_logical_cycle_id(logical_cycle_id)
        if self._closing:
            return None

        async with self._claim_guard:
            if logical_cycle_id in self._claims:
                return None
            self.claim_dir.mkdir(parents=True, exist_ok=True)
            lock = AsyncFileLock(
                str(self._claim_path(logical_cycle_id)),
                timeout=0,
                blocking=False,
            )
            try:
                await lock.acquire(blocking=False)
            except Timeout:
                return None
            claim = JobClaim(logical_cycle_id, lock, self)
            self._claims[logical_cycle_id] = claim
            return claim

    async def schedule(
        self,
        logical_cycle_id: str,
        coroutine_factory: CoroutineFactory,
        *,
        record_id: str,
    ) -> asyncio.Task[Any] | None:
        """Claim a stable identity before creating any externally-visible work."""

        if not callable(coroutine_factory):
            raise TypeError("coroutine_factory must be callable")
        claim = await self.try_claim(logical_cycle_id)
        if claim is None:
            return None
        return await self._schedule_claimed(claim, coroutine_factory, record_id=record_id)

    async def _schedule_claimed(
        self,
        claim: JobClaim,
        coroutine_factory: CoroutineFactory,
        *,
        record_id: str,
    ) -> asyncio.Task[Any] | None:
        if self._closing:
            await claim.release()
            return None
        if record_id in self._tasks_by_record or record_id in JOB_CANCEL_FLAGS:
            await claim.release()
            raise ValueError(f"record already scheduled: {record_id}")

        cancel_event = asyncio.Event()
        JOB_CANCEL_FLAGS[record_id] = cancel_event

        async def run_claimed() -> Any:
            try:
                return await coroutine_factory()
            finally:
                await claim.release()

        task = asyncio.create_task(run_claimed(), name=f"job:{record_id}")
        self._tasks.add(task)
        self._tasks_by_record[record_id] = task
        self._record_by_task[task] = record_id
        task.add_done_callback(self._finalize_task)
        return task

    def _finalize_task(self, task: asyncio.Task[Any]) -> None:
        record_id = self._record_by_task.pop(task, None)
        self._tasks.discard(task)
        if record_id is not None:
            if self._tasks_by_record.get(record_id) is task:
                self._tasks_by_record.pop(record_id, None)
            JOB_CANCEL_FLAGS.pop(record_id, None)

        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.error(
                "Background job %s failed: %s",
                record_id or task.get_name(),
                exception,
                exc_info=(type(exception), exception, exception.__traceback__),
            )

    async def _release_claim(self, claim: JobClaim) -> None:
        if claim.released:
            return
        claim.released = True
        try:
            await claim.lock.release(force=True)
        finally:
            async with self._claim_guard:
                if self._claims.get(claim.logical_cycle_id) is claim:
                    self._claims.pop(claim.logical_cycle_id, None)

    def register_resume_factory(self, kind: str, factory: ResumeFactory) -> None:
        """Register the zero-based resume implementation for one persisted job kind."""

        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("resume factory kind must be a non-empty string")
        if not callable(factory):
            raise TypeError("resume factory must be callable")
        self._resume_factories[kind] = factory

    @staticmethod
    def _repository_identity(repository: Any) -> str:
        path = getattr(repository, "path", None)
        if path is not None:
            return str(Path(path).resolve())
        return f"repository:{id(repository)}"

    def register_reconcile_source(
        self, kind: str, reconciler: Callable[[], Awaitable[Any]]
    ) -> None:
        """Register the boot-time orphan reconciler for one persisted job kind.

        `startup_reconcile` visits every registered kind exactly once per
        call, in a deterministic (sorted) order, so one kind can never
        silently starve the others (D-01/T-07-21). Registering a non-callable
        or an empty kind name fails immediately here rather than silently
        no-op-ing at reconcile time.
        """

        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("reconcile source kind must be a non-empty string")
        if not callable(reconciler):
            raise TypeError("reconcile source must be callable")
        self._reconcile_sources[kind] = reconciler

    async def startup_reconcile(self) -> dict[str, Any]:
        """Reconcile every registered job kind exactly once per lifespan call.

        Concurrent/duplicate callers (e.g. two workers racing app startup)
        are serialized by `_startup_reconcile_guard`; the underlying claim
        (`try_claim`) and per-record CAS machinery are themselves idempotent,
        so a second full pass — whether concurrent or a later sequential
        call — safely finds nothing left to resume rather than needing an
        artificial once-per-process flag. A kind whose reconciler raises is
        logged with its own kind identified (T-07-22) and never blocks the
        remaining kinds from running.
        """

        async with self._startup_reconcile_guard:
            results: dict[str, Any] = {}
            for kind in sorted(self._reconcile_sources):
                reconciler = self._reconcile_sources[kind]
                try:
                    results[kind] = await reconciler()
                except Exception:
                    logger.exception("startup_reconcile: kind=%s failed", kind)
                    results[kind] = None
            return results

    async def reconcile(
        self,
        repository: Any,
        resume_factory: ResumeFactory | None = None,
    ) -> list[asyncio.Task[Any]]:
        """Interrupt and resume each orphan once after winning its logical claim."""

        scheduled: list[asyncio.Task[Any]] = []
        async with self._reconcile_guard:
            try:
                records = repository.list_jobs()
            except Exception:
                # A single corrupt/invalid persisted record must not crash
                # the whole boot reconciliation pass (T-07-22): log which
                # repository is unreadable and let the other registered
                # kinds still run via `startup_reconcile`.
                logger.exception(
                    "reconcile: repository %s returned an invalid/unreadable "
                    "document; skipping this source",
                    self._repository_identity(repository),
                )
                return scheduled
            for record in records:
                if record.status not in {
                    JobExecutionStatus.PENDING,
                    JobExecutionStatus.RUNNING,
                }:
                    continue

                logical_cycle_id = getattr(record, "logical_cycle_id", None)
                factory = resume_factory or self._resume_factories.get(record.kind)
                if not logical_cycle_id:
                    logger.error(
                        "Skipping orphan %s without persisted logical_cycle_id",
                        record.job_id,
                    )
                    continue
                if factory is None:
                    logger.error(
                        "Skipping orphan %s without resume factory for kind %s",
                        record.job_id,
                        record.kind,
                    )
                    continue

                reconciliation_key = (
                    self._repository_identity(repository),
                    record.job_id,
                    record.attempt,
                )
                if reconciliation_key in self._reconciled:
                    continue

                claim = await self.try_claim(logical_cycle_id)
                if claim is None:
                    # Another process owns this cycle. It alone may reclassify it.
                    continue

                interrupted = repository.transition(
                    record.job_id,
                    record.attempt,
                    {record.status},
                    JobExecutionStatus.INTERRUPTED,
                    {"error": "worker interrupted before boot reconciliation"},
                )
                if interrupted is None:
                    await claim.release()
                    continue

                resumed = repository.start_new_attempt(
                    record.job_id,
                    previous_attempt=record.attempt,
                    input=dict(record.input),
                )
                if resumed is None:
                    await claim.release()
                    continue

                async def resume_claimed(
                    *,
                    job_id: str = record.job_id,
                    attempt: int = resumed.attempt,
                    persisted_input: dict[str, Any] = dict(record.input),
                    selected_factory: ResumeFactory = factory,
                ) -> Any:
                    running = repository.transition(
                        job_id,
                        attempt,
                        {JobExecutionStatus.PENDING},
                        JobExecutionStatus.RUNNING,
                    )
                    if running is None:
                        return None
                    return await selected_factory(job_id, dict(persisted_input))

                try:
                    task = await self._schedule_claimed(
                        claim,
                        resume_claimed,
                        record_id=record.job_id,
                    )
                except BaseException:
                    await claim.release()
                    raise
                if task is None:
                    continue
                self._reconciled.add(reconciliation_key)
                scheduled.append(task)
        return scheduled

    async def cancel(self, record_id: str) -> bool:
        """Request cancellation and wait until the worker's cleanup is complete."""

        cancel_event = JOB_CANCEL_FLAGS.get(record_id)
        task = self._tasks_by_record.get(record_id)
        if cancel_event is None and task is None:
            return False
        if cancel_event is not None:
            cancel_event.set()
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        return True

    async def shutdown(self) -> None:
        """Stop accepting work, cancel workers, and release every owned claim."""

        self._closing = True
        tasks = tuple(self._tasks)
        for task in tasks:
            record_id = self._record_by_task.get(task)
            if record_id is not None:
                event = JOB_CANCEL_FLAGS.get(record_id)
                if event is not None:
                    event.set()
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        for claim in tuple(self._claims.values()):
            await claim.release()


job_manager = JobManager()
