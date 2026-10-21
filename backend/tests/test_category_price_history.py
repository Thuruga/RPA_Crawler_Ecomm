from __future__ import annotations

import json

from services import category_monitor_service as category_service


def _redirect_category_repository(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(category_service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(category_service, "MONITORS_FILE", tmp_path / "monitors.json")
    monkeypatch.setattr(category_service, "SCANS_FILE", tmp_path / "scans.json")
    category_service._deleted_monitor_ids.clear()


def _history_file(tmp_path, monitor_id: str):
    return tmp_path / f"category_price_history_{monitor_id}.json"


def test_append_creates_history_file_with_one_point_per_valid_product(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-1"
    checked_at = "2026-07-24T10:00:00+00:00"

    product_a = {"url": "https://example.test/a", "price_full": "100.00", "stock_availability": True}
    product_b = {
        "url": "https://example.test/b",
        "price_full": "50.00",
        "price_discount": "40.00",
        "stock_availability": False,
    }

    category_service._append_price_history_points(monitor_id, [product_a, product_b], checked_at)

    history = json.loads(_history_file(tmp_path, monitor_id).read_text(encoding="utf-8"))
    assert set(history.keys()) == {
        category_service._product_identity(product_a),
        category_service._product_identity(product_b),
    }

    key_a = category_service._product_identity(product_a)
    key_b = category_service._product_identity(product_b)
    assert history[key_a] == [
        {
            "timestamp": checked_at,
            "price": 100.0,
            "price_original": 100.0,
            "available": True,
        }
    ]
    assert history[key_b] == [
        {
            "timestamp": checked_at,
            "price": 40.0,
            "price_original": 50.0,
            "available": False,
        }
    ]


def test_key_format_matches_product_identity_verbatim(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-key"
    product = {"url": "https://example.test/Key-Product", "price_full": "10.00"}

    category_service._append_price_history_points(monitor_id, [product], "2026-07-24T10:00:00+00:00")

    history = json.loads(_history_file(tmp_path, monitor_id).read_text(encoding="utf-8"))
    expected_key = category_service._product_identity(product)
    assert expected_key is not None
    assert expected_key.startswith("url:")
    assert list(history.keys()) == [expected_key]


def test_append_again_grows_history_without_cap_or_dedupe(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-2"
    product_a = {"url": "https://example.test/a", "price_full": "100.00", "stock_availability": True}
    product_b = {"url": "https://example.test/b", "price_full": "50.00", "stock_availability": True}

    category_service._append_price_history_points(monitor_id, [product_a, product_b], "2026-07-24T10:00:00+00:00")
    category_service._append_price_history_points(monitor_id, [product_a, product_b], "2026-07-24T11:00:00+00:00")

    history = json.loads(_history_file(tmp_path, monitor_id).read_text(encoding="utf-8"))
    key_a = category_service._product_identity(product_a)
    key_b = category_service._product_identity(product_b)
    assert len(history[key_a]) == 2
    assert len(history[key_b]) == 2
    assert history[key_a][0]["timestamp"] == "2026-07-24T10:00:00+00:00"
    assert history[key_a][1]["timestamp"] == "2026-07-24T11:00:00+00:00"


def test_null_price_is_skipped_while_other_products_still_get_points(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-3"
    valid_product = {"url": "https://example.test/valid", "price_full": "20.00"}
    null_price_product = {"url": "https://example.test/null-price", "price_full": None, "price_discount": None}

    category_service._append_price_history_points(
        monitor_id, [valid_product, null_price_product], "2026-07-24T10:00:00+00:00"
    )

    history = json.loads(_history_file(tmp_path, monitor_id).read_text(encoding="utf-8"))
    assert category_service._product_identity(null_price_product) not in history
    assert category_service._product_identity(valid_product) in history
    assert len(history) == 1


def test_missing_identity_is_skipped(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-4"
    no_identity_product = {"price_full": "10.00"}
    valid_product = {"url": "https://example.test/valid", "price_full": "10.00"}

    assert category_service._product_identity(no_identity_product) is None

    category_service._append_price_history_points(
        monitor_id, [no_identity_product, valid_product], "2026-07-24T10:00:00+00:00"
    )

    history = json.loads(_history_file(tmp_path, monitor_id).read_text(encoding="utf-8"))
    assert len(history) == 1
    assert category_service._product_identity(valid_product) in history


def test_get_category_price_history_reads_known_unknown_and_absent(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-5"
    product = {"url": "https://example.test/a", "price_full": "10.00"}
    checked_at = "2026-07-24T10:00:00+00:00"

    category_service._append_price_history_points(monitor_id, [product], checked_at)
    key = category_service._product_identity(product)

    assert category_service.get_category_price_history(monitor_id, key) == [
        {"timestamp": checked_at, "price": 10.0, "price_original": 10.0, "available": False}
    ]
    assert category_service.get_category_price_history(monitor_id, "url:unknown") == []
    assert category_service.get_category_price_history("monitor-without-file", key) == []


def test_get_category_price_history_for_scan_product_resolves_via_monitored_products(
    monkeypatch, tmp_path
) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor_id = "monitor-6"
    tracked_product = {"url": "https://example.test/a", "price_full": "10.00", "scan_product_id": "sp-1"}
    untracked_product = {"url": "https://example.test/b", "price_full": "20.00", "scan_product_id": "sp-2"}
    checked_at = "2026-07-24T10:00:00+00:00"

    (tmp_path / f"monitored_products_{monitor_id}.json").write_text(
        json.dumps([tracked_product, untracked_product]), encoding="utf-8"
    )
    category_service._append_price_history_points(monitor_id, [tracked_product], checked_at)

    resolved = category_service.get_category_price_history_for_scan_product(monitor_id, "sp-1")
    assert resolved == [
        {"timestamp": checked_at, "price": 10.0, "price_original": 10.0, "available": False}
    ]

    # Product exists in the listing but has no points yet.
    assert category_service.get_category_price_history_for_scan_product(monitor_id, "sp-2") == []

    # No product with this scan_product_id in the listing.
    assert category_service.get_category_price_history_for_scan_product(monitor_id, "sp-999") is None


def test_delete_category_monitor_removes_price_history_file(monkeypatch, tmp_path) -> None:
    _redirect_category_repository(monkeypatch, tmp_path)
    monitor = category_service.create_category_monitor("https://example.test/category", "example")
    monitor_id = monitor["id"]
    product = {"url": "https://example.test/a", "price_full": "10.00"}

    category_service._append_price_history_points(monitor_id, [product], "2026-07-24T10:00:00+00:00")
    history_file = _history_file(tmp_path, monitor_id)
    assert history_file.exists()

    assert category_service.delete_category_monitor(monitor_id) is True
    assert not history_file.exists()
