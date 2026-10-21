"""Fixture-driven pure parser contracts (activated by Plan 03-02)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

try:
    from services.promotions import parsers as _parsers
    from services.promotions.adapters import html, reserva_prime, shopify, vtex
except ImportError:
    _parsers = html = reserva_prime = shopify = vtex = None

requires_impl = pytest.mark.skipif(_parsers is None, reason="temporary Wave 0 guard: remove in 03-02")

FIXTURES = Path(__file__).parent / "fixtures" / "phase03"


def test_wave0_parser_contract_is_collectable_before_implementation():
    assert _parsers is None or html is not None


def _load(name):
    path = FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else path.read_text(encoding="utf-8")


@requires_impl
def test_d02_reserva_keeps_public_599_and_prime_509_15_separate():
    product = _load("reserva_catalog_product.json")
    offers = reserva_prime.parse_rule(_load("reserva_prime_rule.json"), product)
    public = product["items"][0]["sellers"][0]["commertialOffer"]["PriceWithoutDiscount"]
    assert Decimal(str(public)) == Decimal("599.0")
    assert offers[0].price == Decimal("509.15")
    assert offers[0].condition_type == "membership"


@requires_impl
def test_d01_reserva_inclusion_and_exclusion_rules_cover_category_collection_brand_seller_sku():
    rule = _load("reserva_prime_rule.json")
    product = _load("reserva_catalog_product.json")
    assert reserva_prime.is_eligible(rule, product, sku_id="638383", seller_id="1") is True
    for field, value in (("excludedCategories", "camisas"), ("excludedCollections", "speed"), ("excludedSkus", "638383")):
        changed = {**rule, field: [value]}
        assert reserva_prime.is_eligible(changed, product, sku_id="638383", seller_id="1") is False


@requires_impl
def test_d04_vtex_and_shopify_advertised_then_authoritatively_confirmed():
    assert vtex.parse_teasers(_load("vtex_teasers.json"))[0].state == "advertised"
    assert vtex.parse_checkout(_load("vtex_checkout_confirmed.json"))[0].state == "confirmed"
    assert shopify.parse_product(_load("shopify_product.json"))[0].state == "advertised"
    assert shopify.parse_cart(_load("shopify_cart_discount.json"))[0].state == "confirmed"


@requires_impl
def test_vtex_checkout_ignores_identity_bound_prime_benefits():
    payload = {
        "items": [
            {
                "id": "638383",
                "quantity": 1,
                "seller": "1",
                "price": 29900,
                "sellingPrice": 25415,
            }
        ],
        "ratesAndBenefitsData": {
            "rateAndBenefitsIdentifiers": [
                {"name": "Cliente Prime", "description": "Beneficio aplicado"}
            ]
        },
    }

    assert vtex.parse_checkout(payload) == []


@requires_impl
def test_d05_html_quantity_price_only_when_total_and_quantity_are_explicit():
    offer = html.parse(_load("html_offer.html")).offers[0]
    assert offer.quantity_required == 3 and offer.price == Decimal("100.00")
    assert html.parse("<p>Compre 2 e ganhe desconto</p>").offers == []


@requires_impl
def test_d06_html_failure_states_are_honest():
    assert html.parse(_load("html_no_offer.html")).status == "none_detected"
    assert html.parse(_load("auth_required.html")).status == "requires_auth"
    assert html.parse_http(403, _load("blocked_403.json")).status == "blocked"
    assert html.parse_json(_load("unexpected_schema.json")).status == "error"


@requires_impl
def test_d04_dedup_uses_the_model_claim_key_and_keeps_non_winning_provenance():
    from core.models import ConditionalOffer, commercial_claim_key

    common = {
        "condition_type": "payment_method",
        "condition_label": "10% no Pix",
        "eligibility": "requires_payment_method",
        "payment_method": "pix",
        "scope": {"product_id": "p-1", "sku_id": "sku-1", "seller_id": "1"},
    }
    advertised = ConditionalOffer(
        **common,
        price="539.10",
        state="advertised",
        source={"kind": "html", "ref": "/produto", "authority": "advertised"},
    )
    confirmed = ConditionalOffer(
        **common,
        price="539.10",
        state="confirmed",
        source={"kind": "vtex_checkout", "ref": "/api/checkout", "authority": "authoritative"},
    )

    merged, diagnostics = _parsers.deduplicate_offers([advertised, confirmed])

    assert advertised.offer_key == confirmed.offer_key == commercial_claim_key(advertised)
    assert len(merged) == 1 and merged[0].source == confirmed.source
    assert merged[0].provenance == [advertised.source]
    assert diagnostics


# ---------------------------------------------------------------------------
# GAP-02 (Fase 03 Plano 14) — deduplicate_offers reconcilia escopo parcial
# REAL entre html.parse() (só product_id) e vtex.parse_checkout() (só
# sku_id/seller_id) para a mesma condição comercial.
# ---------------------------------------------------------------------------


@requires_impl
def test_gap02_real_html_and_checkout_complementary_scope_merge_into_one_offer():
    html_offer = html.parse(_load("html_pix_partial_scope.html"), product_id="p-1").offers[0]
    checkout_offer = vtex.parse_checkout(_load("vtex_checkout_pix_partial_scope.json"))[0]

    merged, diagnostics = _parsers.deduplicate_offers([html_offer, checkout_offer])

    assert len(merged) == 1
    winner = merged[0]
    assert winner.scope.product_id == "p-1"
    assert winner.scope.sku_id == "sku-1"
    assert winner.scope.seller_id == "1"
    assert winner.state == "confirmed"
    assert winner.source == checkout_offer.source
    assert winner.provenance == [html_offer.source]
    # Advertised (R$180,00) -> confirmed (R$180,00) is a state transition, so the
    # existing conflict diagnostic still fires (same behavior as the fully-
    # identical-scope case in test_d04); the reconciliation itself is silent.
    assert diagnostics


@requires_impl
def test_gap02_known_divergent_sku_across_evidence_stays_separate():
    from core.models import ConditionalOffer, OfferScope, OfferSource

    common = {
        "condition_type": "payment_method",
        "condition_label": "R$ 180,00 no pix",
        "eligibility": "requires_payment_method",
        "payment_method": "pix",
        "price": Decimal("180.00"),
    }
    offer_a = ConditionalOffer(
        **common,
        scope=OfferScope(sku_id="sku-1"),
        state="confirmed",
        source=OfferSource(kind="vtex_checkout", ref="/api/checkout", authority="authoritative"),
    )
    offer_b = ConditionalOffer(
        **common,
        scope=OfferScope(sku_id="sku-2"),
        state="confirmed",
        source=OfferSource(kind="vtex_checkout", ref="/api/checkout", authority="authoritative"),
    )

    merged, _ = _parsers.deduplicate_offers([offer_a, offer_b])

    assert len(merged) == 2
    assert {offer.scope.sku_id for offer in merged} == {"sku-1", "sku-2"}


@requires_impl
def test_gap02_disjoint_category_ids_stay_separate_overlap_merges():
    from core.models import ConditionalOffer, OfferScope, OfferSource

    common = {
        "condition_type": "payment_method",
        "condition_label": "R$ 180,00 no pix",
        "eligibility": "requires_payment_method",
        "payment_method": "pix",
        "price": Decimal("180.00"),
        "state": "advertised",
        "source": OfferSource(kind="html", ref="/produto", authority="advertised"),
    }
    disjoint_a = ConditionalOffer(**common, scope=OfferScope(category_ids=["camisas"]))
    disjoint_b = ConditionalOffer(**common, scope=OfferScope(category_ids=["calcas"]))
    merged_disjoint, _ = _parsers.deduplicate_offers([disjoint_a, disjoint_b])
    assert len(merged_disjoint) == 2

    overlap_a = ConditionalOffer(**common, scope=OfferScope(category_ids=["camisas", "polos"]))
    overlap_b = ConditionalOffer(**common, scope=OfferScope(category_ids=["polos"]))
    merged_overlap, _ = _parsers.deduplicate_offers([overlap_a, overlap_b])
    assert len(merged_overlap) == 1
