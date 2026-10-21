from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import routes_monitor
from services import category_monitor_service as category_service


@pytest.mark.asyncio
async def test_category_price_history_rebuilds_completed_snapshots_for_current_product(
    monkeypatch,
) -> None:
    current_snapshot = SimpleNamespace(normalized_url="https://example.test/product")
    snapshots = [
        SimpleNamespace(
            captured_at=datetime(2026, 9, 23, 10, tzinfo=timezone.utc),
            effective_price=Decimal("120.00"),
            price_full=Decimal("120.00"),
            stock_availability=True,
        ),
        SimpleNamespace(
            captured_at=datetime(2026, 9, 24, 10, tzinfo=timezone.utc),
            effective_price=Decimal("95.00"),
            price_full=Decimal("120.00"),
            stock_availability=False,
        ),
    ]
    scan_products = SimpleNamespace(
        find_first=AsyncMock(return_value=current_snapshot),
        find_many=AsyncMock(return_value=snapshots),
    )
    monkeypatch.setattr(category_service, "db", SimpleNamespace(scan_products=scan_products))

    points = await category_service.get_category_price_history_for_scan_product(
        "12", "11111111-1111-1111-1111-111111111111"
    )

    assert points == [
        {
            "timestamp": "2026-09-23T10:00:00+00:00",
            "price": 120.0,
            "price_original": 120.0,
            "available": True,
        },
        {
            "timestamp": "2026-09-24T10:00:00+00:00",
            "price": 95.0,
            "price_original": 120.0,
            "available": False,
        },
    ]
    scan_products.find_first.assert_awaited_once_with(
        where={
            "scan_product_id": "11111111-1111-1111-1111-111111111111",
            "category_scans": {"is": {"monitor_id": 12}},
        }
    )
    scan_products.find_many.assert_awaited_once_with(
        where={
            "normalized_url": "https://example.test/product",
            "category_scans": {"is": {"monitor_id": 12, "status": "completed"}},
        },
        order={"captured_at": "asc"},
    )


@pytest.mark.asyncio
async def test_monitored_product_price_history_awaits_history_service(monkeypatch) -> None:
    points = [
        {
            "timestamp": "2026-09-25T10:00:00+00:00",
            "price": 99.9,
            "price_original": 119.9,
            "available": True,
        }
    ]
    history_service = AsyncMock(return_value=points)
    monkeypatch.setattr(
        routes_monitor,
        "get_category_price_history_for_scan_product",
        history_service,
    )

    response = await routes_monitor.get_monitored_product_price_history("1", "product-1")

    assert response == {"points": points}
    history_service.assert_awaited_once_with("1", "product-1")


def test_monitored_product_price_history_serializes_resolved_points(monkeypatch) -> None:
    points = [
        {
            "timestamp": "2026-09-25T10:00:00+00:00",
            "price": 99.9,
            "price_original": 119.9,
            "available": True,
        }
    ]
    history_service = AsyncMock(return_value=points)
    monkeypatch.setattr(
        routes_monitor,
        "get_category_price_history_for_scan_product",
        history_service,
    )
    app = FastAPI()
    app.include_router(routes_monitor.router)

    response = TestClient(app, raise_server_exceptions=False).get(
        "/monitor/category/1/products/product-1/price-history"
    )

    assert response.status_code == 200
    assert response.json() == {"points": points}
    history_service.assert_awaited_once_with("1", "product-1")


@pytest.mark.asyncio
async def test_monitored_product_price_history_preserves_missing_product_404(monkeypatch) -> None:
    history_service = AsyncMock(return_value=None)
    monkeypatch.setattr(
        routes_monitor,
        "get_category_price_history_for_scan_product",
        history_service,
    )

    with pytest.raises(HTTPException) as raised:
        await routes_monitor.get_monitored_product_price_history("1", "missing-product")

    assert raised.value.status_code == 404
    history_service.assert_awaited_once_with("1", "missing-product")
