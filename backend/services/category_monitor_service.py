from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List
import uuid

from core.constants import MARKETPLACE_ENGINE_KEYS
from core.db import db
from prisma import Json
from core.job_manager import JobManager, job_manager as default_job_manager
from core.models import (
    CategoryMonitorStatus,
    CategoryScanOrigin,
    CategoryScanRecord,
    CategoryScanStatus,
    JobProgress,
)
from services.notification_service import notification_store

logger = logging.getLogger("CategoryMonitor")

_CATEGORY_SCAN_TERMINAL = {
    CategoryScanStatus.COMPLETED,
    CategoryScanStatus.FAILED,
    CategoryScanStatus.INTERRUPTED,
}
_CATEGORY_SCAN_TRANSITIONS = {
    CategoryScanStatus.PENDING: {
        CategoryScanStatus.RUNNING,
        CategoryScanStatus.FAILED,
        CategoryScanStatus.INTERRUPTED,
    },
    CategoryScanStatus.RUNNING: _CATEGORY_SCAN_TERMINAL,
}

# Variáveis legadas mantidas apenas para compatibilidade com monkeypatch de testes legados
DATA_DIR = None
MONITORS_FILE = None
SCANS_FILE = None

_deleted_monitor_ids: set[str] = set()
# Importado de core.constants — NÃO duplicar aqui.
_MARKETPLACES: frozenset[str] = MARKETPLACE_ENGINE_KEYS

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _category_scan_slot_start(instant: datetime) -> datetime:
    instant = instant.astimezone(timezone.utc)
    floored_minute = (instant.minute // 10) * 10
    return instant.replace(minute=floored_minute, second=0, microsecond=0)

async def _get_brand_id(brand_key: str) -> int | None:
    brand = await db.brands.find_first(where={"brand_key": brand_key})
    return int(brand.id) if brand else None

def _map_monitor(m: Any) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "url": m.category_url,
        "brand": m.brands.brand_key if m.brands else "",
        "status": m.status,
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
        "last_scraped_at": m.last_scanned_at.isoformat() if m.last_scanned_at else None,
        "last_scan_id": str(m.last_scan_id) if m.last_scan_id else None,
        "last_scan_status": m.last_scan_status,
    }

def _map_scan(s: Any) -> CategoryScanRecord:
    return CategoryScanRecord(
        scan_id=str(s.id),
        monitor_id=str(s.monitor_id),
        brand=s.brands.brand_key if getattr(s, "brands", None) else "",
        category_url=s.category_url,
        origin=CategoryScanOrigin(s.origin),
        status=CategoryScanStatus(s.status),
        product_count=s.product_count,
        error_message=s.error_message,
        created_at=s.created_at.isoformat() if hasattr(s, "created_at") and s.created_at else None,
        started_at=s.started_at.isoformat() if hasattr(s, "started_at") and s.started_at else None,
        completed_at=s.completed_at.isoformat() if hasattr(s, "completed_at") and s.completed_at else None,
        attempt=1, # simplified
        attempts=[], # simplified
    )

def _safe_int(val: Any) -> int | None:
    try:
        return int(val)
    except (ValueError, TypeError):
        return None

async def seed_category_monitors_if_empty() -> None:
    count = await db.category_monitors.count()
    if count > 0:
        return
    json_path = Path(__file__).resolve().parent.parent / "data" / "monitored_categories.json"
    if not json_path.exists():
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data:
            brand_key = item.get("brand", "").lower()
            url = item.get("url", "")
            if not brand_key or not url:
                continue
            brand_id = await _get_brand_id(brand_key)
            if not brand_id:
                continue
            last_scraped_at = None
            if item.get("last_scraped_at"):
                try:
                    last_scraped_at = datetime.fromisoformat(item["last_scraped_at"])
                except Exception:
                    pass
            await db.category_monitors.upsert(
                where={
                    "brand_id_normalized_url": {
                        "brand_id": brand_id,
                        "normalized_url": url,
                    }
                },
                data={
                    "create": {
                        "brand_id": brand_id,
                        "category_url": url,
                        "normalized_url": url,
                        "status": item.get("status", "active"),
                        "last_scanned_at": last_scraped_at,
                        "last_scan_status": item.get("last_scan_status"),
                    },
                    "update": {},
                }
            )
        logger.info("Migração inicial de categorias monitoradas para o PostgreSQL concluída.")
    except Exception as exc:
        logger.warning("Falha ao semear categorias monitoradas iniciais: %s", exc)

async def load_all_monitored_categories() -> list[dict[str, Any]]:
    await seed_category_monitors_if_empty()
    monitors = await db.category_monitors.find_many(include={"brands": True})
    return [_map_monitor(m) for m in monitors]

async def load_monitored_categories() -> List[Dict[str, Any]]:
    monitors = await db.category_monitors.find_many(
        where={"status": CategoryMonitorStatus.ACTIVE.value},
        include={"brands": True}
    )
    return [_map_monitor(m) for m in monitors]

async def get_category_monitor(monitor_id: str) -> dict[str, Any] | None:
    mid = _safe_int(monitor_id)
    if mid is None:
        return None
    m = await db.category_monitors.find_first(
        where={"id": mid},
        include={"brands": True}
    )
    return _map_monitor(m) if m else None

async def create_category_monitor(url: str, brand: str) -> dict[str, Any]:
    if brand.lower() in _MARKETPLACES:
        raise ValueError(f"Marketplaces não suportam monitor de categoria: {brand}")
    brand_id = await _get_brand_id(brand)
    if not brand_id:
        raise ValueError(f"Brand {brand} not found")
    m = await db.category_monitors.create(
        data={
            "brand_id": brand_id,
            "category_url": url,
            "normalized_url": url,
            "status": CategoryMonitorStatus.ACTIVE.value,
        },
        include={"brands": True}
    )
    _deleted_monitor_ids.discard(str(m.id))
    return _map_monitor(m)

async def set_category_monitor_status(monitor_id: str, status: CategoryMonitorStatus | str) -> dict[str, Any] | None:
    mid = _safe_int(monitor_id)
    if mid is None:
        return None
    normalized = CategoryMonitorStatus(status).value
    try:
        m = await db.category_monitors.update(
            where={"id": mid},
            data={"status": normalized},
            include={"brands": True}
        )
        return _map_monitor(m) if m else None
    except Exception:
        return None

async def pause_category_monitor(monitor_id: str) -> dict[str, Any] | None:
    return await set_category_monitor_status(monitor_id, CategoryMonitorStatus.PAUSED)

async def resume_category_monitor(monitor_id: str) -> dict[str, Any] | None:
    return await set_category_monitor_status(monitor_id, CategoryMonitorStatus.ACTIVE)

async def delete_category_monitor(monitor_id: str) -> bool:
    mid = _safe_int(monitor_id)
    if mid is None:
        return False
    try:
        await db.category_monitors.delete(where={"id": mid})
        _deleted_monitor_ids.add(monitor_id)
        return True
    except Exception:
        return False

async def list_category_scans(monitor_id: str | None = None) -> list[dict[str, Any]]:
    where = {}
    if monitor_id:
        mid = _safe_int(monitor_id)
        if mid is not None:
            where["monitor_id"] = mid
        else:
            return []
    scans = await db.category_scans.find_many(where=where, include={"brands": True})
    return [_map_scan(s).model_dump(mode="json") for s in scans]

async def create_category_scan(monitor: dict[str, Any], *, origin: CategoryScanOrigin | str) -> CategoryScanRecord:
    brand_id = await _get_brand_id(monitor["brand"])
    scan = await db.category_scans.create(
        data={
            "monitor_id": int(monitor["id"]),
            "brand_id": brand_id,
            "category_url": monitor["url"],
            "origin": CategoryScanOrigin(origin).value,
        },
        include={"brands": True}
    )
    return _map_scan(scan)

def _category_scan_logical_cycle_id(monitor_id: str, *, origin: CategoryScanOrigin | str, slot_start_utc: datetime | None = None, scan_id: str | None = None) -> str:
    origin_enum = CategoryScanOrigin(origin)
    if origin_enum == CategoryScanOrigin.SCHEDULED:
        if slot_start_utc is None:
            raise ValueError("scheduled category scan requires slot_start_utc")
        return f"category_scan:{monitor_id}:{slot_start_utc.isoformat()}"
    if not scan_id:
        raise ValueError("manual category scan requires a persisted scan_id")
    return f"category_scan:{monitor_id}:{scan_id}"

async def _get_or_create_category_scan(monitor: dict[str, Any], *, origin: CategoryScanOrigin | str, logical_cycle_id: str, scan_id: str | None, scheduled_for: datetime | None) -> CategoryScanRecord:
    brand_id = await _get_brand_id(monitor["brand"])
    if origin == CategoryScanOrigin.SCHEDULED.value or origin == CategoryScanOrigin.SCHEDULED:
        slot_start_utc = scheduled_for or _category_scan_slot_start(_utc_now())
        existing = await db.category_scans.find_first(
            where={
                "monitor_id": int(monitor["id"]),
                "origin": CategoryScanOrigin.SCHEDULED.value,
                "created_at": slot_start_utc,
            },
            include={"brands": True}
        )
        if existing:
            return _map_scan(existing)
        new_scan = await db.category_scans.create(
            data={
                "monitor_id": int(monitor["id"]),
                "brand_id": brand_id,
                "category_url": monitor["url"],
                "origin": CategoryScanOrigin.SCHEDULED.value,
                "created_at": slot_start_utc,
            },
            include={"brands": True}
        )
        return _map_scan(new_scan)
    else:
        if scan_id:
            existing = await db.category_scans.find_first(where={"id": int(scan_id)}, include={"brands": True})
            if existing:
                return _map_scan(existing)
        new_scan = await db.category_scans.create(
            data={
                "monitor_id": int(monitor["id"]),
                "brand_id": brand_id,
                "category_url": monitor["url"],
                "origin": CategoryScanOrigin(origin).value,
            },
            include={"brands": True}
        )
        return _map_scan(new_scan)

async def _transition_category_scan(scan_id: str, attempt: int, expected_statuses: set[CategoryScanStatus | str], next_status: CategoryScanStatus | str, patch: dict[str, Any] | None = None) -> dict[str, Any] | None:
    expected = {CategoryScanStatus(value).value for value in expected_statuses}
    target = CategoryScanStatus(next_status).value
    scan = await db.category_scans.find_first(where={"id": int(scan_id)})
    if not scan or scan.status not in expected:
        return None
    data = {"status": target}
    if patch:
        if "error_message" in patch:
            data["error_message"] = patch["error_message"]
        if "product_count" in patch:
            data["product_count"] = patch["product_count"]
    
    if target == CategoryScanStatus.RUNNING.value:
        data["started_at"] = scan.started_at or _utc_now()
        data["completed_at"] = None
        data["error_message"] = None
    elif target in [s.value for s in _CATEGORY_SCAN_TERMINAL]:
        data["completed_at"] = _utc_now()
        if target == CategoryScanStatus.COMPLETED.value:
            data["error_message"] = None
            
    updated = await db.category_scans.update(
        where={"id": int(scan_id)},
        data=data,
        include={"brands": True}
    )
    return _map_scan(updated).model_dump(mode="json") if updated else None

async def _update_category_scan_progress(scan_id: str, attempt: int, *, completed_units: int, stage: str = "scraping") -> dict[str, Any] | None:
    scan = await db.category_scans.find_first(where={"id": int(scan_id)})
    if not scan or scan.status != CategoryScanStatus.RUNNING.value:
        return None
    return _map_scan(scan).model_dump(mode="json")

async def _start_new_category_scan_attempt(scan_id: str, previous_attempt: int, *, input: dict[str, Any] | None = None) -> CategoryScanRecord | None:
    scan = await db.category_scans.find_first(where={"id": int(scan_id)}, include={"brands": True})
    if not scan or scan.status != CategoryScanStatus.INTERRUPTED.value:
        return None
    updated = await db.category_scans.update(
        where={"id": int(scan_id)},
        data={
            "status": CategoryScanStatus.PENDING.value,
            "started_at": None,
            "completed_at": None,
            "error_message": None,
        },
        include={"brands": True}
    )
    return _map_scan(updated)

async def _update_monitor_scan_state(monitor_id: str, **changes: Any) -> None:
    data = {}
    if "last_scraped_at" in changes:
        data["last_scanned_at"] = changes["last_scraped_at"]
    if "last_scan_id" in changes:
        data["last_scan_id"] = int(changes["last_scan_id"])
    if "last_scan_status" in changes:
        data["last_scan_status"] = changes["last_scan_status"]
    if data:
        await db.category_monitors.update(where={"id": int(monitor_id)}, data=data)

# Prices detection and history functions
def _product_identity(product: Dict[str, Any]) -> str | None:
    for field in ("url", "sku_id", "product_id", "id", "raw_title"):
        value = product.get(field)
        if value is not None and str(value).strip():
            return f"{field}:{str(value).strip().casefold()}"
    return None

def _clean_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    return s


def _int_value(val: Any) -> int | None:
    if val is None or val == "":
        return None
    try:
        import math
        f = float(val)
        if math.isnan(f):
            return None
        return int(f)
    except (TypeError, ValueError):
        return None


def _parse_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x is not None and str(x).strip()]
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str or val_str in {"[]", "nan", "None"}:
            return []
        if val_str.startswith("[") and val_str.endswith("]"):
            try:
                parsed = json.loads(val_str)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if x is not None and str(x).strip()]
            except Exception:
                pass
        return [s.strip() for s in val_str.split(",") if s.strip()]
    return []


def _money_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        import math
        f = float(value)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None

def _effective_product_price(product: Dict[str, Any]) -> float | None:
    effective_price = _money_value(product.get("effective_price"))
    if effective_price is not None:
        return effective_price
    price_full = _money_value(product.get("price_full"))
    price_discount = _money_value(product.get("price_discount"))
    if price_discount is not None and product.get("price_discount_is_delta") is not True:
        return price_discount
    return price_full

def _product_label(product: Dict[str, Any]) -> str:
    return str(
        product.get("raw_title")
        or product.get("product_name")
        or product.get("name")
        or product.get("url")
        or "Produto monitorado"
    )

def detect_category_price_changes(previous_products: List[Dict[str, Any]], current_products: List[Dict[str, Any]]) -> list[dict[str, Any]]:
    previous_by_key = {key: product for product in previous_products if (key := _product_identity(product)) is not None}
    changes = []
    for product in current_products:
        key = _product_identity(product)
        if key is None or key not in previous_by_key:
            continue
        before_price = _effective_product_price(previous_by_key[key])
        after_price = _effective_product_price(product)
        if before_price is None or after_price is None or before_price == after_price:
            continue
        changes.append({
            "product": product,
            "before": before_price,
            "after": after_price,
        })
    return changes

async def _append_price_history_points(monitor_id: str, products: List[Dict[str, Any]], checked_at: datetime) -> None:
    """Keep the former history hook while scan snapshots are the history store.

    Each completed category scan persists its products in ``scan_products``
    before this hook runs.  Retrieving those immutable snapshots avoids a
    second, divergent history store and makes the timeline durable in
    PostgreSQL.
    """
    return None

async def get_category_price_history_for_scan_product(monitor_id: str, scan_product_id: str) -> list | None:
    """Return a monitor product's timeline from completed scan snapshots.

    ``scan_product_id`` identifies the snapshot selected in the current UI;
    its normalized URL is the stable key used to collect that product's older
    snapshots.  The monitor constraint is repeated in both reads so a UUID or
    URL from another monitor cannot disclose its price history.
    """
    monitor_db_id = _safe_int(monitor_id)
    if monitor_db_id is None:
        return None

    current_snapshot = await db.scan_products.find_first(
        where={
            "scan_product_id": scan_product_id,
            "category_scans": {"is": {"monitor_id": monitor_db_id}},
        }
    )
    if current_snapshot is None:
        return None

    normalized_url = str(getattr(current_snapshot, "normalized_url", "") or "").strip()
    if not normalized_url:
        return []

    snapshots = await db.scan_products.find_many(
        where={
            "normalized_url": normalized_url,
            "category_scans": {
                "is": {
                    "monitor_id": monitor_db_id,
                    "status": CategoryScanStatus.COMPLETED.value,
                }
            },
        },
        order={"captured_at": "asc"},
    )

    points = []
    for snapshot in snapshots:
        price = _money_value(getattr(snapshot, "effective_price", None))
        if price is None:
            price = _money_value(getattr(snapshot, "price_discount", None))
        if price is None:
            price = _money_value(getattr(snapshot, "price_full", None))
        if price is None:
            continue

        price_original = _money_value(getattr(snapshot, "price_full", None)) or price
        captured_at = getattr(snapshot, "captured_at", None)
        timestamp = captured_at.isoformat() if captured_at is not None else ""
        points.append(
            {
                "timestamp": timestamp,
                "price": price,
                "price_original": price_original,
                "available": bool(getattr(snapshot, "stock_availability", False)),
            }
        )
    return points

def notify_category_price_changes(monitor: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    monitor_id = str(monitor.get("id") or "").strip()
    brand = str(monitor.get("brand") or "").strip()
    if not monitor_id:
        return

    for change in changes:
        product = change["product"]
        product_url = str(product.get("url") or "").strip()
        if not product_url:
            continue

        label = _product_label(product)
        prefix = f"{brand} - " if brand else ""
        try:
            notification_store.add_notification(
                "Alteração de Preço",
                f"{prefix}{label} mudou de R$ {change['before']:.2f} para R$ {change['after']:.2f}",
                "monitor",
                route_kind="category-monitor",
                route_monitor_id=monitor_id,
                route_product_url=product_url,
            )
        except Exception:
            logger.exception("Falha ao criar notificação de alteração de preço da categoria")

def _ensure_scan_product_ids(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for product in products:
        if not product.get("scan_product_id"):
            product["scan_product_id"] = str(uuid.uuid4())
    return products

async def _execute_category_scan_attempt(record: CategoryScanRecord, monitor: dict[str, Any]) -> str:
    from services.engines.factory import engine_factory
    scan_id = record.scan_id
    attempt = record.attempt
    monitor_id = str(monitor.get("id") or record.monitor_id)
    brand = monitor.get("brand") or record.brand
    url = monitor.get("url") or record.category_url

    transitioned = await _transition_category_scan(scan_id, attempt, {CategoryScanStatus.PENDING}, CategoryScanStatus.RUNNING)
    if transitioned is None:
        return "already_running"
    await _update_monitor_scan_state(monitor_id, last_scan_id=scan_id, last_scan_status=CategoryScanStatus.RUNNING.value)

    # Compare only against the most recent completed snapshot. The current
    # scan owns fresh snapshot IDs, but the product URL is stable across scans
    # and is persisted as the safe deep-link lookup key after this scan commits.
    previous_products: list[dict[str, Any]] = []
    monitor_db_id = _safe_int(monitor_id)
    if monitor_db_id is not None:
        previous_scan = await db.category_scans.find_first(
            where={"monitor_id": monitor_db_id, "status": CategoryScanStatus.COMPLETED.value},
            order={"id": "desc"},
        )
        if previous_scan is not None:
            previous_rows = await db.scan_products.find_many(where={"scan_id": previous_scan.id})
            previous_products = [
                {
                    "url": getattr(row, "normalized_url", None) or getattr(row, "product_url", None),
                    "sku_id": getattr(row, "sku_id", None),
                    "product_id": getattr(row, "source_product_id", None),
                    "raw_title": getattr(row, "raw_title", None),
                    "effective_price": getattr(row, "effective_price", None),
                    "price_full": getattr(row, "price_full", None),
                    "price_discount": getattr(row, "price_discount", None),
                }
                for row in previous_rows
            ]

    scraped_products: list[dict[str, Any]] = []
    try:
        engine = engine_factory.get_engine(brand)
        async for product in engine.run_bulk_scrape(category_url=url):
            if str(monitor_id) in _deleted_monitor_ids:
                await _transition_category_scan(scan_id, attempt, {CategoryScanStatus.RUNNING}, CategoryScanStatus.INTERRUPTED, {"error_message": "monitor deleted mid-scan"})
                await _update_monitor_scan_state(monitor_id, last_scan_id=scan_id, last_scan_status=CategoryScanStatus.INTERRUPTED.value)
                return "interrupted"
            scraped_products.append(product)
            await _update_category_scan_progress(scan_id, attempt, completed_units=len(scraped_products))
            if len(scraped_products) >= 1000:
                break
    except asyncio.CancelledError:
        await _transition_category_scan(scan_id, attempt, {CategoryScanStatus.RUNNING}, CategoryScanStatus.INTERRUPTED, {"error_message": "cancelled"})
        await _update_monitor_scan_state(monitor_id, last_scan_id=scan_id, last_scan_status=CategoryScanStatus.INTERRUPTED.value)
        raise
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        await _transition_category_scan(scan_id, attempt, {CategoryScanStatus.RUNNING}, CategoryScanStatus.FAILED, {"error_message": error_message, "product_count": 0})
        await _update_monitor_scan_state(monitor_id, last_scan_id=scan_id, last_scan_status=CategoryScanStatus.FAILED.value)
        return "failed"

    if str(monitor_id) in _deleted_monitor_ids:
        await _transition_category_scan(scan_id, attempt, {CategoryScanStatus.RUNNING}, CategoryScanStatus.INTERRUPTED, {"error_message": "monitor deleted mid-scan"})
        await _update_monitor_scan_state(monitor_id, last_scan_id=scan_id, last_scan_status=CategoryScanStatus.INTERRUPTED.value)
        return "interrupted"

    scraped_products = _ensure_scan_product_ids(scraped_products)
    price_changes = detect_category_price_changes(previous_products, scraped_products)
    
    for p in scraped_products:
        price_full = _money_value(p.get("price_full"))
        price_discount = _money_value(p.get("price_discount"))
        effective_price = price_discount if price_discount is not None else price_full
        specs = p.get("specifications") or {}
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except Exception:
                specs = {}
        
        rating_val = _money_value(p.get("rating"))
        review_count_val = int(p["review_count"]) if p.get("review_count") is not None else None
        reviews_state_val = str(
            p.get("reviews_state")
            or ("available" if rating_val is not None or review_count_val is not None else "none")
        )
        review_product_id_val = str(p.get("review_product_id") or p.get("product_code") or "") or None

        await db.scan_products.create(data={
            "scan_id": int(scan_id),
            "scan_product_id": p["scan_product_id"],
            "source_product_id": str(p.get("id")) if p.get("id") else None,
            "product_code": str(p.get("product_code")) if p.get("product_code") else None,
            "product_url": p.get("url", ""),
            "normalized_url": p.get("url", ""),
            "raw_title": p.get("raw_title", "") or p.get("name", "") or p.get("url", ""),
            "raw_description": p.get("raw_description", "") or "",
            "category": p.get("category"),
            "sub_category": p.get("sub_category"),
            "breadcrumb": p.get("breadcrumb") or [],
            "composition": p.get("composition"),
            "image_url": p.get("image_url", ""),
            "price_full": price_full,
            "price_discount": price_discount,
            "effective_price": effective_price,
            "stock_availability": bool(p.get("stock_availability")),
            "available_colors": p.get("available_colors") or [],
            "available_sizes": p.get("available_sizes") or [],
            "raw_colors": p.get("raw_colors") or [],
            "raw_sizes": p.get("raw_sizes") or [],
            "rating": rating_val,
            "review_count": review_count_val,
            "reviews_state": reviews_state_val,
            "review_product_id": review_product_id_val,
            "specifications": json.dumps(specs) if isinstance(specs, dict) else "{}",
        })

        
    completed_at = _utc_now()
    await _append_price_history_points(monitor_id, scraped_products, completed_at)
    await _transition_category_scan(scan_id, attempt, {CategoryScanStatus.RUNNING}, CategoryScanStatus.COMPLETED, {"product_count": len(scraped_products)})
    await _update_monitor_scan_state(monitor_id, last_scraped_at=completed_at, last_scan_id=scan_id, last_scan_status=CategoryScanStatus.COMPLETED.value)
    notify_category_price_changes(monitor, price_changes)
    return "completed"

async def run_category_scan(monitor: dict[str, Any], *, scan_id: str | None = None, origin: CategoryScanOrigin | str = CategoryScanOrigin.SCHEDULED, scheduled_for: datetime | None = None, manager: JobManager | None = None) -> str:
    manager = manager or default_job_manager
    url = monitor.get("url")
    brand = monitor.get("brand")
    monitor_id = monitor.get("id")
    if not url or not brand or not monitor_id:
        return "invalid_monitor"

    origin_enum = CategoryScanOrigin(origin)
    slot_start_utc = scheduled_for or _category_scan_slot_start(_utc_now()) if origin_enum == CategoryScanOrigin.SCHEDULED else None
    logical_cycle_id = _category_scan_logical_cycle_id(str(monitor_id), origin=origin_enum, slot_start_utc=slot_start_utc, scan_id=scan_id)

    claim = await manager.try_claim(logical_cycle_id)
    if claim is None:
        return "already_claimed"

    try:
        record = await _get_or_create_category_scan(monitor, origin=origin, logical_cycle_id=logical_cycle_id, scan_id=scan_id, scheduled_for=scheduled_for)
        if record.status in _CATEGORY_SCAN_TERMINAL:
            return "already_terminal"
        if record.status == CategoryScanStatus.RUNNING:
            return "already_running"
        return await _execute_category_scan_attempt(record, monitor)
    finally:
        await claim.release()

async def reconcile_orphaned_category_scans(*, manager: JobManager | None = None) -> list[str]:
    manager = manager or default_job_manager
    resumed = []
    scans = await db.category_scans.find_many(
        where={"status": {"in": [CategoryScanStatus.PENDING.value, CategoryScanStatus.RUNNING.value]}},
        include={"brands": True}
    )
    for scan in scans:
        scan_id = str(scan.id)
        monitor_id = str(scan.monitor_id)
        if scan.origin == CategoryScanOrigin.SCHEDULED.value:
            slot_start_utc = _category_scan_slot_start(scan.created_at)
            logical_cycle_id = f"category_scan:{monitor_id}:{slot_start_utc.isoformat()}"
        else:
            logical_cycle_id = f"category_scan:{monitor_id}:{scan_id}"
            
        claim = await manager.try_claim(logical_cycle_id)
        if claim is None:
            continue
        try:
            interrupted = await _transition_category_scan(scan_id, 1, {CategoryScanStatus.PENDING, CategoryScanStatus.RUNNING}, CategoryScanStatus.INTERRUPTED, {"error_message": "worker interrupted before boot reconciliation"})
            if interrupted is None:
                continue
            resumed_record = await _start_new_category_scan_attempt(scan_id, 1)
            if resumed_record is None:
                continue
            monitor = await get_category_monitor(monitor_id)
            if not monitor:
                continue
            task = await manager._schedule_claimed(claim, lambda: _execute_category_scan_attempt(resumed_record, monitor), record_id=scan_id)
            if task is not None:
                resumed.append(scan_id)
                claim = None
        finally:
            if claim is not None:
                await claim.release()
    return resumed

async def category_monitor_job() -> list[str]:
    monitors = await load_monitored_categories()
    if not monitors:
        logger.debug("category_monitor_job (10 min): nenhum monitor ativo encontrado.")
        return []

    enqueued = []
    from services.rabbitmq_service import rabbitmq_service
    for monitor in monitors:
        try:
            await rabbitmq_service.publish({"brand": monitor["brand"], "monitor_id": monitor["id"]})
            enqueued.append(monitor["id"])
        except Exception:
            logger.error(
                "Falha ao enfileirar monitor %s (%s) no category_monitor_job (10 min). "
                "Continuando para o próximo monitor.",
                monitor.get("id"),
                monitor.get("brand"),
                exc_info=True,
            )
    logger.info("category_monitor_job (10 min): %d/%d monitores ativos enfileirados.", len(enqueued), len(monitors))
    return enqueued


async def persist_category_scan_to_db(
    brand_key: str,
    category_url: str,
    products: List[Any],
    origin: CategoryScanOrigin | str = "ad_hoc",
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
) -> Optional[int]:
    """
    Persiste uma varredura de categoria e todos os seus produtos no PostgreSQL:
    1. Localiza a marca no banco por brand_key ou brand_name.
    2. Garante a existência do category_monitors para a URL normalizada.
    3. Cria o registro em category_scans com status COMPLETED.
    4. Insere os produtos em scan_products evitando duplicações por product_code.
    5. Atualiza category_monitors com o último scan.
    Retorna o ID do scan criado, ou None em caso de falha.
    """
    if not products:
        return None

    from core.db import ensure_db_connected
    try:
        await ensure_db_connected()
    except Exception as conn_err:
        logger.warning("Falha ao conectar no DB para persistir scan: %s", conn_err)
        return None

    # 1. Localiza a marca
    brand = await db.brands.find_first(where={"brand_key": brand_key.lower()})
    if not brand:
        brand = await db.brands.find_first(where={"brand_name": {"mode": "insensitive", "equals": brand_key}})
    if not brand:
        logger.warning("[persist_category_scan_to_db] Marca '%s' não encontrada no banco.", brand_key)
        return None

    brand_id = int(brand.id)
    normalized_url = category_url.strip()
    completed_at = completed_at or _utc_now()
    started_at = started_at or completed_at

    # 2. Localiza ou cria category_monitors
    monitor = await db.category_monitors.find_first(
        where={"brand_id": brand_id, "normalized_url": normalized_url}
    )
    if not monitor:
        try:
            monitor = await db.category_monitors.create(
                data={
                    "brand_id": brand_id,
                    "category_url": category_url,
                    "normalized_url": normalized_url,
                    "status": "active",
                }
            )
        except Exception:
            # Em caso de race condition no unique constraint, busca novamente
            monitor = await db.category_monitors.find_first(
                where={"brand_id": brand_id, "normalized_url": normalized_url}
            )

    if not monitor:
        logger.error("[persist_category_scan_to_db] Falha ao criar/obter category_monitors para %s (%s)", brand_key, category_url)
        return None

    monitor_id = int(monitor.id)
    valid_origins = {"manual", "scheduled"}
    origin_str = origin.value if isinstance(origin, CategoryScanOrigin) else str(origin).lower()
    if origin_str not in valid_origins:
        origin_str = CategoryScanOrigin.MANUAL.value

    if completed_at < started_at:
        completed_at = started_at

    # 3. Cria o registro de category_scans
    scan = await db.category_scans.create(
        data={
            "monitor_id": monitor_id,
            "brand_id": brand_id,
            "category_url": category_url,
            "origin": origin_str,
            "status": CategoryScanStatus.COMPLETED.value,
            "product_count": len(products),
            "created_at": started_at,
            "started_at": started_at,
            "completed_at": completed_at,
        }
    )
    scan_id = int(scan.id)

    # 4. Insere os produtos em scan_products
    seen_codes = set()
    for idx, raw_p in enumerate(products):
        p = raw_p if isinstance(raw_p, dict) else (raw_p.model_dump(mode="json") if hasattr(raw_p, "model_dump") else vars(raw_p))
        sp_id = str(p.get("scan_product_id") or uuid.uuid4())

        raw_code = p.get("product_code") or p.get("sku") or p.get("sku_id") or p.get("id")
        code_str = str(raw_code).strip() if raw_code else None
        if code_str:
            if code_str in seen_codes:
                code_str = f"{code_str}_{idx}"
            seen_codes.add(code_str)

        price_full = _money_value(p.get("price_full") or p.get("price"))
        price_discount = _money_value(p.get("price_discount"))
        effective_price = _effective_product_price(p)

        specs = p.get("specifications") or {}
        if isinstance(specs, str):
            try:
                specs = json.loads(specs)
            except Exception:
                specs = {}
        elif not isinstance(specs, dict):
            specs = {}

        cond_offers = p.get("conditional_offers") or []
        if isinstance(cond_offers, str):
            try:
                cond_offers = json.loads(cond_offers)
            except Exception:
                cond_offers = []
        elif not isinstance(cond_offers, list):
            cond_offers = []

        rating_val = _money_value(p.get("rating"))
        review_count_val = _int_value(p.get("review_count"))
        reviews_state_val = str(
            p.get("reviews_state")
            or ("available" if rating_val is not None or review_count_val is not None else "none")
        )
        review_product_id_val = _clean_str(p.get("review_product_id") or code_str)

        try:
            await db.scan_products.create(
                data={
                    "scan_id": scan_id,
                    "scan_product_id": sp_id,
                    "source_product_id": _clean_str(p.get("id")),
                    "product_code": _clean_str(code_str),
                    "sku_id": _clean_str(p.get("sku_id") or p.get("sku")),
                    "seller_id": _clean_str(p.get("seller_id")),
                    "product_url": str(p.get("url") or p.get("link") or ""),
                    "normalized_url": str(p.get("url") or p.get("link") or ""),
                    "raw_title": str(p.get("raw_title") or p.get("product_name") or p.get("title") or ""),
                    "raw_description": str(p.get("raw_description") or p.get("product_description") or ""),
                    "category": _clean_str(p.get("category")),
                    "sub_category": _clean_str(p.get("sub_category")),
                    "breadcrumb": _parse_list(p.get("breadcrumb")),
                    "composition": _clean_str(p.get("composition")),
                    "image_url": _clean_str(p.get("image_url")),
                    "seller": _clean_str(p.get("seller")),
                    "price_full": price_full,
                    "price_discount": price_discount,
                    "effective_price": effective_price,
                    "stock_availability": bool(p.get("stock_availability") or p.get("available", True)),
                    "available_colors": _parse_list(p.get("available_colors")),
                    "available_sizes": _parse_list(p.get("available_sizes")),
                    "raw_colors": _parse_list(p.get("raw_colors")),
                    "raw_sizes": _parse_list(p.get("raw_sizes")),
                    "rating": rating_val,
                    "review_count": review_count_val,
                    "reviews_state": reviews_state_val,
                    "review_product_id": review_product_id_val,
                    "specifications": Json(specs),
                    "conditional_offers": Json(cond_offers),
                }
            )
        except Exception as prod_err:
            logger.warning("[persist_category_scan_to_db] Falha ao inserir produto %s: %s", code_str, prod_err)

    # 5. Atualiza category_monitors
    await db.category_monitors.update(
        where={"id": monitor_id},
        data={
            "last_scanned_at": completed_at,
            "last_scan_id": scan_id,
            "last_scan_status": CategoryScanStatus.COMPLETED.value,
        }
    )

    logger.info(
        "[persist_category_scan_to_db] Sucesso! Scan ID #%d criado para %s (%s) com %d produtos.",
        scan_id, brand_key, category_url, len(products)
    )
    return scan_id

