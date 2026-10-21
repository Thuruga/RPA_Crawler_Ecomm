import pytest
from fastapi.testclient import TestClient
from app import app
from config import settings

client = TestClient(app)

def test_trigger_category_scan_enqueues_messages_for_brand(monkeypatch):
    # `load_monitored_categories` reads monitors from Postgres now, so the
    # double has to be awaitable -- the route awaits it.
    async def mock_load():
        return [
            {"id": "mon-1", "brand": "aramis", "url": "url1"},
            {"id": "mon-2", "brand": "ARAMIS", "url": "url2"},
            {"id": "mon-3", "brand": "reserva", "url": "url3"},
        ]
    
    monkeypatch.setattr("services.category_monitor_service.load_monitored_categories", mock_load)
    
    published = []
    
    async def mock_publish(msg):
        published.append(msg)
        
    monkeypatch.setattr("services.rabbitmq_service.rabbitmq_service.publish", mock_publish)
    
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-key")
    response = client.post("/monitor/category/trigger-scan", json={"brand": "aramis"}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.json()
    data = response.json()
    
    assert data["enqueued"] == ["mon-1", "mon-2"]
    assert len(published) == 2
    assert published[0] == {"brand": "aramis", "monitor_id": "mon-1"}
    assert published[1] == {"brand": "ARAMIS", "monitor_id": "mon-2"}

def test_trigger_category_scan_no_active_monitors(monkeypatch):
    async def mock_load():
        return [{"id": "mon-3", "brand": "reserva", "url": "url3"}]
    
    monkeypatch.setattr("services.category_monitor_service.load_monitored_categories", mock_load)
    
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-key")
    response = client.post("/monitor/category/trigger-scan", json={"brand": "aramis"}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.json()
    data = response.json()
    
    assert data["enqueued"] == []
    assert data["reason"] == "Nenhum monitor ativo encontrado para a marca 'aramis'."

def test_trigger_category_scan_handles_broker_error(monkeypatch):
    async def mock_load():
        return [{"id": "mon-1", "brand": "aramis", "url": "url1"}]
    
    monkeypatch.setattr("services.category_monitor_service.load_monitored_categories", mock_load)
    
    async def mock_publish(msg):
        raise RuntimeError("Broker down")
        
    monkeypatch.setattr("services.rabbitmq_service.rabbitmq_service.publish", mock_publish)
    
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-key")
    response = client.post("/monitor/category/trigger-scan", json={"brand": "aramis"}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 503
    assert "Broker down" in response.json()["detail"]
