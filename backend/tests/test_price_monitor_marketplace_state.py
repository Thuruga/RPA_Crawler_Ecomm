"""MKT-03 (Fase 8, Plano 03): o ciclo de monitor de preço distingue leitura
nova de leitura reaproveitada. Cobre o gap de Wave 0 confirmado em
08-RESEARCH.md: uma leitura `stale` (dict COMPLETO e válido, reaproveitado do
cache do MarketplaceCoordinator) nunca deve gerar uma observação de preço
fantasma, uma notificação de "mudança de preço" falsa, nem sobrescrever o
último preço bom conhecido."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.models import (
    ConditionalOfferResult,
    PriceCheckState,
    PriceHistoryEntry,
    PriceMonitorConfig,
    PriceMonitorStatus,
)
from services import price_monitor_service
from services.engines.factory import engine_factory


def _service(monkeypatch, tmp_path: Path) -> price_monitor_service.PriceMonitorService:
    monkeypatch.setattr(price_monitor_service, "STORAGE_FILE", tmp_path / "price_monitors.json")
    return price_monitor_service.PriceMonitorService()


def _monitor_config(job_id: str = "job-1", **overrides) -> PriceMonitorConfig:
    now = datetime.now(timezone.utc)
    defaults = dict(
        job_id=job_id,
        url="https://example.test/produto",
        brand="mercado_livre",
        interval_minutes=10,
        start_time=now.isoformat(),
        active=True,
        status=PriceMonitorStatus.ACTIVE,
        next_check_due_at=now,
    )
    defaults.update(overrides)
    return PriceMonitorConfig(**defaults)


def _fake_product(**overrides) -> dict:
    payload = {
        "url": "https://example.test/produto",
        "brand": "mercado_livre",
        "raw_title": "Camisa Teste",
        "price_full": 199.9,
        "stock_availability": True,
    }
    payload.update(overrides)
    return payload


def _patch_engine(monkeypatch, engine) -> None:
    # Patch a CLASSE (get_engine é um @staticmethod) — nunca a instância, para
    # não mascarar o wiring real (mesmo padrão de test_price_check_runs.py).
    monkeypatch.setattr(type(engine_factory), "get_engine", staticmethod(lambda brand: engine))


def _patch_offers(monkeypatch) -> None:
    monkeypatch.setattr(
        price_monitor_service,
        "collect_conditional_offers_for_monitor",
        AsyncMock(return_value=ConditionalOfferResult(status="none_detected", probe_level="full")),
    )


def _spy_notifications(monkeypatch):
    sent_messages: list[dict] = []
    added_notifications: list[dict] = []

    async def _send_message(payload, job_id):
        sent_messages.append(payload)

    def _add_notification(*, title, message, tab):
        added_notifications.append({"title": title, "message": message, "tab": tab})

    monkeypatch.setattr(price_monitor_service.manager, "send_message", _send_message)
    monkeypatch.setattr(price_monitor_service.notification_store, "add_notification", _add_notification)
    return sent_messages, added_notifications


@pytest.mark.asyncio
async def test_stale_read_does_not_create_new_price_observation(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)
    _spy_notifications(monkeypatch)
    service = _service(monkeypatch, tmp_path)

    old_success_at = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    config = _monitor_config(
        job_id="job-stale",
        last_price=100.0,
        last_price_original=None,
        last_price_discount=None,
        last_status=PriceCheckState.OK.value,
        last_success_at=old_success_at,
    )
    config.history.append(
        PriceHistoryEntry(price=100.0, price_original=None, last_price_discount=None, available=True)
    )
    service.monitors[config.job_id] = config

    next_retry_at = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    stale_payload = _fake_product(
        price_full=50.0,  # preço diferente vindo do cache — não deve ser absorvido
        _marketplace_read_state="stale",
        last_success_at=old_success_at,
        next_retry_at=next_retry_at,
    )
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=stale_payload)))

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.STALE
    assert len(config.history) == 1
    assert config.last_price == 100.0
    assert config.last_status == PriceCheckState.STALE.value
    assert config.next_retry_at == next_retry_at
    assert config.last_success_at == old_success_at
    assert error is not None
    assert "anti-bot" not in error.lower()
    assert "preservad" in error.lower()


@pytest.mark.asyncio
async def test_stale_read_emits_no_price_update_notification(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)
    sent_messages, added_notifications = _spy_notifications(monkeypatch)
    service = _service(monkeypatch, tmp_path)

    old_success_at = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    config = _monitor_config(
        job_id="job-stale-notify",
        last_price=100.0,
        last_status=PriceCheckState.OK.value,
        last_success_at=old_success_at,
    )
    config.history.append(
        PriceHistoryEntry(price=100.0, price_original=None, last_price_discount=None, available=True)
    )
    service.monitors[config.job_id] = config

    stale_payload = _fake_product(
        price_full=50.0,
        _marketplace_read_state="stale",
        last_success_at=old_success_at,
        next_retry_at=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
    )
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=stale_payload)))

    await service._execute_price_check(config.job_id, config)

    assert not any(msg.get("type") == "price_update" for msg in sent_messages)
    assert not any("Alteração de Preço" in n["title"] for n in added_notifications)


@pytest.mark.asyncio
async def test_blocked_read_without_cache_maps_to_blocked_state(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)
    _spy_notifications(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-blocked")
    service.monitors[config.job_id] = config

    next_retry_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    blocked_payload = {
        "_marketplace_read_state": "blocked",
        "last_success_at": None,
        "next_retry_at": next_retry_at,
    }
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=blocked_payload)))

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.BLOCKED
    assert config.last_status == PriceCheckState.BLOCKED.value
    assert config.next_retry_at == next_retry_at
    assert len(config.history) == 0


@pytest.mark.asyncio
async def test_unsupported_read_maps_to_error_state(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)
    _spy_notifications(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-unsupported")
    service.monitors[config.job_id] = config

    unsupported_payload = {
        "_marketplace_read_state": "unsupported",
        "last_success_at": None,
        "next_retry_at": None,
    }
    _patch_engine(
        monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=unsupported_payload))
    )

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.ERROR
    assert config.last_status == PriceCheckState.ERROR.value
    assert "não é suportada" in config.last_error.lower() or "nao e suportada" in config.last_error.lower()


@pytest.mark.asyncio
async def test_current_read_still_records_observation_and_success(monkeypatch, tmp_path: Path):
    _patch_offers(monkeypatch)
    _spy_notifications(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-current", last_price=100.0)
    config.history.append(
        PriceHistoryEntry(price=100.0, price_original=None, last_price_discount=None, available=True)
    )
    service.monitors[config.job_id] = config

    current_payload = _fake_product(
        price_full=150.0,
        _marketplace_read_state="current",
        last_success_at=datetime.now(timezone.utc).isoformat(),
        next_retry_at=(datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
    )
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=current_payload)))

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.OK
    assert len(config.history) == 2
    assert config.last_price == 150.0
    assert config.last_status == PriceCheckState.OK.value
    assert config.last_success_at == config.last_checked_at


@pytest.mark.asyncio
async def test_non_marketplace_engine_without_read_state_behaves_as_before(
    monkeypatch, tmp_path: Path
):
    _patch_offers(monkeypatch)
    _spy_notifications(monkeypatch)
    service = _service(monkeypatch, tmp_path)
    config = _monitor_config(job_id="job-vtex", brand="aramis")
    service.monitors[config.job_id] = config

    # Engine de marca própria (VTEX/Shopify/...) não passa pelo wrapper: o
    # dict devolvido não tem nenhuma das 3 chaves extras.
    plain_payload = _fake_product(brand="aramis")
    _patch_engine(monkeypatch, SimpleNamespace(get_pdp_product=AsyncMock(return_value=plain_payload)))

    state, error = await service._execute_price_check(config.job_id, config)

    assert state == PriceCheckState.OK
    assert len(config.history) == 1
    assert config.next_retry_at is None
