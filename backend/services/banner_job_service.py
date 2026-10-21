"""Sequential multi-brand job orchestration for desktop banner extraction."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager, nullcontext
from typing import Any, Iterable

from core.banner_models import (
    BannerCandidate,
    BannerRun,
    BannerRunStatus,
    BrandBannerProgress,
    BrandBannerStatus,
)
from core.browser_manager import BrowserManager
from core.job_manager import JOB_CANCEL_FLAGS
from core.websocket import manager
from services.banner_blob_mirror_service import BannerBlobMirrorService, banner_blob_mirror_service
from services.banner_extraction_service import (
    BannerExtractionIssue,
    BannerExtractionService,
    SERVER_SIDE_BANNER_BRANDS,
    banner_extraction_service,
)
from services.banner_report_service import BannerReportService
from services.banner_storage_service import BannerStorageService, banner_storage_service
from services.brand_service import BrandManagerService, brand_service
from services.banner_assisted_service import ASSISTED_BRANDS


ASSISTED_ONLY_BRANDS = ASSISTED_BRANDS - SERVER_SIDE_BANNER_BRANDS


@contextmanager
def chromium_session():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=BrowserManager.CHROMIUM_ARGS)
        try:
            yield browser
        finally:
            browser.close()


class BannerJobService:
    def __init__(
        self,
        storage: BannerStorageService = banner_storage_service,
        collector: BannerExtractionService = banner_extraction_service,
        brands: BrandManagerService = brand_service,
        browser_session_factory=chromium_session,
        mirror: BannerBlobMirrorService = banner_blob_mirror_service,
    ):
        self.storage = storage
        self.collector = collector
        self.brands = brands
        self.browser_session_factory = browser_session_factory
        self.reports = BannerReportService(storage)
        self.mirror = mirror

    def create_job(self, requested_brands: Iterable[str]) -> BannerRun:
        requested = list(dict.fromkeys(key.lower().strip() for key in requested_brands if key.strip()))
        if not requested:
            raise ValueError("select at least one active brand")
        active = {brand.brand_key: brand for brand in self.brands.list_brands(active_only=True)}
        invalid = [key for key in requested if key not in active]
        if invalid:
            raise ValueError(f"inactive or unknown brands: {', '.join(invalid)}")
        run_id = str(uuid.uuid4())
        run = BannerRun(
            run_id=run_id,
            selected_brands=requested,
            brand_progress={
                key: BrandBannerProgress(
                    brand_key=key,
                    brand_name=active[key].brand_name,
                    status=(
                        BrandBannerStatus.WAITING_BROWSER
                        if key in ASSISTED_ONLY_BRANDS
                        else BrandBannerStatus.PENDING
                    ),
                )
                for key in requested
            },
        )
        self.storage.save_run(run)
        JOB_CANCEL_FLAGS[run_id] = asyncio.Event()
        return run

    async def run_job(self, run_id: str) -> BannerRun:
        loop = asyncio.get_running_loop()
        cancel_event = JOB_CANCEL_FLAGS.get(run_id)
        if cancel_event is None:
            raise KeyError(run_id)
        try:
            return await asyncio.to_thread(self._run_sync, run_id, cancel_event, loop)
        except Exception as exc:
            run = self.storage.get_run(run_id)
            if not run:
                raise
            error = f"{type(exc).__name__}: {exc}"
            for progress in run.brand_progress.values():
                if progress.status in {BrandBannerStatus.PENDING, BrandBannerStatus.RUNNING}:
                    progress.status = BrandBannerStatus.FAILED
                    progress.error = error
            self._finalize(run)
            await manager.send_message({
                "type": "banner_progress", "job_id": run_id,
                "event": {
                    "kind": "terminal" if run.status != BannerRunStatus.RUNNING else "progress",
                    "status": run.status.value,
                },
                "run": run.model_dump(mode="json"),
            }, run_id)
            return run
        finally:
            JOB_CANCEL_FLAGS.pop(run_id, None)

    def _run_sync(self, run_id: str, cancel_event: asyncio.Event, loop: asyncio.AbstractEventLoop) -> BannerRun:
        run = self.storage.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        active = {brand.brand_key: brand for brand in self.brands.list_brands(active_only=True)}

        browser_brands = [
            key for key in run.selected_brands
            if key not in ASSISTED_ONLY_BRANDS and key not in SERVER_SIDE_BANNER_BRANDS
        ]
        browser_context = self.browser_session_factory() if browser_brands else nullcontext(None)
        with browser_context as browser:
            for key in run.selected_brands:
                if key in ASSISTED_ONLY_BRANDS:
                    continue
                progress = run.brand_progress[key]
                if cancel_event.is_set():
                    progress.status = BrandBannerStatus.CANCELLED
                    continue
                progress.status = BrandBannerStatus.RUNNING
                self._publish(loop, run, {"kind": "brand", "brand_key": key, "status": progress.status})

                def collector_progress(event: dict) -> None:
                    if event.get("kind") == "banner":
                        candidate = BannerCandidate.model_validate(event["banner"])
                        if all(existing.banner_id != candidate.banner_id for existing in run.banners):
                            run.banners.append(candidate)
                            progress.banner_count = len([b for b in run.banners if b.brand_key == key])
                    elif event.get("kind") == "discovery":
                        progress.video_count = event.get("video_count", progress.video_count)
                    self._publish(loop, run, {**event, "brand_key": key})

                try:
                    result = self.collector.extract_brand(
                        browser, active[key], cancel_event=cancel_event,
                        progress_callback=collector_progress,
                    )
                    for banner in result.banners:
                        if all(existing.banner_id != banner.banner_id for existing in run.banners):
                            run.banners.append(banner)
                    run.video_slides.extend(result.videos)
                    progress.banner_count = len([b for b in run.banners if b.brand_key == key])
                    progress.video_count = len(result.videos)
                    progress.screenshot_asset = result.screenshot_asset
                    progress.status = BrandBannerStatus.COMPLETED
                except InterruptedError:
                    progress.status = BrandBannerStatus.CANCELLED
                    cancel_event.set()
                except BannerExtractionIssue as exc:  # preserve actionable extraction state
                    progress.status = (
                        BrandBannerStatus.BLOCKED
                        if exc.kind == "BLOCKED"
                        else BrandBannerStatus.NO_BANNERS
                    )
                    progress.error = f"{type(exc).__name__}: {exc}"
                    progress.screenshot_asset = exc.screenshot_asset
                except Exception as exc:  # isolate each retailer failure
                    progress.status = BrandBannerStatus.FAILED
                    progress.error = f"{type(exc).__name__}: {exc}"
                self._publish(loop, run, {
                    "kind": "brand", "brand_key": key, "status": progress.status,
                    "banner_count": progress.banner_count, "video_count": progress.video_count, "error": progress.error,
                })

        self._merge_assisted_results(run)
        if cancel_event.is_set():
            for progress in run.brand_progress.values():
                if progress.status in {
                    BrandBannerStatus.PENDING,
                    BrandBannerStatus.RUNNING,
                    BrandBannerStatus.WAITING_BROWSER,
                }:
                    progress.status = BrandBannerStatus.CANCELLED
            run.status = BannerRunStatus.CANCELLED
            self.storage.save_run(run)
            self.reports.generate(run)
            self.mirror.schedule(run.run_id)
        else:
            self._finalize(run)

        self._publish(loop, run, {
            "kind": "terminal" if run.status != BannerRunStatus.RUNNING else "progress",
            "status": run.status,
        })
        return run

    def stop_job(self, run_id: str) -> bool:
        event = JOB_CANCEL_FLAGS.get(run_id)
        run = self.storage.get_run(run_id)
        if not run or run.status != BannerRunStatus.RUNNING:
            return False
        if event:
            event.set()
        for progress in run.brand_progress.values():
            if progress.status in {
                BrandBannerStatus.PENDING,
                BrandBannerStatus.RUNNING,
                BrandBannerStatus.WAITING_BROWSER,
            }:
                progress.status = BrandBannerStatus.CANCELLED
        run.status = BannerRunStatus.CANCELLED
        self.storage.save_run(run)
        return True

    def finalize_if_ready(self, run_id: str) -> BannerRun:
        run = self.storage.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        self._finalize(run)
        return run

    def _finalize(self, run: BannerRun) -> None:
        with self.storage.locked():
            self._merge_assisted_results(run)
            active = {
                BrandBannerStatus.PENDING,
                BrandBannerStatus.RUNNING,
                BrandBannerStatus.WAITING_BROWSER,
            }
            if any(progress.status in active for progress in run.brand_progress.values()):
                run.status = BannerRunStatus.RUNNING
                self.storage.save_run(run)
                return
            failed = [
                progress for progress in run.brand_progress.values()
                if progress.status in {
                    BrandBannerStatus.FAILED,
                    BrandBannerStatus.BLOCKED,
                    BrandBannerStatus.NO_BANNERS,
                }
            ]
            if failed:
                run.status = BannerRunStatus.PARTIAL if run.banners or run.video_slides else BannerRunStatus.FAILED
                run.error = "; ".join(
                    f"{item.brand_name}: {item.error or item.status.value}"
                    for item in failed
                )
            elif not run.banners and not run.video_slides:
                run.status = BannerRunStatus.FAILED
                run.error = "Nenhum banner foi encontrado nas marcas selecionadas."
            elif any(progress.status == BrandBannerStatus.CANCELLED for progress in run.brand_progress.values()):
                run.status = BannerRunStatus.CANCELLED
            else:
                run.status = BannerRunStatus.REVIEW
                run.error = None
            self.storage.save_run(run)
        self.reports.generate(run)
        self.mirror.schedule(run.run_id)

    def _merge_assisted_results(self, run: BannerRun) -> None:
        current = self.storage.get_run(run.run_id)
        if not current:
            return
        for key in ASSISTED_ONLY_BRANDS & set(run.brand_progress):
            external = current.brand_progress[key]
            if external.status != BrandBannerStatus.WAITING_BROWSER:
                run.brand_progress[key] = external
        known_banner_ids = {banner.banner_id for banner in run.banners}
        run.banners.extend(
            banner for banner in current.banners
            if banner.brand_key in ASSISTED_ONLY_BRANDS and banner.banner_id not in known_banner_ids
        )
        known_videos = {(video.brand_key, video.slide_order, video.source_url) for video in run.video_slides}
        run.video_slides.extend(
            video for video in current.video_slides
            if video.brand_key in ASSISTED_ONLY_BRANDS
            and (video.brand_key, video.slide_order, video.source_url) not in known_videos
        )

    def _publish(self, loop: asyncio.AbstractEventLoop, run: BannerRun, event: dict) -> None:
        with self.storage.locked():
            self._merge_assisted_results(run)
            self.storage.save_run(run)
        payload = {
            "type": "banner_progress",
            "job_id": run.run_id,
            "event": {
                **event,
                "status": str(event["status"].value if hasattr(event.get("status"), "value") else event.get("status")),
            },
            "run": run.model_dump(mode="json"),
        }
        asyncio.run_coroutine_threadsafe(manager.send_message(payload, run.run_id), loop)


banner_job_service = BannerJobService()
