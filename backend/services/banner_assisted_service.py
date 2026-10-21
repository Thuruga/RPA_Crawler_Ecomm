"""Signed browser-assisted capture ingestion for anti-bot protected retailers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from config import settings
from core.banner_models import BannerCandidate, BannerVideoSlide, BrandBannerStatus
from services.banner_extraction_service import normalize_image_content_type
from services.banner_storage_service import BannerStorageService, banner_storage_service, friendly_banner_filename
from services.brand_service import BrandManagerService, brand_service


ASSISTED_BRANDS = frozenset({"zara", "lacoste"})
TARGET_URLS = {
    "zara": "https://www.zara.com/br/pt/",
    "lacoste": "https://www.lacoste.com/br/",
}
SOURCE_HOST_SUFFIXES = {
    "zara": ("zara.com",),
    "lacoste": ("lacoste.com",),
}
MAX_CAPTURE_ASSETS = 100
MAX_CAPTURE_BYTES = 50 * 1024 * 1024
TOKEN_TTL_SECONDS = 15 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _valid_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


@dataclass(frozen=True)
class AssistedCaptureGrant:
    run_id: str
    brands: tuple[str, ...]
    expires_at: int


class BannerAssistedService:
    def __init__(
        self,
        storage: BannerStorageService = banner_storage_service,
        brands: BrandManagerService = brand_service,
    ):
        self.storage = storage
        self.brands = brands
        self._secret = hashlib.sha256(
            f"banner-assisted:{settings.INTERNAL_API_KEY}".encode("utf-8")
        ).digest()

    def issue_token(self, run_id: str, brands: Iterable[str]) -> tuple[str, int]:
        allowed = tuple(sorted(set(brands) & ASSISTED_BRANDS))
        if not allowed:
            raise ValueError("no browser-assisted brands requested")
        expires_at = int(time.time()) + TOKEN_TTL_SECONDS
        payload = json.dumps(
            {"run_id": run_id, "brands": allowed, "exp": expires_at},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64url(payload)}.{_b64url(signature)}", expires_at

    def verify_token(self, token: str, run_id: str, brand_key: str) -> AssistedCaptureGrant:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload = _unb64url(encoded_payload)
            signature = _unb64url(encoded_signature)
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("invalid assisted capture token")
            decoded = json.loads(payload)
            grant = AssistedCaptureGrant(
                run_id=str(decoded["run_id"]),
                brands=tuple(decoded["brands"]),
                expires_at=int(decoded["exp"]),
            )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("invalid assisted capture token") from exc
        if grant.expires_at < int(time.time()):
            raise ValueError("assisted capture token expired")
        if grant.run_id != run_id or brand_key not in grant.brands:
            raise ValueError("assisted capture token does not authorize this capture")
        return grant

    def ingest(self, run_id: str, brand_key: str, payload: object) -> tuple[int, int]:
        if brand_key not in ASSISTED_BRANDS:
            raise ValueError("brand does not support browser-assisted capture")
        run = self.storage.get_run(run_id)
        if not run or brand_key not in run.brand_progress:
            raise KeyError(run_id)
        progress = run.brand_progress[brand_key]
        if progress.status != BrandBannerStatus.WAITING_BROWSER:
            raise ValueError("brand is not waiting for browser capture")

        capture_error = str(getattr(payload, "capture_error", "") or "").strip()
        if capture_error:
            return self.fail(run_id, brand_key, capture_error)

        page_url = str(getattr(payload, "page_url", ""))
        source_host = (urlparse(page_url).hostname or "").lower()
        if not any(source_host == suffix or source_host.endswith(f".{suffix}") for suffix in SOURCE_HOST_SUFFIXES[brand_key]):
            raise ValueError("capture page does not match the requested brand")
        if bool(getattr(payload, "blocked", False)):
            return self.fail(run_id, brand_key, "A página exibiu um bloqueio no navegador")

        media = list(getattr(payload, "media", []))
        complete = bool(getattr(payload, "complete", True))
        if len(media) > MAX_CAPTURE_ASSETS:
            raise ValueError("capture contains too many assets")

        active = {brand.brand_key: brand for brand in self.brands.list_brands(active_only=True)}
        brand_name = active[brand_key].brand_name if brand_key in active else brand_key.title()
        banners: list[BannerCandidate] = []
        videos: list[BannerVideoSlide] = []
        existing_banners = [banner for banner in run.banners if banner.brand_key == brand_key]
        existing_videos = [video for video in run.video_slides if video.brand_key == brand_key]
        seen_hashes = {banner.asset.sha256 for banner in existing_banners}
        seen_sources = {banner.source_url for banner in existing_banners}
        seen_video_sources = {video.source_url for video in existing_videos if video.source_url}
        total_bytes = sum(banner.asset.byte_count for banner in existing_banners)

        for item in media:
            source_url = str(getattr(item, "source_url", ""))
            if not _valid_http_url(source_url) or source_url in seen_sources:
                continue
            kind = str(getattr(item, "kind", "image"))
            order = len(existing_banners) + len(existing_videos) + len(banners) + len(videos) + 1
            if kind == "video":
                if source_url not in seen_video_sources:
                    seen_video_sources.add(source_url)
                    videos.append(BannerVideoSlide(
                        brand_key=brand_key,
                        slide_order=order,
                        source_url=source_url,
                        click_url=getattr(item, "link_url", None),
                        alt_text=getattr(item, "alt_text", None),
                    ))
                continue
            encoded = getattr(item, "data_base64", None)
            if not encoded:
                continue
            try:
                body = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("capture contains invalid base64 asset data") from exc

            body_sha256 = hashlib.sha256(body).hexdigest()
            if body_sha256 in seen_hashes:
                continue

            total_bytes += len(body)
            if total_bytes > MAX_CAPTURE_BYTES:
                raise ValueError("capture exceeds the total byte limit")
            content_type = normalize_image_content_type(
                str(getattr(item, "content_type", "")), source_url, body
            )
            asset = self.storage.store_asset(body, content_type)
            seen_hashes.add(asset.sha256)
            seen_sources.add(source_url)
            banner_order = len(existing_banners) + len(banners) + 1
            banners.append(BannerCandidate(
                banner_id=str(uuid.uuid4()),
                brand_key=brand_key,
                brand_name=brand_name,
                slide_order=banner_order,
                friendly_filename=friendly_banner_filename(
                    banner_order, str(getattr(item, "alt_text", "") or ""), brand_name, asset.extension
                ),
                asset=asset,
                source_url=source_url,
                rendered_url=str(getattr(item, "rendered_url", "") or source_url),
                click_url=getattr(item, "link_url", None),
                alt_text=getattr(item, "alt_text", None),
                dom_kind=kind,
                rendered_width=getattr(item, "rendered_width", None),
                rendered_height=getattr(item, "rendered_height", None),
                natural_width=getattr(item, "natural_width", None),
                natural_height=getattr(item, "natural_height", None),
            ))

        # Reload immediately before the write so we preserve progress produced by
        # the normal extractor while the extension was downloading image bytes.
        with self.storage.locked():
            run = self.storage.get_run(run_id)
            if not run:
                raise KeyError(run_id)
            progress = run.brand_progress[brand_key]
            if progress.status != BrandBannerStatus.WAITING_BROWSER:
                raise ValueError("brand is not waiting for browser capture")
            
            # Avoid duplicate inserts if multiple chunks reported the same asset
            existing_locked_hashes = {b.asset.sha256 for b in run.banners if b.brand_key == brand_key}
            new_banners = [b for b in banners if b.asset.sha256 not in existing_locked_hashes]
            existing_locked_vids = {v.source_url for v in run.video_slides if v.brand_key == brand_key and v.source_url}
            new_videos = [v for v in videos if v.source_url not in existing_locked_vids]

            run.banners.extend(new_banners)
            run.video_slides.extend(new_videos)
            progress.banner_count = len([banner for banner in run.banners if banner.brand_key == brand_key])
            progress.video_count = len([video for video in run.video_slides if video.brand_key == brand_key])
            if complete:
                if progress.banner_count or progress.video_count:
                    progress.status = BrandBannerStatus.COMPLETED
                    progress.error = None
                else:
                    progress.status = BrandBannerStatus.NO_BANNERS
                    progress.error = "A extensão não encontrou banners na página inicial."
            self.storage.save_run(run)
        return len(banners), len(videos)

    def fail(self, run_id: str, brand_key: str, message: str) -> tuple[int, int]:
        with self.storage.locked():
            run = self.storage.get_run(run_id)
            if not run or brand_key not in run.brand_progress:
                raise KeyError(run_id)
            progress = run.brand_progress[brand_key]
            if progress.status == BrandBannerStatus.WAITING_BROWSER:
                progress.status = BrandBannerStatus.FAILED
                progress.error = message[:500]
                self.storage.save_run(run)
        return 0, 0


banner_assisted_service = BannerAssistedService()
