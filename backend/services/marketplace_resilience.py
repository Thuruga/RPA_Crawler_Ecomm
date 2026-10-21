"""Coordenacao compartilhada e persistente para leituras de marketplaces."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


BASE_INTERVAL = timedelta(hours=12)
MAX_JITTER_SECONDS = 60 * 60
MAX_FORBIDDEN_BACKOFF = timedelta(hours=72)


class MarketplaceHttpError(RuntimeError):
    def __init__(self, status_code: int, retry_after_seconds: int | None = None):
        super().__init__(f"marketplace HTTP {status_code}")
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class MarketplaceUnavailable(RuntimeError):
    def __init__(self, state: str, next_retry_at: str | None = None):
        super().__init__(state)
        self.state = state
        self.next_retry_at = next_retry_at


@dataclass
class ResilientRead:
    value: Any
    state: str
    cache_hit: bool
    last_success_at: str | None
    next_retry_at: str | None


class MarketplaceCoordinator:
    """Serializa chamadas externas e compartilha resultados por origem."""

    def __init__(
        self,
        state_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[], int] | None = None,
    ) -> None:
        self.state_path = state_path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jitter = jitter or (lambda: random.randint(0, MAX_JITTER_SECONDS))
        self._semaphore = asyncio.Semaphore(1)
        self._pending: dict[str, asyncio.Task[ResilientRead]] = {}
        self._pending_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()

    async def execute(
        self,
        marketplace: str,
        source: str,
        loader: Callable[[], Awaitable[Any]],
        *,
        unsupported: bool = False,
    ) -> ResilientRead:
        key = self._key(marketplace, source)
        async with self._pending_lock:
            existing = self._pending.get(key)
            if existing is not None:
                task = existing
            else:
                task = asyncio.create_task(
                    self._execute_once(key, marketplace, loader, unsupported=unsupported)
                )
                self._pending[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._pending_lock:
                    if self._pending.get(key) is task:
                        self._pending.pop(key, None)

    async def _execute_once(
        self,
        key: str,
        marketplace: str,
        loader: Callable[[], Awaitable[Any]],
        *,
        unsupported: bool,
    ) -> ResilientRead:
        now = self._utc(self.clock())
        async with self._state_lock:
            state = self._load_state()
            entry = state["entries"].get(key)
            circuit = state["circuits"].get(marketplace, {})

        if unsupported:
            return self._cached_read(entry, "unsupported", now)

        blocked_until = self._parse_dt(circuit.get("blocked_until"))
        if blocked_until and blocked_until > now:
            if entry and entry.get("value") is not None:
                return self._cached_read(entry, "stale", now, blocked_until)
            raise MarketplaceUnavailable("blocked", blocked_until.isoformat())

        next_due = self._parse_dt(entry.get("next_due_at")) if entry else None
        if entry and entry.get("value") is not None and next_due and next_due > now:
            return self._cached_read(entry, "current", now, next_due)

        try:
            async with self._semaphore:
                value = await loader()
        except MarketplaceHttpError as exc:
            blocked_until = await self._record_failure(marketplace, exc, now)
            if entry and entry.get("value") is not None:
                return self._cached_read(entry, "stale", now, blocked_until)
            raise MarketplaceUnavailable("blocked", blocked_until.isoformat()) from exc

        success_at = now.isoformat()
        next_due_at = now + BASE_INTERVAL + timedelta(seconds=max(0, self.jitter()))
        async with self._state_lock:
            state = self._load_state()
            state["entries"][key] = {
                "value": value,
                "last_success_at": success_at,
                "next_due_at": next_due_at.isoformat(),
            }
            state["circuits"][marketplace] = {
                "forbidden_failures": 0,
                "blocked_until": None,
            }
            self._save_state(state)
        return ResilientRead(value, "current", False, success_at, next_due_at.isoformat())

    async def _record_failure(
        self,
        marketplace: str,
        error: MarketplaceHttpError,
        now: datetime,
    ) -> datetime:
        async with self._state_lock:
            state = self._load_state()
            circuit = state["circuits"].get(marketplace, {})
            failures = int(circuit.get("forbidden_failures") or 0)
            if error.status_code == 403:
                failures += 1
                delay = min(timedelta(hours=12 * failures), MAX_FORBIDDEN_BACKOFF)
            elif error.status_code == 429:
                delay = timedelta(seconds=error.retry_after_seconds or 24 * 60 * 60)
            else:
                delay = BASE_INTERVAL
            blocked_until = now + delay
            state["circuits"][marketplace] = {
                "forbidden_failures": failures,
                "blocked_until": blocked_until.isoformat(),
            }
            self._save_state(state)
            return blocked_until

    def _cached_read(
        self,
        entry: dict[str, Any] | None,
        state: str,
        now: datetime,
        next_retry_at: datetime | None = None,
    ) -> ResilientRead:
        if not entry or entry.get("value") is None:
            raise MarketplaceUnavailable(state, next_retry_at.isoformat() if next_retry_at else None)
        retry = next_retry_at or self._parse_dt(entry.get("next_due_at"))
        return ResilientRead(
            entry["value"],
            state,
            True,
            entry.get("last_success_at"),
            retry.isoformat() if retry else None,
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("entries", {})
                payload.setdefault("circuits", {})
                return payload
        except (OSError, ValueError, TypeError):
            pass
        return {"entries": {}, "circuits": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    @staticmethod
    def _key(marketplace: str, source: str) -> str:
        return f"{marketplace.strip().lower()}::{source.strip()}"

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return MarketplaceCoordinator._utc(datetime.fromisoformat(str(value)))
        except ValueError:
            return None

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)



marketplace_coordinator = MarketplaceCoordinator(
    Path(__file__).resolve().parents[1] / "data" / "marketplace_resilience.json"
)
