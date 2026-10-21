"""Content-addressed storage and immutable lifecycle for banner runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from core.banner_models import (
    BannerHistorySummary,
    BannerRun,
    BannerRunStatus,
    StoredBannerAsset,
    utc_now_iso,
)


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data" / "banners"
MAX_ASSET_BYTES = 30 * 1024 * 1024
EXTENSION_BY_MIME = {
    "image/avif": "avif",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/webp": "webp",
}
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$")


def _slug(value: str, fallback: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value.lower()).strip("-")
    return clean[:60] or fallback


def friendly_banner_filename(
    order: int,
    description: str,
    brand: str,
    extension: str,
    captured_at: datetime | str | None = None,
) -> str:
    ext = extension.lower().lstrip(".")
    if ext not in set(EXTENSION_BY_MIME.values()):
        raise ValueError("unsupported banner extension")
    del description  # Kept in the signature for compatibility with existing callers.
    if isinstance(captured_at, str):
        captured_at = datetime.fromisoformat(captured_at)
    instant = captured_at or datetime.now(ZoneInfo("America/Sao_Paulo"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
    local_instant = instant.astimezone(ZoneInfo("America/Sao_Paulo"))
    return f"{local_instant:%m-%Y}-{_slug(brand, 'marca')}-{order:02d}.{ext}"


class BannerStorageService:
    def __init__(self, root: Path | str = DEFAULT_ROOT, max_asset_bytes: int = MAX_ASSET_BYTES):
        self.root = Path(root).resolve()
        self.assets_dir = self.root / "assets"
        self.runs_dir = self.root / "runs"
        self.reports_dir = self.root / "reports"
        self.max_asset_bytes = max_asset_bytes
        self._lock = threading.RLock()
        for directory in (self.assets_dir, self.runs_dir, self.reports_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        """Serialize a multi-step read/modify/write transaction for one process."""
        with self._lock:
            yield

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("invalid run id")
        return run_id

    @staticmethod
    def extension_for(content_type: str, supplied: str | None = None) -> str:
        mime = (content_type or "").split(";", 1)[0].strip().lower()
        expected = EXTENSION_BY_MIME.get(mime)
        candidate = (supplied or expected or "").lower().lstrip(".")
        if not expected or candidate not in set(EXTENSION_BY_MIME.values()):
            raise ValueError(f"unsupported banner content type: {content_type}")
        # MIME is authoritative; jpeg may be named jpeg but is stored as jpg.
        return expected

    def asset_path(self, digest: str, extension: str) -> Path:
        asset = StoredBannerAsset(
            sha256=digest,
            extension=extension,
            content_type=next((m for m, e in EXTENSION_BY_MIME.items() if e == extension), ""),
            byte_count=0,
        )
        if asset.extension not in set(EXTENSION_BY_MIME.values()):
            raise ValueError("unsupported banner extension")
        return self.assets_dir / f"{asset.sha256}.{asset.extension}"

    def store_asset(self, data: bytes, content_type: str, extension: str | None = None) -> StoredBannerAsset:
        if len(data) > self.max_asset_bytes:
            raise ValueError("banner asset exceeds configured byte limit")
        ext = self.extension_for(content_type, extension)
        digest = hashlib.sha256(data).hexdigest()
        target = self.asset_path(digest, ext)
        with self._lock:
            if not target.exists():
                self._atomic_bytes(target, data)
        return StoredBannerAsset(
            sha256=digest,
            extension=ext,
            content_type=content_type.split(";", 1)[0].lower(),
            byte_count=len(data),
        )

    def resolve_asset(self, asset: StoredBannerAsset) -> Path:
        path = self.asset_path(asset.sha256, asset.extension)
        if not path.is_file():
            raise FileNotFoundError(asset.sha256)
        return path

    def save_run(self, run: BannerRun) -> BannerRun:
        self._validate_run_id(run.run_id)
        with self._lock:
            existing = self.get_run(run.run_id)
            if existing and existing.status == BannerRunStatus.COMPLETED:
                if existing.model_dump(mode="json") != run.model_dump(mode="json"):
                    raise ValueError("completed banner runs are immutable")
                return existing
            run.updated_at = utc_now_iso()
            self._atomic_json(self.runs_dir / f"{run.run_id}.json", run.model_dump(mode="json"))
        return run

    def get_run(self, run_id: str) -> Optional[BannerRun]:
        self._validate_run_id(run_id)
        path = self.runs_dir / f"{run_id}.json"
        # A concurrent _atomic_json (os.replace) is not atomic for readers on the
        # Docker Desktop bind mount: the file can vanish for a moment between the
        # existence check and the read. Retry briefly before treating it as missing.
        for attempt in range(5):
            if not path.is_file() and attempt == 0:
                return None
            try:
                return BannerRun.model_validate_json(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                time.sleep(0.05)
        return None

    def list_runs(self) -> list[BannerRun]:
        runs: list[BannerRun] = []
        for path in self.runs_dir.glob("*.json"):
            try:
                runs.append(BannerRun.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        return runs

    def list_history(self) -> list[BannerHistorySummary]:
        self.cleanup_old_records()
        completed = [run for run in self.list_runs() if run.status == BannerRunStatus.COMPLETED]
        completed.sort(key=lambda run: run.approved_at or run.created_at, reverse=True)
        return [
            BannerHistorySummary(
                run_id=run.run_id,
                created_at=run.created_at,
                approved_at=run.approved_at or run.updated_at,
                banner_count=len(run.banners),
                brand_count=len({banner.brand_key for banner in run.banners}),
            )
            for run in completed
        ]

    def approve_run(self, run_id: str, selected_banner_ids: Iterable[str]) -> BannerRun:
        selected = set(selected_banner_ids)
        with self._lock:
            run = self.get_run(run_id)
            if not run:
                raise KeyError(run_id)
            if run.status != BannerRunStatus.REVIEW:
                raise ValueError("only review runs can be approved")
            if not selected and run.banners:
                raise ValueError("select at least one banner")
            known = {banner.banner_id for banner in run.banners}
            if not selected.issubset(known):
                raise ValueError("selection contains an unknown banner")
            run.banners = [
                banner.model_copy(update={"approved": True})
                for banner in run.banners
                if banner.banner_id in selected
            ]
            run.status = BannerRunStatus.COMPLETED
            run.approved_at = utc_now_iso()
            self.save_run(run)
            self.collect_orphan_assets()
            return run

    def delete_history(self, run_id: str) -> bool:
        with self._lock:
            run = self.get_run(run_id)
            if not run or run.status != BannerRunStatus.COMPLETED:
                return False
            (self.runs_dir / f"{run_id}.json").unlink(missing_ok=True)
            self._delete_reports(run_id)
            self.collect_orphan_assets()
            return True

    def cleanup_old_records(self, now: datetime | None = None, retention_days: int = 30) -> int:
        now = now or datetime.now(timezone.utc)
        removed = 0
        with self._lock:
            for run in self.list_runs():
                timestamp = datetime.fromisoformat(run.approved_at or run.updated_at)
                expired_history = run.status == BannerRunStatus.COMPLETED and now - timestamp > timedelta(days=retention_days)
                expired_session_draft = run.status in {
                    BannerRunStatus.PARTIAL, BannerRunStatus.CANCELLED, BannerRunStatus.FAILED,
                } and now - timestamp > timedelta(days=1)
                if expired_history or expired_session_draft:
                    (self.runs_dir / f"{run.run_id}.json").unlink(missing_ok=True)
                    self._delete_reports(run.run_id)
                    removed += 1
            if removed:
                self.collect_orphan_assets()
        return removed

    def _delete_reports(self, run_id: str) -> None:
        report_dir = self.reports_dir / run_id
        if not report_dir.is_dir():
            return
        for child in report_dir.iterdir():
            if child.is_file():
                child.unlink()
        report_dir.rmdir()

    def collect_orphan_assets(self) -> int:
        referenced = set()
        for run in self.list_runs():
            referenced.update((b.asset.sha256, b.asset.extension) for b in run.banners)
            referenced.update(
                (p.screenshot_asset.sha256, p.screenshot_asset.extension)
                for p in run.brand_progress.values()
                if p.screenshot_asset
            )
        removed = 0
        for path in self.assets_dir.iterdir():
            if not path.is_file() or "." not in path.name:
                continue
            digest, ext = path.name.rsplit(".", 1)
            if (digest, ext) not in referenced:
                path.unlink()
                removed += 1
        return removed

    @staticmethod
    def _atomic_bytes(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _atomic_json(target: Path, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        BannerStorageService._atomic_bytes(target, payload)


banner_storage_service = BannerStorageService()
