import asyncio
import pytest
import uuid

from core.db import db, ensure_db_connected
from core.models import CategoryScanOrigin, CategoryScanStatus
from services.category_monitor_service import persist_category_scan_to_db
from services.search_history_service import search_history_service


@pytest.mark.asyncio
async def test_persist_category_scan_creates_db_records():
    await ensure_db_connected()

    test_product_code = f"test_prod_{uuid.uuid4().hex[:8]}"
    test_products = [
        {
            "product_code": test_product_code,
            "product_name": "Camisa Polo Teste",
            "url": f"https://www.aramis.com.br/produto-{test_product_code}",
            "price_full": 299.90,
            "price_discount": 199.90,
            "category": "Polos",
            "available_colors": ["Azul", "Branco"],
            "available_sizes": ["M", "G"],
            "stock_availability": True,
        }
    ]

    scan_id = await persist_category_scan_to_db(
        brand_key="aramis",
        category_url="https://www.aramis.com.br/roupas/polos",
        products=test_products,
        origin=CategoryScanOrigin.MANUAL,
    )

    assert scan_id is not None
    assert scan_id > 0

    # Verifica category_scans
    scan_row = await db.category_scans.find_unique(where={"id": scan_id})
    assert scan_row is not None
    assert scan_row.status == CategoryScanStatus.COMPLETED.value
    assert scan_row.product_count == 1
    assert scan_row.origin == "manual"

    # Verifica scan_products
    products_rows = await db.scan_products.find_many(where={"scan_id": scan_id})
    assert len(products_rows) == 1
    saved_prod = products_rows[0]
    assert saved_prod.product_code == test_product_code
    assert saved_prod.raw_title == "Camisa Polo Teste"
    assert float(saved_prod.price_full) == 299.90
    assert float(saved_prod.price_discount) == 199.90
    assert saved_prod.available_colors == ["Azul", "Branco"]

    # Limpeza
    await db.category_scans.delete(where={"id": scan_id})


@pytest.mark.asyncio
async def test_search_job_persists_to_postgresql():
    await ensure_db_connected()

    test_job_id = str(uuid.uuid4())
    search_history_service.create_job(
        job_id=test_job_id,
        query="tênis knit",
        brands=["aramis", "reserva"],
        type="search",
    )

    # Allow fire-and-forget task to execute
    await asyncio.sleep(0.3)

    db_job = await db.search_jobs.find_unique(
        where={"job_id": test_job_id},
        include={"search_job_brands": True}
    )
    assert db_job is not None
    assert db_job.query == "tênis knit"
    assert db_job.status == "PENDING"
    brand_keys = [b.source_key for b in db_job.search_job_brands]
    assert "aramis" in brand_keys
    assert "reserva" in brand_keys

    # Test update status through state machine: PENDING -> RUNNING -> COMPLETED
    search_history_service.update_job(
        job_id=test_job_id,
        status="running",
    )
    await asyncio.sleep(0.3)

    db_job_running = await db.search_jobs.find_unique(where={"job_id": test_job_id})
    assert db_job_running.status == "RUNNING"

    search_history_service.update_job(
        job_id=test_job_id,
        status="completed",
        results=[{"title": "Tênis Aramis Knit", "price": 499.0}],
    )
    await asyncio.sleep(0.3)

    db_job_updated = await db.search_jobs.find_unique(where={"job_id": test_job_id})
    assert db_job_updated.status == "COMPLETED"
    assert db_job_updated.result_payload is not None

    # Test delete
    search_history_service.delete_job(test_job_id)
    await asyncio.sleep(0.3)

    db_job_deleted = await db.search_jobs.find_unique(where={"job_id": test_job_id})
    assert db_job_deleted is None
