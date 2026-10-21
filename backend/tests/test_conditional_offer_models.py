"""Wave 0 contract for Phase 03 domain models (activated by Plan 03-02)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

try:
    from services.promotions import base
except ImportError:
    base = None

requires_impl = pytest.mark.skipif(base is None, reason="temporary Wave 0 guard: remove in 03-02")


def test_wave0_model_contract_is_collectable_before_implementation():
    assert base is None or hasattr(base, "ConditionalOffer")


def _offer(**changes):
    payload = {
        "condition_type": "membership",
        "condition_label": "Cliente Prime",
        "eligibility": "requires_membership",
        "price": Decimal("509.15"),
        "discount_value": Decimal("15"),
        "discount_unit": "percent",
        "currency": "BRL",
        "scope": {"product_id": "104962", "sku_id": "638383", "seller_id": "1"},
        "state": "confirmed",
        "source": {"kind": "brand_custom_api", "ref": "/_v/rsv-prime/primepromotion", "authority": "authoritative"},
    }
    payload.update(changes)
    return base.ConditionalOffer(**payload)


@requires_impl
def test_d01_all_condition_types_are_validated_and_unknown_is_rejected():
    for condition in ("membership", "payment_method", "quantity", "coupon", "checkout", "authentication", "other"):
        assert _offer(condition_type=condition).condition_type == condition
    with pytest.raises(ValidationError):
        _offer(condition_type="magic")


@requires_impl
def test_d02_public_product_defaults_are_additive_and_legacy_payload_loads():
    from core.models import RawProductBronze, SearchProductResult

    raw = RawProductBronze(url="https://example.test/p", brand="x", raw_title="Produto")
    search = SearchProductResult(brand="x", product_name="Produto", url="https://example.test/p")
    assert raw.conditional_offers == []
    assert search.conditional_offers == []


@requires_impl
def test_d04_offer_key_is_stable_when_price_or_state_changes_but_tracks_scope():
    original = _offer()
    changed = _offer(
        price=Decimal("499.00"),
        state="advertised",
        source={"kind": "html", "ref": "/produto", "authority": "advertised"},
    )
    other_sku = _offer(scope={"product_id": "104962", "sku_id": "other", "seller_id": "1"})
    other_seller = _offer(scope={"product_id": "104962", "sku_id": "638383", "seller_id": "2"})
    other_condition = _offer(condition_label="Clube diferente")
    assert original.offer_key == changed.offer_key
    assert original.offer_key != other_sku.offer_key
    assert original.offer_key != other_seller.offer_key
    assert original.offer_key != other_condition.offer_key


@requires_impl
def test_explicit_legacy_offer_key_is_preserved_on_load():
    assert _offer(offer_key="legacy:source-dependent:key").offer_key == "legacy:source-dependent:key"


@requires_impl
def test_d05_quantity_price_requires_unequivocal_operands_and_rounds_half_up():
    normalized = base.normalize_quantity_offer(quantity=3, total=Decimal("300.00"))
    assert normalized == Decimal("100.00")
    assert base.normalize_quantity_offer(quantity=None, total=Decimal("300.00")) is None
    assert base.normalize_quantity_offer(quantity=3, total=None) is None


@requires_impl
def test_d15_d16_conditional_price_never_changes_public_effective_price_or_sort_key():
    from core.models import SearchProductResult
    from core.pricing import resolve_effective_price

    expensive = SearchProductResult(brand="x", product_name="A", url="https://example.test/a", price_full=599.0, conditional_offers=[_offer(price=Decimal("1.00"))])
    cheap = SearchProductResult(brand="x", product_name="B", url="https://example.test/b", price_full=550.0)
    assert resolve_effective_price(expensive.price_full, expensive.price_discount, expensive.price_discount_is_delta) == 599.0
    assert [p.product_name for p in sorted([expensive, cheap], key=lambda p: p.price_full)] == ["B", "A"]


# ---------------------------------------------------------------------------
# GAP-01 (Fase 03 Plano 13) — sku_id/seller_id/price_without_discount/collection_ids
# preservados no RawProductBronze real, independentes de teaser pré-existente.
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "phase03"


def _reserva_product_dict() -> dict:
    """Fixture real (`reserva_catalog_product.json`) enriquecida apenas com os campos
    obrigatórios de `VtexProduct`/`VtexItem`/`VtexSeller`/`VtexCommertialOffer` que a
    fixture (usada crua por `test_promotion_parsers.py`) não carrega — são scaffolding
    de schema (nome do SKU, nome do vendedor, RewardValue), nunca o contexto comercial
    sob teste (sku_id/seller_id/PriceWithoutDiscount/collectionIds), que vem 100% da
    fixture real e não é reescrito aqui.
    """
    data = json.loads((_FIXTURE_DIR / "reserva_catalog_product.json").read_text(encoding="utf-8"))
    items = []
    for item in data["items"]:
        sellers = []
        for seller in item["sellers"]:
            sellers.append({
                "sellerName": "Loja Oficial",
                "sellerDefault": True,
                **seller,
                "commertialOffer": {"RewardValue": 0.0, **seller["commertialOffer"]},
            })
        items.append({
            "name": "Camisa ML Flanela Speed",
            "nameComplete": "Camisa ML Flanela Speed - Speed",
            "images": [{"imageUrl": "https://www.reserva.ink/img/camisa-ml-flanela-speed.jpg"}],
            **item,
            "sellers": sellers,
        })
    return {
        "brandId": 1,
        "linkText": "camisa-ml-flanela-speed",
        "categoryId": "1",
        "link": "https://www.reserva.ink/camisa-ml-flanela-speed",
        **data,
        "items": items,
    }


def test_gap01_legacy_payload_without_new_fields_defaults_are_additive():
    from core.models import RawProductBronze

    raw = RawProductBronze(
        url="u", brand="b", raw_title="t", price_full=10.0, image_url="i",
    )
    assert raw.sku_id is None
    assert raw.seller_id is None
    assert raw.price_without_discount is None
    assert raw.collection_ids == []


@pytest.mark.asyncio
async def test_gap01_parse_product_dict_preserves_real_commercial_context_without_teaser():
    from services.vtex_api_scraper import VtexApiClient

    data = _reserva_product_dict()
    client = VtexApiClient("reserva")

    with patch.object(VtexApiClient, "_get_color_family", return_value=[]), \
         patch("services.vtex_api_scraper.get_single_review", return_value=(None, None)):
        product = await client.parse_product_dict(data, "https://www.example.test/p", "www.example.test")

    assert product is not None
    assert product.sku_id == "638383"
    assert product.seller_id == "1"
    assert product.price_without_discount == 599.0
    assert product.collection_ids == ["speed"]
    # Sem teaser na fixture — a preservação do contexto não depende de oferta detectada.
    assert product.conditional_offers == []


# ---------------------------------------------------------------------------
# GAP-02 (Fase 03 Plano 14) — reconciliação hierárquica de escopo parcial:
# ausência de campo é desconhecido, nunca conflito.
# ---------------------------------------------------------------------------


def test_gap02_scopes_compatible_treats_missing_scalar_as_unknown_not_conflict():
    from core.models import OfferScope, scopes_compatible

    assert scopes_compatible(
        OfferScope(product_id="p-1"), OfferScope(sku_id="sku-1", seller_id="1")
    ) is True


def test_gap02_scopes_compatible_rejects_known_divergent_scalar():
    from core.models import OfferScope, scopes_compatible

    assert scopes_compatible(OfferScope(sku_id="sku-1"), OfferScope(sku_id="sku-2")) is False


def test_gap02_scopes_compatible_list_fields_use_intersection_not_equality():
    from core.models import OfferScope, scopes_compatible

    assert scopes_compatible(
        OfferScope(category_ids=["camisas"]), OfferScope(category_ids=["calcas"])
    ) is False
    assert scopes_compatible(
        OfferScope(category_ids=["camisas", "polos"]), OfferScope(category_ids=["polos"])
    ) is True


def test_gap02_scopes_compatible_works_for_plain_dict_scope():
    from core.models import OfferScope, scopes_compatible

    assert scopes_compatible(
        OfferScope(product_id="p-1"), {"sku_id": "sku-1", "seller_id": "1"}
    ) is True
    assert scopes_compatible({"sku_id": "sku-1"}, {"sku_id": "sku-2"}) is False


def test_gap02_merge_scopes_reconciles_complementary_evidence():
    from core.models import OfferScope, merge_scopes

    merged = merge_scopes(
        OfferScope(product_id="p-1"), OfferScope(sku_id="sku-1", seller_id="1")
    )
    assert merged == OfferScope(product_id="p-1", sku_id="sku-1", seller_id="1")


def test_gap02_condition_signature_key_ignores_scope_price_state_and_source():
    from core.models import condition_signature_key

    base = _offer()
    other_scope = _offer(
        scope={"product_id": "other", "sku_id": "other-sku", "seller_id": "9"}
    )
    other_evidence = _offer(
        price=Decimal("1.00"),
        state="advertised",
        source={"kind": "html", "ref": "/x", "authority": "advertised"},
    )
    other_condition = _offer(condition_label="Clube diferente")
    other_eligibility = _offer(eligibility="other")
    assert condition_signature_key(base) == condition_signature_key(other_scope)
    assert condition_signature_key(base) == condition_signature_key(other_evidence)
    assert condition_signature_key(base) != condition_signature_key(other_condition)
    assert condition_signature_key(base) != condition_signature_key(other_eligibility)


def test_gap02_commercial_claim_key_contract_unchanged():
    """Regression: existing commercial_claim_key behavior (test_d04) is untouched."""
    from core.models import commercial_claim_key

    original = _offer()
    changed = _offer(
        price=Decimal("499.00"),
        state="advertised",
        source={"kind": "html", "ref": "/produto", "authority": "advertised"},
    )
    other_sku = _offer(scope={"product_id": "104962", "sku_id": "other", "seller_id": "1"})
    assert commercial_claim_key(original) == commercial_claim_key(changed)
    assert commercial_claim_key(original) != commercial_claim_key(other_sku)


def test_gap02_offers_share_commercial_claim_true_for_complementary_scope():
    from core.models import offers_share_commercial_claim

    complementary = _offer(scope={"sku_id": "638383", "seller_id": "1"})
    assert offers_share_commercial_claim(_offer(), complementary) is True


def test_gap02_offers_share_commercial_claim_false_for_known_divergent_scope():
    from core.models import offers_share_commercial_claim

    divergent = _offer(scope={"product_id": "104962", "sku_id": "638383", "seller_id": "2"})
    assert offers_share_commercial_claim(_offer(), divergent) is False


def test_gap02_offers_share_commercial_claim_true_for_snapshot_dict_scope():
    """Proves the getattr-on-dict bug is fixed for ConditionalOfferSnapshot.scope."""
    from core.models import ConditionalOfferSnapshot, offers_share_commercial_claim

    canonical = _offer()
    snapshot = ConditionalOfferSnapshot.model_validate(canonical.model_dump(mode="json"))
    assert isinstance(snapshot.scope, dict)
    assert offers_share_commercial_claim(canonical, snapshot) is True

    divergent = _offer(scope={"product_id": "104962", "sku_id": "638383", "seller_id": "2"})
    divergent_snapshot = ConditionalOfferSnapshot.model_validate(divergent.model_dump(mode="json"))
    assert offers_share_commercial_claim(canonical, divergent_snapshot) is False
