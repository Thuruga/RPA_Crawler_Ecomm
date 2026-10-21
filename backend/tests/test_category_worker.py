import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from services.category_worker import process_category_scan_message
from core.models import CategoryScanOrigin

@pytest.mark.asyncio
async def test_process_message_valid_payload_runs_scan(monkeypatch):
    # Setup mock message
    msg = MagicMock()
    msg.body = json.dumps({"brand": "aramis", "monitor_id": "mon-1"}).encode("utf-8")
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()

    # Mock load_monitored_categories
    async def mock_load():
        return [{"id": "mon-1", "brand": "aramis"}]
    monkeypatch.setattr("services.category_worker.load_monitored_categories", mock_load)

    # Mock run_category_scan
    mock_run = AsyncMock(return_value="scan_result")
    monkeypatch.setattr("services.category_worker.run_category_scan", mock_run)

    await process_category_scan_message(msg)

    # Verify
    mock_run.assert_called_once_with({"id": "mon-1", "brand": "aramis"}, origin=CategoryScanOrigin.SCHEDULED)
    msg.ack.assert_called_once()
    msg.nack.assert_not_called()

@pytest.mark.asyncio
async def test_process_message_invalid_json_nacks(monkeypatch):
    msg = MagicMock()
    msg.body = b"invalid json"
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()

    await process_category_scan_message(msg)

    msg.ack.assert_not_called()
    msg.nack.assert_called_once_with(requeue=False)

@pytest.mark.asyncio
async def test_process_message_monitor_not_found_acks(monkeypatch):
    msg = MagicMock()
    msg.body = json.dumps({"brand": "aramis", "monitor_id": "mon-1"}).encode("utf-8")
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()

    async def mock_load():
        return [] # No monitors
    monkeypatch.setattr("services.category_worker.load_monitored_categories", mock_load)

    await process_category_scan_message(msg)

    msg.ack.assert_called_once()
    msg.nack.assert_not_called()

@pytest.mark.asyncio
async def test_process_message_scan_error_nacks(monkeypatch):
    msg = MagicMock()
    msg.body = json.dumps({"brand": "aramis", "monitor_id": "mon-1"}).encode("utf-8")
    msg.ack = AsyncMock()
    msg.nack = AsyncMock()

    async def mock_load():
        return [{"id": "mon-1", "brand": "aramis"}]
    monkeypatch.setattr("services.category_worker.load_monitored_categories", mock_load)

    mock_run = AsyncMock(side_effect=RuntimeError("scan failed"))
    monkeypatch.setattr("services.category_worker.run_category_scan", mock_run)

    await process_category_scan_message(msg)

    msg.ack.assert_not_called()
    msg.nack.assert_called_once_with(requeue=False)
