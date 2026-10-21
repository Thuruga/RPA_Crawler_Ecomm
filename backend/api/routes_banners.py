"""Authenticated API for desktop banner extraction, review and history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.banner_models import BannerRun, BannerRunStatus
from services.banner_blob_mirror_service import banner_blob_mirror_service
from services.banner_job_service import ASSISTED_ONLY_BRANDS, banner_job_service
from services.banner_assisted_service import TARGET_URLS, banner_assisted_service
from services.banner_report_service import BannerReportService
from services.banner_storage_service import banner_storage_service


router = APIRouter(prefix="/banners", tags=["Banners"])
assisted_router = APIRouter(prefix="/banners/assisted", tags=["Banners assisted capture"])
banner_report_service = BannerReportService(banner_storage_service)


class StartBannerJobRequest(BaseModel):
    brands: list[str] = Field(min_length=1)


class ApproveBannerRunRequest(BaseModel):
    banner_ids: list[str] = Field(default_factory=list)


class AssistedMediaItem(BaseModel):
    kind: str = "image"
    source_url: str
    rendered_url: str | None = None
    link_url: str | None = None
    alt_text: str | None = None
    rendered_width: int | None = None
    rendered_height: int | None = None
    natural_width: int | None = None
    natural_height: int | None = None
    content_type: str | None = None
    data_base64: str | None = None


class AssistedCaptureRequest(BaseModel):
    brand_key: str
    page_url: str
    source_host: str | None = None
    blocked: bool = False
    capture_error: str | None = None
    complete: bool = True
    media: list[AssistedMediaItem] = Field(default_factory=list, max_length=100)


def _run_or_404(run_id: str) -> BannerRun:
    try:
        run = banner_storage_service.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not run:
        raise HTTPException(status_code=404, detail="Extração não encontrada")
    return run


@router.post("/jobs", status_code=202)
async def start_banner_job(request: StartBannerJobRequest, background_tasks: BackgroundTasks):
    try:
        run = banner_job_service.create_job(request.brands)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(banner_job_service.run_job, run.run_id)
    assisted_brands = [key for key in run.selected_brands if key in ASSISTED_ONLY_BRANDS]
    assisted_capture = None
    if assisted_brands:
        token, expires_at = banner_assisted_service.issue_token(run.run_id, assisted_brands)
        assisted_capture = {
            "targets": [
                {"brand_key": key, "page_url": TARGET_URLS[key]}
                for key in assisted_brands
            ],
            "receiver_path": f"/banners/assisted/jobs/{run.run_id}/captures",
            "token": token,
            "expires_at": expires_at,
        }
    return {
        "job_id": run.run_id,
        "status": run.status,
        "run": run,
        "assisted_capture": assisted_capture,
    }


@assisted_router.post("/jobs/{run_id}/captures", status_code=201)
async def receive_assisted_capture(
    run_id: str,
    request: AssistedCaptureRequest,
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de captura ausente")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        banner_assisted_service.verify_token(token, run_id, request.brand_key)
        image_count, video_count = banner_assisted_service.ingest(run_id, request.brand_key, request)
        run = banner_job_service.finalize_if_ready(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Extração não encontrada") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "job_id": run_id,
        "brand_key": request.brand_key,
        "image_count": image_count,
        "video_count": video_count,
        "status": run.status,
    }


@router.get("/jobs/{run_id}", response_model=BannerRun)
async def get_banner_job(run_id: str):
    return _run_or_404(run_id)


@router.post("/jobs/{run_id}/stop", status_code=202)
async def stop_banner_job(run_id: str):
    run = _run_or_404(run_id)
    if run.status != BannerRunStatus.RUNNING or not banner_job_service.stop_job(run_id):
        raise HTTPException(status_code=409, detail="A extração não está em andamento")
    return {"job_id": run_id, "message": "Parada solicitada"}


@router.post("/jobs/{run_id}/approve", response_model=BannerRun)
async def approve_banner_job(run_id: str, request: ApproveBannerRunRequest):
    _run_or_404(run_id)
    try:
        run = banner_storage_service.approve_run(run_id, request.banner_ids)
        banner_report_service.generate(run)
        banner_blob_mirror_service.schedule(run.run_id)
        return run
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/mirror/retry")
async def retry_banner_blob_mirror():
    if not banner_blob_mirror_service.enabled:
        raise HTTPException(
            status_code=409,
            detail="Espelho de banners no Azure Blob desativado: defina BANNER_BLOB_SAS_URL no backend/.env.",
        )
    return await asyncio.wrap_future(banner_blob_mirror_service.submit_retry())


@router.get("/history")
async def list_banner_history():
    return banner_storage_service.list_history()


@router.get("/history/{run_id}", response_model=BannerRun)
async def get_banner_history(run_id: str):
    run = _run_or_404(run_id)
    if run.status != BannerRunStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Histórico não encontrado")
    return run


@router.delete("/history/{run_id}", status_code=204)
async def delete_banner_history(run_id: str):
    try:
        deleted = banner_storage_service.delete_history(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Histórico não encontrado")


@router.get("/assets/{run_id}/{banner_id}")
async def get_banner_asset(run_id: str, banner_id: str):
    run = _run_or_404(run_id)
    banner = next((item for item in run.banners if item.banner_id == banner_id), None)
    if not banner:
        raise HTTPException(status_code=404, detail="Banner não encontrado")
    try:
        path = banner_storage_service.resolve_asset(banner.asset)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado") from exc
    return FileResponse(
        path, media_type=banner.asset.content_type,
        headers={"Content-Disposition": f'inline; filename="{banner.friendly_filename}"'},
    )


@router.get("/screenshots/{run_id}/{brand_key}")
async def get_banner_screenshot(run_id: str, brand_key: str):
    run = _run_or_404(run_id)
    progress = run.brand_progress.get(brand_key)
    if not progress or not progress.screenshot_asset:
        raise HTTPException(status_code=404, detail="Captura não encontrada")
    try:
        path = banner_storage_service.resolve_asset(progress.screenshot_asset)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Captura não encontrada") from exc
    return FileResponse(path, media_type=progress.screenshot_asset.content_type)


@router.get("/runs/{run_id}/reports/{report_format}")
async def get_banner_report(run_id: str, report_format: str):
    _run_or_404(run_id)
    try:
        path = banner_report_service.resolve(run_id, report_format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Relatório não encontrado") from exc
    media = {"json": "application/json", "csv": "text/csv", "html": "text/html"}[report_format]
    return FileResponse(path, media_type=media, filename=path.name)
