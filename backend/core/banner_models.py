"""Typed contracts for desktop banner extraction and review."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BannerRunStatus(str, Enum):
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class BrandBannerStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_BROWSER = "WAITING_BROWSER"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    NO_BANNERS = "NO_BANNERS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StoredBannerAsset(BaseModel):
    sha256: str
    extension: str
    content_type: str
    byte_count: int

    @field_validator("sha256")
    @classmethod
    def valid_digest(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
            raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
        return normalized


class BannerCandidate(BaseModel):
    banner_id: str
    brand_key: str
    brand_name: str
    slide_order: int = Field(ge=1)
    friendly_filename: str
    asset: StoredBannerAsset
    source_url: str
    rendered_url: str
    click_url: Optional[str] = None
    alt_text: Optional[str] = None
    dom_kind: str = "img"
    rendered_width: Optional[int] = None
    rendered_height: Optional[int] = None
    natural_width: Optional[int] = None
    natural_height: Optional[int] = None
    captured_at: str = Field(default_factory=utc_now_iso)
    approved: bool = False


class BannerVideoSlide(BaseModel):
    brand_key: str
    slide_order: int = Field(ge=1)
    source_url: Optional[str] = None
    click_url: Optional[str] = None
    alt_text: Optional[str] = None


class BrandBannerProgress(BaseModel):
    brand_key: str
    brand_name: str
    status: BrandBannerStatus = BrandBannerStatus.PENDING
    banner_count: int = 0
    video_count: int = 0
    error: Optional[str] = None
    screenshot_asset: Optional[StoredBannerAsset] = None


class BannerRun(BaseModel):
    run_id: str
    selected_brands: List[str]
    status: BannerRunStatus = BannerRunStatus.RUNNING
    brand_progress: Dict[str, BrandBannerProgress] = Field(default_factory=dict)
    banners: List[BannerCandidate] = Field(default_factory=list)
    video_slides: List[BannerVideoSlide] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    approved_at: Optional[str] = None
    error: Optional[str] = None


class BannerHistorySummary(BaseModel):
    run_id: str
    created_at: str
    approved_at: str
    banner_count: int
    brand_count: int
    status: BannerRunStatus = BannerRunStatus.COMPLETED

