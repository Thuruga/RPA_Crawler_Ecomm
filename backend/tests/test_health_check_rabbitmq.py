import pytest
from fastapi.testclient import TestClient

from app import app

client = TestClient(app)

def test_health_check_includes_rabbitmq_status(monkeypatch):
    from services.rabbitmq_service import rabbitmq_service
    
    import asyncio
    async def mock_wait_for(*args, **kwargs):
        pass
    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)
    
    monkeypatch.setattr(rabbitmq_service, "get_status", lambda: "connected")
    
    response = client.get("/health-check")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["rabbitmq"] == "connected"
