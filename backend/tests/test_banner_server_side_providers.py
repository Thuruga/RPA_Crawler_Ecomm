from __future__ import annotations

import json
import hashlib
from types import SimpleNamespace

from core.banner_models import StoredBannerAsset
from services.banner_extraction_service import BannerExtractionService


class _MemoryStorage:
    max_asset_bytes = 30 * 1024 * 1024

    def store_asset(self, data: bytes, content_type: str):
        return StoredBannerAsset(
            sha256=hashlib.sha256(data).hexdigest(),
            extension={"image/jpeg": "jpg", "image/png": "png"}[content_type],
            content_type=content_type,
            byte_count=len(data),
        )


def _brand(key: str):
    return SimpleNamespace(
        brand_key=key,
        brand_name=key.title(),
        domain=f"www.{key}.com",
        banner_start_url=None,
        proxy_url=None,
    )


def test_zara_uses_only_active_universe_and_deduplicates_content(monkeypatch):
    service = BannerExtractionService(storage=_MemoryStorage())
    woman_slide = {
        "sectionName": "Campanha Mulher",
        "xmedias": [{
            "width": 1920,
            "height": 1080,
            "layers": [
                {"type": "vector", "path": "/logo/", "name": "logo", "timestamp": 1},
                {"type": "image", "path": "/campaign/", "name": "woman", "timestamp": 2},
            ],
        }],
    }
    duplicate_slide = {
        "sectionName": "Mesmo arquivo",
        "xmedias": [{
            "width": 1920,
            "height": 1080,
            "layers": [{"type": "image", "path": "/campaign/", "name": "duplicate", "timestamp": 3}],
        }],
    }
    man_slide = {
        "sectionName": "Não deve entrar",
        "xmedias": [{
            "width": 1920,
            "height": 1080,
            "layers": [{"type": "image", "path": "/campaign/", "name": "man", "timestamp": 4}],
        }],
    }
    payload = {"spots": [{"content": {"slides": [
        {"sectionName": "WOMAN", "spot": {"content": {"slides": [woman_slide, duplicate_slide]}}},
        {"sectionName": "MAN", "spot": {"content": {"slides": [man_slide]}}},
    ]}}]}

    def fake_request(session, url, **kwargs):
        del session, kwargs
        if "itxrest" in url:
            return json.dumps(payload).encode(), "application/json", url
        assert "/logo/" not in url
        assert "/man/" not in url
        return b"\xff\xd8\xffsame-campaign-image", "image/jpeg", url

    monkeypatch.setattr(service, "_request_direct", fake_request)
    result = service.extract_brand(None, _brand("zara"))

    assert len(result.banners) == 1
    assert result.banners[0].source_url.endswith("/campaign/woman/woman.jpg?ts=2&w=1280")
    assert result.banners[0].dom_kind == "zara-api"
    assert result.screenshot_asset is None


def test_lacoste_selects_desktop_picture_once_and_detects_video(monkeypatch):
    service = BannerExtractionService(storage=_MemoryStorage())
    html = """
    <main class="js-modular-full-page">
      <a href="/br/oferta"><picture>
        <source media="(min-width: 1920px)" srcset="//imagesa1.lacoste.com/hero-xl.jpg 1x">
        <source media="(min-width: 768px)" srcset="//imagesa1.lacoste.com/hero-md.jpg 1x">
        <img src="//imagesa1.lacoste.com/hero-sm.jpg" alt="Semana do Cliente" width="1920" height="840">
      </picture></a>
      <picture><img src="//imagesa1.lacoste.com/same-content.jpg" alt="Duplicada"></picture>
      <video aria-label="Campanha em vídeo"><source src="//imagesa1.lacoste.com/campaign.mp4"></video>
    </main>
    """.encode()

    def fake_request(session, url, **kwargs):
        del session, kwargs
        if url.endswith("masculino.html"):
            return html, "text/html", url
        return b"\xff\xd8\xffsame-campaign-image", "image/jpeg", url

    monkeypatch.setattr(service, "_request_direct", fake_request)
    result = service.extract_brand(None, _brand("lacoste"))

    assert len(result.banners) == 1
    assert result.banners[0].source_url == "https://imagesa1.lacoste.com/hero-xl.jpg"
    assert result.banners[0].click_url == "https://www.lacoste.com/br/oferta"
    assert result.banners[0].dom_kind == "lacoste-html"
    assert len(result.videos) == 1
    assert result.videos[0].source_url == "https://imagesa1.lacoste.com/campaign.mp4"
