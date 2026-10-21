from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from core.banner_models import BannerRunStatus, BrandBannerStatus
from services.banner_assisted_service import BannerAssistedService
from services.banner_job_service import BannerJobService
from services.banner_storage_service import BannerStorageService


@pytest.fixture(autouse=True)
def _enable_legacy_assisted_path_for_these_compatibility_tests(monkeypatch):
    # Production now extracts Zara and Lacoste automatically on the server.
    # These tests intentionally keep covering the dormant ingestion endpoint.
    monkeypatch.setattr(
        "services.banner_job_service.ASSISTED_ONLY_BRANDS",
        frozenset({"zara", "lacoste"}),
    )


class FakeBrands:
    def list_brands(self, active_only=True):
        del active_only
        return [
            SimpleNamespace(brand_key="zara", brand_name="Zara"),
            SimpleNamespace(brand_key="lacoste", brand_name="Lacoste"),
        ]


def _image_payload(brand_key: str):
    page_url = {
        "zara": "https://www.zara.com/br/pt/",
        "lacoste": "https://www.lacoste.com/br/",
    }[brand_key]
    # The storage layer is content addressed; the MIME normalizer only needs a
    # real format signature for this focused ingestion test.
    png = b"\x89PNG\r\n\x1a\n" + b"assisted-banner"
    return SimpleNamespace(
        page_url=page_url,
        blocked=False,
        capture_error=None,
        media=[SimpleNamespace(
            kind="image",
            source_url="https://static.zara.net/banner.png" if brand_key == "zara" else "https://imagesa1.lacoste.com/banner.png",
            rendered_url=None,
            link_url=None,
            alt_text="Campanha",
            rendered_width=1600,
            rendered_height=700,
            natural_width=1600,
            natural_height=700,
            content_type="image/png",
            data_base64=base64.b64encode(png).decode("ascii"),
        )],
    )


def test_assisted_only_run_waits_then_moves_to_review(tmp_path):
    storage = BannerStorageService(tmp_path / "banners")
    brands = FakeBrands()
    jobs = BannerJobService(
        storage=storage,
        collector=SimpleNamespace(),
        brands=brands,
        browser_session_factory=lambda: (_ for _ in ()).throw(AssertionError("browser must not start")),
    )
    assisted = BannerAssistedService(storage=storage, brands=brands)

    run = jobs.create_job(["zara"])
    assert run.brand_progress["zara"].status == BrandBannerStatus.WAITING_BROWSER
    assert asyncio.run(jobs.run_job(run.run_id)).status == BannerRunStatus.RUNNING

    token, _ = assisted.issue_token(run.run_id, ["zara"])
    assisted.verify_token(token, run.run_id, "zara")
    image_chunk = _image_payload("zara")
    image_chunk.complete = False
    assert assisted.ingest(run.run_id, "zara", image_chunk) == (1, 0)
    assert jobs.finalize_if_ready(run.run_id).status == BannerRunStatus.RUNNING

    final_chunk = SimpleNamespace(
        page_url="https://www.zara.com/br/pt/",
        blocked=False,
        capture_error=None,
        complete=True,
        media=[],
    )
    assert assisted.ingest(run.run_id, "zara", final_chunk) == (0, 0)

    completed = jobs.finalize_if_ready(run.run_id)
    assert completed.status == BannerRunStatus.REVIEW
    assert completed.brand_progress["zara"].status == BrandBannerStatus.COMPLETED
    assert completed.brand_progress["zara"].banner_count == 1
    assert storage.resolve_asset(completed.banners[0].asset).read_bytes().startswith(b"\x89PNG")


def test_token_cannot_authorize_another_brand(tmp_path):
    assisted = BannerAssistedService(
        storage=BannerStorageService(tmp_path / "banners"),
        brands=FakeBrands(),
    )
    token, _ = assisted.issue_token("12345678-run", ["zara"])
    with pytest.raises(ValueError, match="does not authorize"):
        assisted.verify_token(token, "12345678-run", "lacoste")


def test_capture_error_finishes_brand_as_failed(tmp_path):
    storage = BannerStorageService(tmp_path / "banners")
    brands = FakeBrands()
    jobs = BannerJobService(storage=storage, collector=SimpleNamespace(), brands=brands)
    assisted = BannerAssistedService(storage=storage, brands=brands)
    run = jobs.create_job(["lacoste"])

    payload = SimpleNamespace(
        page_url="https://www.lacoste.com/br/",
        capture_error="Falha ao baixar a imagem",
        blocked=False,
        media=[],
    )
    assert assisted.ingest(run.run_id, "lacoste", payload) == (0, 0)
    failed = jobs.finalize_if_ready(run.run_id)
    assert failed.status == BannerRunStatus.FAILED
    assert failed.brand_progress["lacoste"].status == BrandBannerStatus.FAILED
    assert "Falha ao baixar" in (failed.error or "")


def test_assisted_ingest_deduplicates_identical_assets(tmp_path):
    storage = BannerStorageService(tmp_path / "banners")
    brands = FakeBrands()
    jobs = BannerJobService(storage=storage, collector=SimpleNamespace(), brands=brands)
    assisted = BannerAssistedService(storage=storage, brands=brands)
    run = jobs.create_job(["zara"])

    png = b"\x89PNG\r\n\x1a\n" + b"identical-content-bytes"
    encoded = base64.b64encode(png).decode("ascii")

    # Ingest first chunk
    payload1 = SimpleNamespace(
        page_url="https://www.zara.com/br/pt/",
        blocked=False,
        capture_error=None,
        complete=False,
        media=[SimpleNamespace(
            kind="image",
            source_url="https://static.zara.net/campaign-a/banner1.png",
            alt_text="Mulher 1",
            content_type="image/png",
            data_base64=encoded,
        )],
    )
    count1, _ = assisted.ingest(run.run_id, "zara", payload1)
    assert count1 == 1

    # Ingest second chunk with different URL but identical binary content (e.g. from off-screen universe)
    payload2 = SimpleNamespace(
        page_url="https://www.zara.com/br/pt/",
        blocked=False,
        capture_error=None,
        complete=True,
        media=[SimpleNamespace(
            kind="image",
            source_url="https://static.zara.net/campaign-b/banner2.png",
            alt_text="Homem 1",
            content_type="image/png",
            data_base64=encoded,
        )],
    )
    count2, _ = assisted.ingest(run.run_id, "zara", payload2)
    assert count2 == 0  # Deduplicated and skipped!

    final_run = storage.get_run(run.run_id)
    assert len(final_run.banners) == 1
    assert final_run.brand_progress["zara"].banner_count == 1
