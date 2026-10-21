from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from core.banner_models import BrandBannerStatus
from services.banner_extraction_service import (
    BannerExtractionIssue,
    BannerExtractionService,
    normalize_image_content_type,
)
from core.banner_models import StoredBannerAsset


class _MemoryStorage:
    max_asset_bytes = 30 * 1024 * 1024

    def store_asset(self, data: bytes, content_type: str):
        mime = content_type.split(";", 1)[0]
        extension = {"image/png": "png", "image/webp": "webp"}[mime]
        return StoredBannerAsset(
            sha256=hashlib.sha256(data).hexdigest(),
            extension=extension,
            content_type=mime,
            byte_count=len(data),
        )


class _FakeBody:
    def __init__(self, text: str):
        self._text = text

    def inner_text(self, timeout: int):
        return self._text


class _FakePage:
    url = "https://example.test/"
    context = SimpleNamespace()

    def screenshot(self, full_page=False):
        # A minimal valid PNG header is enough because the fake storage only
        # needs to receive bytes for this contract test.
        return b"\x89PNG\r\n\x1a\n"

    def title(self):
        return "Example"

    def locator(self, selector):
        return _FakeBody("")

    def evaluate(self, script):
        if "window.scrollTo" in script:
            return True
        if "declared_slides" in script:
            return {"declared_slides": 1, "video_slides": []}
        if "const pattern" in script:
            return None
        return [{
            "kind": "img",
            "source_url": "https://example.com/banner.webp",
            "rendered_url": "https://example.com/banner.webp",
            "alt": "Banner principal",
            "link_url": "https://example.test/oferta",
            "natural_width": 1920,
            "natural_height": 700,
            "width": 1366,
            "height": 498,
        }]


def test_html_error_is_never_accepted_as_an_image():
    with pytest.raises(BannerExtractionIssue, match="HTML") as error:
        normalize_image_content_type(
            "image/jpeg",
            "https://cdn.example.test/banner.jpg",
            b"<!doctype html><title>Access Denied</title>",
        )

    assert error.value.kind == "BLOCKED"


def test_collect_page_preserves_screenshot_when_no_banner_is_found():
    storage = _MemoryStorage()
    service = BannerExtractionService(
        storage=storage,
        max_slides=0,
        asset_fetcher=lambda *_args: (b"RIFFxxxxWEBP", "image/webp"),
    )
    page = _FakePage()
    page.evaluate = lambda script: (
        {"declared_slides": None, "video_slides": []}
        if "declared_slides" in script
        else []
    )

    with pytest.raises(BannerExtractionIssue) as error:
        service.collect_page(page, "example", "Example")

    assert error.value.kind == "NO_BANNERS"
    assert error.value.screenshot_asset is not None


def test_collect_page_extracts_banner_and_keeps_source_provenance():
    storage = _MemoryStorage()
    service = BannerExtractionService(
        storage=storage,
        max_slides=0,
        asset_fetcher=lambda *_args: (b"RIFFxxxxWEBP", "image/webp"),
    )
    result = service.collect_page(_FakePage(), "example", "Example")

    assert len(result.banners) == 1
    assert result.banners[0].source_url == "https://example.com/banner.webp"
    assert result.banners[0].asset.content_type == "image/webp"


def test_collect_page_classifies_mp4_candidate_as_video_before_downloading():
    service = BannerExtractionService(
        storage=_MemoryStorage(),
        max_slides=0,
        asset_fetcher=lambda *_args: pytest.fail("MP4 must not reach the image downloader"),
    )
    page = _FakePage()
    page.evaluate = lambda script: (
        {"declared_slides": 1, "video_slides": []}
        if "declared_slides" in script
        else None
        if "const pattern" in script
        else True
        if "window.scrollTo" in script
        else [{
            "kind": "img",
            "source_url": "https://oriba.com.br/banner.mp4?w=1280",
            "rendered_url": "https://oriba.com.br/banner.mp4?w=1280",
            "alt": "Banner em vídeo",
            "link_url": "",
            "natural_width": None,
            "natural_height": None,
            "width": 1366,
            "height": 768,
        }]
    )

    result = service.collect_page(page, "oriba", "Oriba")

    assert result.banners == []
    assert [video.source_url for video in result.videos] == [
        "https://oriba.com.br/banner.mp4?w=1280"
    ]


def test_blocked_and_empty_states_are_distinct_from_success():
    assert BrandBannerStatus.BLOCKED.value == "BLOCKED"
    assert BrandBannerStatus.NO_BANNERS.value == "NO_BANNERS"
    assert BrandBannerStatus.COMPLETED.value == "COMPLETED"


def test_collect_page_extracts_video_slide_and_preserves_metadata():
    service = BannerExtractionService(
        storage=_MemoryStorage(),
        max_slides=0,
        asset_fetcher=lambda *_args: pytest.fail("Videos must not reach image downloader"),
    )
    page = _FakePage()
    page.evaluate = lambda script: (
        {"declared_slides": 1, "video_slides": []}
        if "declared_slides" in script
        else None
        if "const pattern" in script
        else True
        if "window.scrollTo" in script
        else [{
            "kind": "video",
            "source_url": "https://tfmedia.tf.com.br/hero.mp4",
            "rendered_url": "https://tfmedia.tf.com.br/hero.mp4",
            "alt": "Banner Home Principal - Lancamento Verao 2027",
            "link_url": "https://www.tf.com.br/verao",
            "natural_width": 1920,
            "natural_height": 1080,
            "width": 1366,
            "height": 503,
        }]
    )

    result = service.collect_page(page, "trackfield", "Track & Field")

    assert result.banners == []
    assert len(result.videos) == 1
    assert result.videos[0].brand_key == "trackfield"
    assert result.videos[0].source_url == "https://tfmedia.tf.com.br/hero.mp4"
    assert result.videos[0].alt_text == "Banner Home Principal - Lancamento Verao 2027"
    assert result.videos[0].click_url == "https://www.tf.com.br/verao"
    assert result.videos[0].slide_order == 1

