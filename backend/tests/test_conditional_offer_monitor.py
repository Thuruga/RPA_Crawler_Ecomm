"""Conditional-offer lifecycle and persistence contracts (Plan 03-05)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services import price_monitor_service as monitor
from core.models import ConditionalOffer, ConditionalOfferResult, OfferScope, OfferSource, PriceMonitorConfig
from services.promotions.orchestrator import PromotionOrchestrator

def test_monitor_contract_exposes_bounded_history_limit():
    assert monitor.CONDITIONAL_OFFER_HISTORY_LIMIT == 200


def _offer(price=509.15, state="advertised"):
    return {"offer_key": "membership:prime:sku-1:1", "condition_label": "Cliente Prime", "price": price, "state": state, "observed_from": "2026-07-14T00:00:00Z"}


def _canonical_offer(price="509.15", state="advertised", *, source=None, offer_key="membership:prime:sku-1:1"):
    return ConditionalOffer(
        offer_key=offer_key,
        condition_type="membership",
        condition_label="Cliente Prime",
        eligibility="requires_membership",
        price=price,
        state=state,
        scope=OfferScope(sku_id="sku-1"),
        source=source or OfferSource(kind="fixture", ref="fixture", authority="advertised"),
    )


@pytest.mark.parametrize("before,after,status,event", [
    ([], [_offer()], "found", "started"),
    ([_offer()], [_offer(price=499.0)], "found", "changed"),
    ([_offer()], [_offer(state="confirmed")], "found", "state_changed"),
    ([_offer()], [], "none_detected", "ended"),
])
def test_d17_started_changed_state_changed_and_ended_are_deterministic(before, after, status, event):
    current, events = monitor.diff_conditional_offers(before, after, collection_status=status, checked_at="2026-07-14T01:00:00Z")
    assert [item.event_type for item in events] == [event]
    if event == "ended":
        assert events[0].after.state == "expired"


@pytest.mark.parametrize("status", ["blocked", "error", "timeout", "partial"])
def test_d17_inconclusive_collection_preserves_current_and_never_emits_ended(status):
    current, events = monitor.diff_conditional_offers([_offer()], [], collection_status=status, checked_at="2026-07-14T01:00:00Z")
    assert current == [_offer()]
    assert all(event.event_type != "ended" for event in events)


def test_d17_legacy_record_loads_and_history_is_bounded_to_200(tmp_path: Path):
    path = tmp_path / "monitors.json"
    path.write_text(json.dumps([{"id": "legacy", "url": "https://example.test/p", "last_price": 599.0}]), encoding="utf-8")
    loaded = monitor.load_monitors(path)
    assert loaded[0].conditional_offers_current == [] and loaded[0].conditional_offer_history == []
    loaded[0].conditional_offer_history = [monitor.ConditionalOfferEvent(event_type="changed", offer_key=str(i)) for i in range(250)]
    monitor.save_monitors(path, loaded)
    assert len(monitor.load_monitors(path)[0].conditional_offer_history) == 200


@pytest.mark.asyncio
async def test_d18_conditional_event_uses_separate_websocket_type_and_notification_title():
    websocket = AsyncMock()
    notifier = AsyncMock()
    await monitor.emit_conditional_offer_event(_offer(), "started", websocket=websocket, notifier=notifier)
    payload = websocket.await_args.args[0]
    assert payload["type"] == "conditional_offer_update"
    assert payload["type"] != "price_update"
    assert "preço" not in notifier.await_args.kwargs["title"].lower()


def test_d18_interrupted_atomic_replace_keeps_previous_valid_json(tmp_path: Path, monkeypatch):
    path = tmp_path / "monitors.json"
    path.write_text('[{"id":"original"}]', encoding="utf-8")
    monkeypatch.setattr(monitor.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("fixture failure")))
    with pytest.raises(OSError):
        monitor.atomic_write_json(path, [{"id": "new"}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"id": "original"}]


@pytest.mark.asyncio
async def test_monitor_uses_confirming_start_trigger_once_then_cached_cycle(monkeypatch):
    config = PriceMonitorConfig(job_id="job-1", url="https://example.test/p", brand="brand")
    collect = AsyncMock(return_value=ConditionalOfferResult(status="none_detected", probe_level="full"))
    monkeypatch.setattr(monitor, "collect_conditional_offers_for_monitor", collect)

    await monitor.refresh_conditional_offers_for_monitor(config, job_id=config.job_id)
    await monitor.refresh_conditional_offers_for_monitor(config, job_id=config.job_id)

    assert [call.kwargs["trigger"] for call in collect.await_args_list] == [
        "monitor_started",
        "monitor_cycle",
    ]


@pytest.mark.asyncio
async def test_monitor_cycle_never_runs_confirmation_even_when_forced():
    advertised = _canonical_offer()
    confirm = AsyncMock(return_value=ConditionalOfferResult(
        status="found",
        offers=[_canonical_offer(state="confirmed")],
        probe_level="confirm",
    ))
    orchestrator = PromotionOrchestrator(
        probe=lambda _level: ConditionalOfferResult(status="found", offers=[advertised]),
        confirm=confirm,
    )

    await orchestrator.collect(
        brand="brand", url="https://example.test/p", trigger="monitor_started", force=True,
    )
    await orchestrator.collect(
        brand="brand", url="https://example.test/p", trigger="monitor_cycle", force=True,
    )

    assert confirm.await_count == 1


def test_service_atomic_reload_preserves_current_and_bounded_events(tmp_path: Path, monkeypatch):
    storage = tmp_path / "price_monitors.json"
    monkeypatch.setattr(monitor, "STORAGE_FILE", storage)
    service = monitor.PriceMonitorService()
    config = PriceMonitorConfig(
        job_id="job-1", url="https://example.test/p", brand="brand", active=False,
        conditional_offers_current=[_canonical_offer().model_dump(mode="json")],
        conditional_offer_history=[
            monitor.ConditionalOfferEvent(event_type="changed", offer_key=str(index))
            for index in range(205)
        ],
    )
    service.monitors[config.job_id] = config
    # The service persists to Postgres now, so round-trip through the
    # module-level file helpers instead -- they are what still applies
    # CONDITIONAL_OFFER_HISTORY_LIMIT, which is the cap under test here.
    monitor.save_monitors(storage, service.monitors.values())

    reloaded = monitor.PriceMonitorService()
    for loaded in monitor.load_monitors(storage):
        reloaded.monitors[loaded.job_id] = loaded

    assert reloaded.monitors[config.job_id].conditional_offers_current[0].offer_key == "membership:prime:sku-1:1"
    assert len(reloaded.monitors[config.job_id].conditional_offer_history) == 200


def test_source_transition_preserves_one_timeline_without_ended_or_started():
    config = PriceMonitorConfig(job_id="job-1", url="https://example.test/p", brand="brand")
    advertised = _canonical_offer(
        state="advertised",
        offer_key=None,
        source=OfferSource(kind="html", ref="/produto", authority="advertised"),
    )
    confirmed = _canonical_offer(
        state="confirmed",
        offer_key=None,
        source=OfferSource(
            kind="vtex_checkout", ref="/api/checkout", authority="authoritative"
        ),
    )

    first = monitor.apply_conditional_offer_result(
        config,
        ConditionalOfferResult(
            status="found",
            offers=[advertised],
            probe_level="full",
            checked_at="2026-07-14T00:00:00Z",
        ),
    )
    second = monitor.apply_conditional_offer_result(
        config,
        ConditionalOfferResult(
            status="found",
            offers=[confirmed],
            probe_level="confirm",
            checked_at="2026-07-14T01:00:00Z",
        ),
    )

    assert [event.event_type.value for event in first] == ["started"]
    assert [event.event_type.value for event in second] == ["state_changed"]
    assert all(event.event_type.value not in {"ended", "started"} for event in second)
    assert len(config.conditional_offers_current) == 1
    assert config.conditional_offers_current[0].observed_from == "2026-07-14T00:00:00Z"
    assert advertised.offer_key == confirmed.offer_key


def test_source_ref_change_is_silent_but_real_price_change_is_changed():
    before = _canonical_offer(
        offer_key=None,
        source=OfferSource(kind="html", ref="/produto?v=1", authority="advertised"),
    )
    ref_changed = _canonical_offer(
        offer_key=None,
        source=OfferSource(kind="html", ref="/produto?v=2", authority="advertised"),
    )
    current, ref_events = monitor.diff_conditional_offers(
        [before],
        [ref_changed],
        collection_status="found",
        checked_at="2026-07-14T01:00:00Z",
    )
    _, price_events = monitor.diff_conditional_offers(
        current,
        [_canonical_offer(price="499.00", offer_key=None, source=ref_changed.source)],
        collection_status="found",
        checked_at="2026-07-14T02:00:00Z",
    )

    assert ref_events == []
    assert [event.event_type.value for event in price_events] == ["changed"]


def test_conclusive_observation_correlates_legacy_source_dependent_key():
    legacy = _canonical_offer(
        state="advertised",
        offer_key="legacy:html:key",
        source=OfferSource(kind="html", ref="/produto", authority="advertised"),
    )
    canonical = _canonical_offer(
        state="confirmed",
        offer_key=None,
        source=OfferSource(
            kind="vtex_checkout", ref="/api/checkout", authority="authoritative"
        ),
    )

    current, events = monitor.diff_conditional_offers(
        [legacy],
        [canonical],
        collection_status="found",
        checked_at="2026-07-14T01:00:00Z",
    )

    assert current[0].offer_key == "legacy:html:key"
    assert [event.event_type.value for event in events] == ["state_changed"]


# ---------------------------------------------------------------------------
# GAP-02 (Fase 03 Plano 14) — o monitor correlaciona evidência complementar de
# ciclos diferentes para a MESMA alegação comercial (não mais apenas escopo
# exato), sem correlacionar escopos genuinamente divergentes entre si.
# ---------------------------------------------------------------------------

_PHASE03_FIXTURES = Path(__file__).parent / "fixtures" / "phase03"


def test_gap02_cross_cycle_correlates_checkout_only_evidence_with_reconciled_offer():
    """Cycle 1: reconciled html+checkout offer (full scope). Cycle 2: checkout-only
    re-derivation of the SAME claim (scope missing product_id). Must stay one entry,
    with no artificial ended+started."""
    from services.promotions import parsers
    from services.promotions.adapters import html as html_adapter
    from services.promotions.adapters import vtex as vtex_adapter

    html_offer = html_adapter.parse(
        (_PHASE03_FIXTURES / "html_pix_partial_scope.html").read_text(encoding="utf-8"),
        product_id="p-1",
    ).offers[0]
    checkout_payload = json.loads(
        (_PHASE03_FIXTURES / "vtex_checkout_pix_partial_scope.json").read_text(encoding="utf-8")
    )
    checkout_offer = vtex_adapter.parse_checkout(checkout_payload)[0]
    reconciled, _ = parsers.deduplicate_offers([html_offer, checkout_offer])
    assert len(reconciled) == 1

    config = PriceMonitorConfig(job_id="job-gap02", url="https://example.test/p", brand="brand")

    first_events = monitor.apply_conditional_offer_result(
        config,
        ConditionalOfferResult(
            status="found",
            offers=reconciled,
            probe_level="full",
            checked_at="2026-07-15T00:00:00Z",
        ),
    )
    assert [event.event_type.value for event in first_events] == ["started"]
    assert len(config.conditional_offers_current) == 1

    cycle2_checkout_offer = vtex_adapter.parse_checkout(checkout_payload)[0]
    second_events = monitor.apply_conditional_offer_result(
        config,
        ConditionalOfferResult(
            status="found",
            offers=[cycle2_checkout_offer],
            probe_level="full",
            checked_at="2026-07-15T01:00:00Z",
        ),
    )

    assert len(config.conditional_offers_current) == 1
    assert all(
        event.event_type.value not in {"ended", "started"} for event in second_events
    )


def test_gap02_cross_cycle_genuinely_different_sku_does_not_correlate():
    """A cross-cycle offer with a known-different, non-empty SKU must NOT correlate
    with the existing entry — it is a legitimate started+ended pair, not a false
    merge between different products."""
    config = PriceMonitorConfig(job_id="job-gap02b", url="https://example.test/p", brand="brand")
    offer_sku1 = ConditionalOffer(
        condition_type="payment_method",
        condition_label="R$ 180,00 no pix",
        eligibility="requires_payment_method",
        payment_method="pix",
        price="180.00",
        state="confirmed",
        scope=OfferScope(sku_id="sku-1", seller_id="1"),
        source=OfferSource(kind="vtex_checkout", ref="/api/checkout", authority="authoritative"),
    )
    first_events = monitor.apply_conditional_offer_result(
        config,
        ConditionalOfferResult(
            status="found",
            offers=[offer_sku1],
            probe_level="full",
            checked_at="2026-07-15T00:00:00Z",
        ),
    )
    assert [event.event_type.value for event in first_events] == ["started"]

    offer_sku2 = ConditionalOffer(
        condition_type="payment_method",
        condition_label="R$ 180,00 no pix",
        eligibility="requires_payment_method",
        payment_method="pix",
        price="180.00",
        state="confirmed",
        scope=OfferScope(sku_id="sku-2", seller_id="1"),
        source=OfferSource(kind="vtex_checkout", ref="/api/checkout", authority="authoritative"),
    )
    second_events = monitor.apply_conditional_offer_result(
        config,
        ConditionalOfferResult(
            status="found",
            offers=[offer_sku2],
            probe_level="full",
            checked_at="2026-07-15T01:00:00Z",
        ),
    )

    event_types = {event.event_type.value for event in second_events}
    assert "started" in event_types
    assert "ended" in event_types
    assert len(config.conditional_offers_current) == 1
    assert config.conditional_offers_current[0].offer_key != offer_sku1.offer_key
