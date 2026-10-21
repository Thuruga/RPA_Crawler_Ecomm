"""Testes para o módulo de autenticação limpo do OmniPulse."""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import api.auth as auth_module
from api.auth import (
    register_auth_error_handler,
    verify_api_key,
    verify_connect_or_internal_key,
    verify_ws_api_key,
    verify_ws_connect_or_key,
)


def _create_app():
    app = FastAPI()
    register_auth_error_handler(app)

    @app.get("/protected")
    async def protected_route(identity: dict = Depends(verify_connect_or_internal_key)):
        return identity

    return app


@pytest.fixture(autouse=True)
def configure_keys(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "INTERNAL_API_KEY", "portfolio-key-12345")


def test_dev_bypass_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "AUTH_ENABLED", False)
    app = _create_app()
    client = TestClient(app)
    resp = client.get("/protected")
    assert resp.status_code == 200
    assert resp.json()["auth_type"] == "dev_bypass"


def test_valid_x_api_key_when_auth_enabled(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "AUTH_ENABLED", True)
    app = _create_app()
    client = TestClient(app)
    resp = client.get("/protected", headers={"X-API-Key": "portfolio-key-12345"})
    assert resp.status_code == 200
    assert resp.json()["auth_type"] == "internal_api_key"


def test_valid_bearer_token_when_auth_enabled(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "AUTH_ENABLED", True)
    app = _create_app()
    client = TestClient(app)
    resp = client.get("/protected", headers={"Authorization": "Bearer portfolio-key-12345"})
    assert resp.status_code == 200
    assert resp.json()["auth_type"] == "bearer_token"


def test_missing_credentials_returns_401(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "AUTH_ENABLED", True)
    app = _create_app()
    client = TestClient(app)
    resp = client.get("/protected")
    assert resp.status_code == 401
    assert resp.json()["error"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_websocket_auth(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "AUTH_ENABLED", True)
    assert await verify_ws_connect_or_key(api_key="portfolio-key-12345") is True
    assert await verify_ws_connect_or_key(token="portfolio-key-12345") is True
    assert await verify_ws_connect_or_key(token="wrong-key") is False

    monkeypatch.setattr(auth_module.settings, "AUTH_ENABLED", False)
    assert await verify_ws_connect_or_key(token="any") is True
