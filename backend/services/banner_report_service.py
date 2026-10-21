"""Generate auditable JSON, CSV and HTML artifacts for a banner run."""

from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path

from core.banner_models import BannerRun
from services.banner_storage_service import BannerStorageService


class BannerReportService:
    def __init__(self, storage: BannerStorageService):
        self.storage = storage

    def generate(self, run: BannerRun) -> dict[str, Path]:
        self.storage._validate_run_id(run.run_id)
        target = self.storage.reports_dir / run.run_id
        target.mkdir(parents=True, exist_ok=True)
        outputs = {
            "json": target / "banners.json",
            "csv": target / "banners.csv",
            "html": target / "galeria.html",
        }
        self.storage._atomic_bytes(
            outputs["json"],
            json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        csv_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            csv_buffer,
            fieldnames=[
                "banner_id", "brand_key", "brand_name", "slide_order", "friendly_filename",
                "sha256", "content_type", "byte_count", "source_url", "rendered_url",
                "click_url", "alt_text", "natural_width", "natural_height", "captured_at",
            ],
        )
        writer.writeheader()
        for banner in run.banners:
            row = banner.model_dump(mode="json")
            asset = row.pop("asset")
            writer.writerow({key: asset.get(key, row.get(key)) for key in writer.fieldnames})
        self.storage._atomic_bytes(outputs["csv"], csv_buffer.getvalue().encode("utf-8-sig"))

        cards = "".join(
            f'<article><img src="../../assets/{b.asset.sha256}.{b.asset.extension}" '
            f'alt="{html.escape(b.alt_text or b.friendly_filename)}"><h2>{html.escape(b.brand_name)}</h2>'
            f'<p>{html.escape(b.friendly_filename)}</p></article>'
            for b in run.banners
        ) + "".join(
            f'<article><video controls muted playsinline src="{html.escape(v.source_url or "")}"></video>'
            f'<h2>{html.escape(v.brand_key)}</h2><p>{html.escape(v.alt_text or f"Vídeo slide {v.slide_order}")}</p></article>'
            for v in run.video_slides if v.source_url
        )
        errors = "".join(
            f"<li>{html.escape(p.brand_name)}: {html.escape(p.error or '')}</li>"
            for p in run.brand_progress.values() if p.error
        )
        document = (
            "<!doctype html><html lang='pt-BR'><meta charset='utf-8'>"
            "<title>Galeria de banners</title><style>body{font-family:sans-serif;background:#111;color:#eee}"
            "main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}"
            "article{border:1px solid #444;padding:12px}img,video{width:100%;aspect-ratio:16/7;object-fit:contain}</style>"
            f"<h1>Execução {html.escape(run.run_id)}</h1><ul>{errors}</ul><main>{cards}</main></html>"
        )
        self.storage._atomic_bytes(outputs["html"], document.encode("utf-8"))
        return outputs

    def resolve(self, run_id: str, report_format: str) -> Path:
        self.storage._validate_run_id(run_id)
        names = {"json": "banners.json", "csv": "banners.csv", "html": "galeria.html"}
        if report_format not in names:
            raise ValueError("unsupported report format")
        path = self.storage.reports_dir / run_id / names[report_format]
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

