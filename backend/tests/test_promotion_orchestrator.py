"""Layering, precedence, cache and single-flight contracts (Plan 03-03)."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

try:
    from services.promotions import orchestrator as module
except ImportError:
    module = None

requires_impl = pytest.mark.skipif(module is None, reason="temporary Wave 0 guard: remove in 03-03")


def test_wave0_orchestrator_contract_is_collectable_before_implementation():
    assert module is None or hasattr(module, "PromotionOrchestrator")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@requires_impl
@pytest.mark.asyncio
async def test_d10_fast_full_confirm_levels_have_strict_cost_boundaries():
    calls = []
    orchestrator = module.PromotionOrchestrator(probe=lambda level: calls.append(level))
    await orchestrator.collect(brand="x", url="https://x.example/p", trigger="search")
    assert calls == ["fast"]


@requires_impl
@pytest.mark.asyncio
async def test_d11_confirm_only_runs_after_advertised_and_for_allowed_trigger():
    calls = []
    orchestrator = module.PromotionOrchestrator(confirm=lambda offer: calls.append(offer))
    await orchestrator.collect(brand="x", url="https://x.example/p", trigger="search")
    assert calls == []
    await orchestrator.collect(brand="x", url="https://x.example/p", trigger="manual_revalidate", advertised=[object()])
    assert len(calls) == 1


@requires_impl
@pytest.mark.asyncio
async def test_d12_single_flight_ttl_and_force_revalidation():
    clock = FakeClock()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"status": "none_detected"}

    orchestrator = module.PromotionOrchestrator(fetch=fetch, clock=clock, ttl=60)
    await asyncio.gather(*(orchestrator.collect(brand="x", url="https://x.example/p") for _ in range(3)))
    assert calls == 1
    await orchestrator.collect(brand="x", url="https://x.example/p")
    assert calls == 1
    await orchestrator.collect(brand="x", url="https://x.example/p", force=True)
    assert calls == 2


@requires_impl
@pytest.mark.parametrize("statuses, expected", [(["none_detected", "blocked"], "blocked"), (["none_detected", "error"], "error"), (["unsupported"], "unsupported")])
def test_d04_blocked_or_error_never_collapses_to_none_detected(statuses, expected):
    assert module.aggregate_status(statuses) == expected


@requires_impl
def test_d09_authoritative_source_wins_without_losing_advertised_diagnostic_or_scope():
    advertised = module.offer_fixture(state="advertised", authority="observed", price="510.00", sku_id="sku-1")
    confirmed = module.offer_fixture(state="confirmed", authority="authoritative", price="509.15", sku_id="sku-1")
    merged, diagnostics = module.merge_offers([advertised, confirmed])
    assert merged[0].price == confirmed.price and merged[0].scope.sku_id == "sku-1"
    assert diagnostics


@requires_impl
@pytest.mark.asyncio
async def test_d13_offer_enrichment_is_independent_from_product_detail_failure():
    orchestrator = module.PromotionOrchestrator(fetch=lambda: {"status": "none_detected"})
    result = await orchestrator.collect(brand="x", url="https://x.example/p", trigger="product_opened", product_detail=None)
    assert result.status == "none_detected"


@requires_impl
@pytest.mark.asyncio
async def test_d14_manual_force_bypasses_product_cache_but_keeps_trigger_gate():
    orchestrator = module.PromotionOrchestrator()
    with pytest.raises(ValueError):
        await orchestrator.collect(brand="x", url="https://x.example/p", trigger="search", force=True)


@requires_impl
@pytest.mark.asyncio
async def test_d14_unknown_trigger_cannot_reach_confirmation():
    calls = []
    orchestrator = module.PromotionOrchestrator(confirm=lambda offer: calls.append(offer))
    with pytest.raises(ValueError):
        await orchestrator.collect(
            brand="x", url="https://x.example/p", trigger="browser_value", advertised=[object()]
        )
    assert calls == []


@requires_impl
@pytest.mark.asyncio
async def test_d04_only_matching_authoritative_allocation_is_confirmed():
    import json
    from pathlib import Path

    from services.promotions.confirmation import AnonymousConfirmationTransport

    advertised = module.offer_fixture(
        state="advertised", authority="structured", price="200.00", sku_id="vtex-sku-1"
    ).model_copy(update={"condition_type": "payment_method", "condition_label": "10% com Pix", "payment_method": "pix"})
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "phase03" / "vtex_checkout_confirmed.json").read_text(encoding="utf-8")
    )

    class Session:
        async def request(self, *_args, **_kwargs):
            return {"status": 200, "payload": fixture}

        async def close(self):
            pass

    result = await AnonymousConfirmationTransport(session_factory=Session).confirm_vtex(
        brand_domain="example.com", advertised=advertised, sku_id="vtex-sku-1", seller_id="1"
    )
    assert result.status == "found"
    assert result.offers[0].state == "confirmed"
    assert result.offers[0].offer_key == advertised.offer_key


@requires_impl
@pytest.mark.asyncio
async def test_d14_failed_confirmation_preserves_advertised_offer():
    advertised = module.offer_fixture(
        state="advertised", authority="structured", price="510.00", sku_id="sku-1"
    )
    orchestrator = module.PromotionOrchestrator(
        confirm=lambda _offer: {"status": "none_detected", "probe_level": "confirm"}
    )
    result = await orchestrator.collect(
        brand="x",
        url="https://x.example/p",
        trigger="product_opened",
        advertised=[advertised],
    )
    assert result.status == "found"
    assert result.offers[0].state == "advertised"


@requires_impl
@pytest.mark.asyncio
async def test_sources_attempted_are_aggregated_without_duplicates():
    orchestrator = module.PromotionOrchestrator(
        probe=lambda: {
            "status": "none_detected",
            "sources_attempted": ["vtex_structured", "vtex_structured"],
        }
    )
    result = await orchestrator.collect(
        brand="x", url="https://x.example/p", trigger="product_opened", force=True
    )
    assert result.sources_attempted == ["vtex_structured"]


@requires_impl
@pytest.mark.asyncio
async def test_html_advertisement_and_checkout_confirmation_merge_into_one_claim():
    from core.models import ConditionalOffer, OfferScope, OfferSource

    common = {
        "condition_type": "payment_method",
        "condition_label": "10% no Pix",
        "eligibility": "requires_payment_method",
        "payment_method": "pix",
        "scope": OfferScope(product_id="p-1", sku_id="sku-1", seller_id="1"),
    }
    advertised = ConditionalOffer(
        **common,
        price=Decimal("539.10"),
        state="advertised",
        source=OfferSource(kind="html", ref="/produto", authority="advertised"),
    )
    confirmed = ConditionalOffer(
        **common,
        price=Decimal("539.10"),
        state="confirmed",
        source=OfferSource(
            kind="vtex_checkout", ref="/api/checkout", authority="authoritative"
        ),
    )
    orchestrator = module.PromotionOrchestrator(
        probe=lambda _level: {
            "status": "found",
            "offers": [advertised],
            "sources_attempted": ["public_html"],
        },
        confirm=lambda _offer: {
            "status": "found",
            "offers": [confirmed],
            "sources_attempted": ["vtex_checkout"],
        },
    )

    result = await orchestrator.collect(
        brand="brand",
        url="https://brand.example/produto",
        trigger="product_opened",
        force=True,
    )

    assert len(result.offers) == 1
    assert result.offers[0].state == "confirmed"
    assert result.offers[0].source == confirmed.source
    assert result.offers[0].provenance == [advertised.source]
    assert result.sources_attempted == ["public_html", "vtex_checkout"]


@requires_impl
def test_canonical_merge_keeps_genuinely_distinct_claims_separate():
    first = module.offer_fixture(
        state="advertised", authority="observed", price="510.00", sku_id="sku-1"
    ).model_copy(update={"offer_key": None})
    second = first.model_copy(
        update={"condition_label": "Cliente Prime Plus", "offer_key": None}
    )

    merged, _ = module.merge_offers([first, second])

    assert len(merged) == 2
