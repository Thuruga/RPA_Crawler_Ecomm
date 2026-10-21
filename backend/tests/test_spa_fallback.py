from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import install_frontend_routes


FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _build_app():
    assert (FRONTEND_DIST / "index.html").is_file()
    target = FastAPI()

    @target.get("/monitor/jobs")
    def monitor_api():
        return {"native": True}

    @target.get("/search")
    def search_api():
        return {"native": True}

    install_frontend_routes(target, FRONTEND_DIST)
    return target


def test_allowlisted_documents_serve_spa():
    client = TestClient(_build_app())
    for path in ["/", "/monitor", "/comparativa", "/sku", "/categorias", "/banners", "/marcas", "/comparativa/job%2Fid%201", "/sku/job-1"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html"), path


def test_api_and_asset_misses_are_never_rewritten_to_html():
    client = TestClient(_build_app())
    assert client.get("/monitor/jobs").json() == {"native": True}
    assert client.get("/search").json() == {"native": True}

    for path in ["/monitor/missing", "/history/missing", "/ws/missing", "/banners/jobs/missing", "/assets/missing.js", "/unknown"]:
        response = client.get(path)
        assert response.status_code == 404, path
        assert not response.headers.get("content-type", "").startswith("text/html"), path