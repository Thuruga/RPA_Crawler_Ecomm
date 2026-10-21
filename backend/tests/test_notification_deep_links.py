from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from api import routes_search
from services.job_repository import JsonJobRepository
from services.notification_service import NotificationStore
from services.search_history_service import SearchHistoryService
from services import category_monitor_service as category_service


def test_notification_route_target_is_all_or_none_and_closed():
    store = NotificationStore()
    store.add_notification("Informativa", "Sem destino", "monitor")
    assert "route_target" not in store.get_all()[0]

    with pytest.raises(ValueError):
        store.add_notification("Parcial", "Inválida", "search", route_kind="search")
    with pytest.raises(ValueError):
        store.add_notification("Parcial", "Inválida", "search", route_job_id="job-1")
    with pytest.raises(ValueError):
        store.add_notification("Kind", "Inválida", "search", route_kind="other", route_job_id="job-1")
    with pytest.raises(ValueError):
        store.add_notification("ID", "Inválida", "search", route_kind="search", route_job_id="  ")

    store.add_notification("Concluída", "Abra", "search", route_kind="search", route_job_id="job/id 1")
    assert store.get_all()[0]["route_target"] == {"kind": "search", "jobId": "job/id 1"}

    store.add_notification(
        "Preço alterado",
        "Abra o monitor",
        "monitor",
        route_kind="monitor",
        route_monitor_id="monitor/id 1",
    )
    assert store.get_all()[0]["route_target"] == {"kind": "monitor", "monitorId": "monitor/id 1"}

    with pytest.raises(ValueError):
        store.add_notification("Parcial", "Inválida", "monitor", route_kind="monitor")
    with pytest.raises(ValueError):
        store.add_notification("Parcial", "Inválida", "monitor", route_monitor_id="monitor-1")
    with pytest.raises(ValueError):
        store.add_notification(
            "Mista", "Inválida", "monitor", route_kind="monitor", route_job_id="job-1"
        )
    with pytest.raises(ValueError):
        store.add_notification(
            "Vazia", "Inválida", "monitor", route_kind="monitor", route_monitor_id="  "
        )

    store.add_notification(
        "Preço alterado",
        "Abra o produto da categoria",
        "monitor",
        route_kind="category-monitor",
        route_monitor_id="category-1",
        route_product_url="https://example.test/produto/1",
    )
    assert store.get_all()[0]["route_target"] == {
        "kind": "category-monitor",
        "monitorId": "category-1",
        "productUrl": "https://example.test/produto/1",
    }

    with pytest.raises(ValueError):
        store.add_notification(
            "Parcial", "Inválida", "monitor", route_kind="category-monitor", route_monitor_id="category-1"
        )


def test_category_price_change_notification_targets_the_durable_product_url(monkeypatch):
    notifications = []
    monkeypatch.setattr(
        category_service.notification_store,
        "add_notification",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    category_service.notify_category_price_changes(
        {"id": "12", "brand": "dafiti"},
        [{
            "product": {
                "url": "https://example.test/camisa",
                "raw_title": "Camisa Azul",
            },
            "before": 234.24,
            "after": 199.90,
        }],
    )

    assert notifications == [(
        ("Alteração de Preço", "dafiti - Camisa Azul mudou de R$ 234.24 para R$ 199.90", "monitor"),
        {
            "route_kind": "category-monitor",
            "route_monitor_id": "12",
            "route_product_url": "https://example.test/camisa",
        },
    )]


@pytest.mark.asyncio
async def test_completed_comparative_notification_uses_persisted_job_id(
    monkeypatch, tmp_path: Path
):
    # Phase 07 Plan 04 (D-08): a busca agora roda em background — o worker
    # (não mais o handler síncrono) é quem dispara a notificação terminal.
    # O teste chama o worker diretamente com o payload persistido, exatamente
    # como o JobManager faria após o 202.
    service = SearchHistoryService(JsonJobRepository(tmp_path / "search_history.json"))
    notifications = []
    monkeypatch.setattr(uuid, "uuid4", lambda: "search-job")
    monkeypatch.setattr(routes_search.brand_service, "list_brands", lambda active_only=True: [SimpleNamespace(brand_key="aramis")])

    async def search_all_brands(**kwargs):
        return []

    monkeypatch.setattr(routes_search.engine_factory, "search_all_brands", search_all_brands)
    monkeypatch.setattr(routes_search, "search_history_service", service)
    monkeypatch.setattr(
        "services.notification_service.notification_store.add_notification",
        lambda *args, **kwargs: notifications.append(kwargs),
    )

    created = service.create_job(
        job_id="search-job", query="camisa", brands=["aramis"], type="search"
    )
    await service.run_search_job("search-job", created.input)

    final = service.get_job("search-job")
    assert final is not None and final.status == "COMPLETED"
    assert notifications[-1]["route_kind"] == "search"
    assert notifications[-1]["route_job_id"] == "search-job"


@pytest.mark.asyncio
async def test_completed_cross_notification_uses_persisted_job_id(
    monkeypatch, tmp_path: Path
):
    service = SearchHistoryService(JsonJobRepository(tmp_path / "search_history.json"))
    notifications = []
    monkeypatch.setattr(uuid, "uuid4", lambda: "cross-job")
    monkeypatch.setattr(routes_search, "search_history_service", service)
    monkeypatch.setattr(
        "services.notification_service.notification_store.add_notification",
        lambda *args, **kwargs: notifications.append(kwargs),
    )

    async def compare_product(**kwargs):
        return {}

    monkeypatch.setattr(
        "services.cross_marketplace_service.cross_marketplace_service.compare_product",
        compare_product,
    )

    created = service.create_job(
        job_id="cross-job",
        query="SKU: SKU-1",
        brands=["mercadolivre", "netshoes", "amazon"],
        type="cross",
        target_sku="SKU-1",
        search_query="camisa",
    )
    await service.run_cross_search_job("cross-job", created.input)

    final = service.get_job("cross-job")
    assert final is not None and final.status == "COMPLETED"
    assert notifications[-1]["route_kind"] == "cross"
    assert notifications[-1]["route_job_id"] == "cross-job"
