"""Literal backend-to-frontend wire contract for conditional offers."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from api import routes_product, routes_promotions
from core.models import (
    ConditionalOffer,
    ConditionalOfferEvent,
    ConditionalOfferResult,
    ConditionalOfferSnapshot,
    OfferState,
    OfferScope,
    OfferSource,
    PriceHistoryEntry,
    PriceMonitorConfig,
)


FIXTURE = Path(__file__).parent / "fixtures" / "phase03" / "conditional_offer_wire.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _offer() -> ConditionalOffer:
    return ConditionalOffer(
        offer_key="membership:cliente-prime:sku-638383",
        condition_type="membership",
        condition_label="Cliente Prime",
        eligibility="requires_membership",
        price=Decimal("509.15"),
        effective_unit_price=Decimal("509.15"),
        discount_value=Decimal("15"),
        discount_unit="percent",
        currency="BRL",
        scope=OfferScope(product_id="104962", sku_id="638383", seller_id="1"),
        state="confirmed",
        source=OfferSource(
            kind="brand_custom_api",
            ref="/_v/rsv-prime/primepromotion",
            authority="authoritative",
            observed_at="2026-07-14T00:00:00Z",
        ),
        raw_text="Cliente Prime - 15% OFF",
        observed_from="2026-07-14T00:00:00Z",
    )


def _result() -> ConditionalOfferResult:
    return ConditionalOfferResult(
        status="found",
        offers=[_offer()],
        checked_at="2026-07-14T00:00:00Z",
        probe_level="full",
        sources_attempted=["reserva_prime"],
    )


def _config() -> PriceMonitorConfig:
    current = ConditionalOfferSnapshot(**_offer().model_dump())
    before = current.model_copy(
        update={
            "price": Decimal("599.00"),
            "effective_unit_price": Decimal("599.00"),
            "state": OfferState.ADVERTISED,
        }
    )
    event = ConditionalOfferEvent(
        event_type="changed",
        offer_key=current.offer_key,
        before=before,
        after=current,
        observed_from="2026-07-14T00:00:00Z",
        created_at="2026-07-14T00:05:00Z",
    )
    return PriceMonitorConfig(
        job_id="job-wire-1",
        url="https://www.usereserva.com/camisa-ml-flanela-speed/p",
        brand="reserva",
        start_time="2026-07-14T00:00:00Z",
        last_price=599.0,
        last_price_original=599.0,
        last_status="ok",
        last_checked_at="2026-07-14T00:05:00Z",
        history=[
            PriceHistoryEntry(
                timestamp="2026-07-14T00:00:00Z",
                price=599.0,
                available=True,
            )
        ],
        conditional_offers_current=[current],
        conditional_offers_status="found",
        conditional_offers_checked_at="2026-07-14T00:05:00Z",
        conditional_offer_history=[event],
        product_name="Camisa ML Flanela Speed",
    )


def _offer_wire(value: dict) -> dict:
    keys = (
        "offer_key",
        "condition_type",
        "condition_label",
        "eligibility",
        "price",
        "effective_unit_price",
        "discount_value",
        "discount_unit",
        "currency",
        "state",
    )
    return {key: value.get(key) for key in keys}


def _config_wire(value: dict) -> dict:
    return {
        "job_id": value["job_id"],
        "url": value["url"],
        "brand": value["brand"],
        "history": [
            {"timestamp": item["timestamp"], "price": item["price"]}
            for item in value["history"]
        ],
        "conditional_offers_current": [
            _offer_wire(item) for item in value["conditional_offers_current"]
        ],
        "conditional_offers_status": value.get("conditional_offers_status"),
        "conditional_offers_checked_at": value.get("conditional_offers_checked_at"),
        "conditional_offers_diagnostics": value.get("conditional_offers_diagnostics", []),
        "conditional_offer_history": [
            {
                "event_type": event["event_type"],
                "offer_key": event["offer_key"],
                "before": _offer_wire(event["before"]),
                "after": _offer_wire(event["after"]),
                "observed_from": event.get("observed_from"),
                "observed_until": event.get("observed_until"),
                "created_at": event["created_at"],
            }
            for event in value["conditional_offer_history"]
        ],
    }


def _result_wire(value: dict) -> dict:
    return {
        "status": value["status"],
        "offers": [_offer_wire(item) for item in value["offers"]],
        "checked_at": value["checked_at"],
        "probe_level": value["probe_level"],
        "sources_attempted": value["sources_attempted"],
        "diagnostics": value["diagnostics"],
        "cache": value["cache"],
    }


def _mutation_wire(value: dict) -> dict:
    config = value["config"]
    event = config["conditional_offer_history"][0]
    return {
        "job_id": value["job_id"],
        "status": value["status"],
        "config": {
            "job_id": config["job_id"],
            "url": config["url"],
            "brand": config["brand"],
            "conditional_offers_current": [
                {
                    key: config["conditional_offers_current"][0][key]
                    for key in ("price", "effective_unit_price", "discount_value", "discount_unit")
                }
            ],
            "conditional_offer_history": [
                {
                    "before": {
                        key: event["before"][key] for key in ("price", "discount_unit")
                    },
                    "after": {
                        key: event["after"][key] for key in ("price", "discount_unit")
                    },
                }
            ],
        },
    }


def test_pydantic_model_dump_json_matches_literal_decimal_wire() -> None:
    wire = _fixture()["wire"]
    result_json = json.loads(_result().model_dump_json())
    config_json = json.loads(_config().model_dump_json())

    assert _result_wire(result_json) == wire["conditional_offer_result"]
    assert _config_wire(config_json) == wire["price_monitor_config"]
    assert result_json["offers"][0]["price"] == "509.15"
    assert result_json["offers"][0]["discount_value"] == "15"
    assert result_json["offers"][0]["discount_unit"] == "percent"


def test_fastapi_routes_preserve_wire_for_detail_current_history_and_mutations(monkeypatch) -> None:
    wire = _fixture()["wire"]
    config = _config()
    app = FastAPI()
    app.include_router(routes_promotions.router)
    app.include_router(routes_product.router)

    monkeypatch.setattr(
        routes_promotions,
        "_validated_brand",
        lambda *_args: SimpleNamespace(brand_key="reserva"),
    )
    monkeypatch.setattr(
        routes_promotions,
        "promotion_orchestrator",
        AsyncMock(return_value=_result()),
    )
    monkeypatch.setattr(routes_product.monitor_service, "monitors", {config.job_id: config})
    start = AsyncMock(return_value=(config, "created"))
    monkeypatch.setattr(routes_product.monitor_service, "start_monitor", start)
    monkeypatch.setattr(routes_product.monitor_service, "resume_monitor", AsyncMock())

    client = TestClient(app)
    detail = client.post(
        "/search/product/conditional-offers",
        json={
            "brand": "reserva",
            "url": config.url,
            "trigger": "product_opened",
            "force": False,
        },
    )
    assert detail.status_code == 200
    assert _result_wire(detail.json()) == wire["conditional_offer_result"]

    listed = client.get("/monitors")
    history = client.get(f"/monitor/history/{config.job_id}")
    assert _config_wire(listed.json()[0]) == wire["price_monitor_config"]
    # Phase 07 Plan 12: /monitor/history/{job_id} is now typed
    # response_model=PriceMonitorConfig (JOB-01 check_runs honesty), so it
    # serializes through the same Pydantic JSON-mode path as /monitors —
    # Decimal-as-string, consistent with both GET routes returning the same
    # resource shape. It no longer goes through the untyped
    # `.model_dump()` + jsonable_encoder Decimal-as-float path.
    assert _config_wire(history.json()) == wire["price_monitor_config"]

    created = client.post(
        "/monitor/start",
        json={"url": config.url, "brand": "reserva", "interval": 10, "duration": 24},
    )
    assert _mutation_wire(created.json()) == wire["start_monitor"]

    start.return_value = (config, "already_active")
    existing = client.post(
        "/monitor/start",
        json={"url": config.url, "brand": "reserva", "interval": 10, "duration": 24},
    )
    assert _mutation_wire(existing.json()) == wire["add_to_monitor"]

    resumed = client.post(f"/monitor/resume/{config.job_id}")
    assert resumed.json() == wire["resume_status_only"]
    # /monitor/resume/{job_id} does not itself return a config today; this
    # synthesizes the shape an eventual "resume-with-config" response would
    # have, mirroring exactly how /monitor/start's real response is built
    # (`config.model_dump()` run through FastAPI's untyped jsonable_encoder,
    # i.e. Decimal-as-float) rather than reusing `history.json()`, whose
    # Decimal representation is intentionally decoupled from this route by
    # Phase 07 Plan 12's response_model typing (see assertion above).
    optional_config = {
        "status": "resumed",
        "config": jsonable_encoder(config.model_dump()),
    }
    assert _mutation_wire({"job_id": config.job_id, **optional_config})["config"] == wire["resume_with_config"]["config"]
