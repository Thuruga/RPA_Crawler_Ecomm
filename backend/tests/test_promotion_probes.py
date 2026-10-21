"""Transport/probe contracts with fake sessions only (activated by Plan 03-03)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

try:
    from services.promotions import registry as probes
    from services.promotions import runtime
except ImportError:
    probes = runtime = None

from services.shopify_api_client import ShopifyApiClient

requires_impl = pytest.mark.skipif(probes is None, reason="temporary Wave 0 guard: remove in 03-03")


def test_wave0_probe_contract_is_collectable_before_implementation():
    assert probes is None or hasattr(probes, "resolve_for_engine")


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


# ---------------------------------------------------------------------------
# GAP-01 (Fase 03 Plano 13) helpers — o PDP real (`VtexApiClient.parse_product_dict()`)
# alimentando `execute_resolved_probes()`/`reserva_prime.probe()` sem teaser
# pré-semeado e sem mock do adapter inteiro.
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "phase03"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _reserva_vtex_product_dict() -> dict:
    """`reserva_catalog_product.json` (usada crua por `test_promotion_parsers.py`)
    enriquecida apenas com o scaffolding de schema que `VtexProduct` exige
    (name/nameComplete/images/sellerName/sellerDefault/RewardValue/brandId/
    linkText/categoryId/link) — o contexto comercial real (items/sellers/
    commertialOffer/collectionIds) vem 100% da fixture, sem reescrita."""
    data = _load_fixture("reserva_catalog_product.json")
    items = []
    for item in data.get("items", []):
        sellers = []
        for seller in item.get("sellers", []):
            sellers.append({
                "sellerName": "Loja Oficial",
                "sellerDefault": True,
                **seller,
                "commertialOffer": {"RewardValue": 0.0, **seller["commertialOffer"]},
            })
        items.append({
            "name": "Camisa ML Flanela Speed",
            "nameComplete": "Camisa ML Flanela Speed - Speed",
            "images": [{"imageUrl": "https://www.usereserva.com/img/camisa.jpg"}],
            **item,
            "sellers": sellers,
        })
    return {
        "brandId": 1,
        "linkText": "camisa-ml-flanela-speed",
        "categoryId": "1",
        "link": "https://www.usereserva.com/camisa-ml-flanela-speed",
        **data,
        "items": items,
    }


async def _real_reserva_product():
    """Retorna o `RawProductBronze` real de `parse_product_dict()` — sem qualquer
    teaser pré-semeado — usado como prova fim-a-fim de que o contexto comercial
    (sku_id/seller_id/price_without_discount/collection_ids) chega ao adapter."""
    from services.vtex_api_scraper import VtexApiClient

    client = VtexApiClient("reserva")
    data = _reserva_vtex_product_dict()
    with patch.object(VtexApiClient, "_get_color_family", return_value=[]), \
         patch("services.vtex_api_scraper.get_single_review", return_value=(None, None)):
        return await client.parse_product_dict(
            data, "https://www.usereserva.com/camisa-ml-flanela-speed", "www.usereserva.com"
        )


def _reserva_brand() -> SimpleNamespace:
    return SimpleNamespace(
        brand_key="reserva",
        domain="www.usereserva.com",
        engine="vtex",
        promotion_probes=[{
            "type": "brand_rule_endpoint",
            "adapter": "reserva_prime",
            "path": "/_v/rsv-prime/primepromotion",
        }],
    )


@requires_impl
@pytest.mark.asyncio
async def test_d10_fast_probe_never_opens_browser_or_mutates_cart():
    browser_calls = []
    cart_calls = []
    result = await probes.run_level("fast", trigger="search", browser=lambda *a: browser_calls.append(a), checkout=lambda *a: cart_calls.append(a))
    assert result.probe_level == "fast"
    assert browser_calls == [] and cart_calls == []


@requires_impl
@pytest.mark.asyncio
async def test_d11_confirm_requires_advertised_evidence_and_allowed_trigger():
    confirm = probes.get_confirm_probe()
    assert await confirm.should_run(advertised=[], trigger="product_opened") is False
    assert await confirm.should_run(advertised=[object()], trigger="search") is False
    assert await confirm.should_run(advertised=[object()], trigger="manual_revalidate") is True


@requires_impl
@pytest.mark.asyncio
async def test_d03_probes_use_fake_ephemeral_session_and_never_real_checkout():
    session = FakeSession({"status": "none_detected"})
    await probes.run_with_session(session, brand="example", level="full")
    assert session.calls
    assert all("placeOrder" not in url and "transaction" not in url for _, url, _ in session.calls)


@requires_impl
def test_d06_registry_returns_explicit_unsupported_for_missing_platform_adapter():
    result = probes.resolve_for_engine("unknown", brand_key="example")
    assert result.status == "unsupported"


@requires_impl
@pytest.mark.parametrize("engine", ["vtex", "shopify", "wake", "zara"])
def test_d08_every_active_own_brand_engine_has_probe_or_explicit_diagnostic(engine):
    resolved = probes.resolve_for_engine(engine, brand_key=f"fixture-{engine}")
    assert resolved.probes or resolved.status in {"unsupported", "blocked", "requires_auth"}


@requires_impl
def test_resolved_probe_preserves_default_and_configured_runtime_descriptors():
    resolved = probes.resolve_for_engine(
        "vtex",
        brand_key="reserva",
        promotion_probes=[{
            "type": "brand_rule_endpoint",
            "adapter": "reserva_prime",
            "path": "/_v/rsv-prime/primepromotion",
        }],
    )
    assert [probe.source_name for probe in resolved.probes] == [
        "vtex_structured",
        "reserva_prime:/_v/rsv-prime/primepromotion",
    ]
    assert tuple(probe.config for probe in resolved.probes) == resolved.configs


@requires_impl
@pytest.mark.asyncio
async def test_gap01_reserva_prime_finds_real_offer_from_pdp_without_teaser_or_mock():
    """GAP-01 principal: parte do retorno REAL de `parse_product_dict()` (sem
    teaser pré-semeado) e de uma sessão fake servindo o JSON real da regra —
    nunca um mock de `reserva_prime.probe` inteiro."""
    product = await _real_reserva_product()
    assert product is not None
    assert product.conditional_offers == []
    assert product.sku_id == "638383"
    assert product.seller_id == "1"
    assert product.price_without_discount == 599.0
    assert product.collection_ids == ["speed"]

    brand = _reserva_brand()
    resolved = probes.resolve_brand(brand)
    session = FakeSession(_load_fixture("reserva_prime_rule.json"))

    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand=brand,
        url="https://www.usereserva.com/camisa-ml-flanela-speed",
        level="full",
        product=product,
        session=session,
    )

    assert session.calls
    assert result.status == "found"
    assert "reserva_prime:/_v/rsv-prime/primepromotion" in result.sources_attempted
    prime_offers = [offer for offer in result.offers if offer.condition_label == "Cliente Prime"]
    assert len(prime_offers) == 1
    offer = prime_offers[0]
    assert offer.state == "confirmed"
    assert offer.price == Decimal("509.15")
    assert offer.scope.sku_id == "638383"
    assert offer.scope.seller_id == "1"


@requires_impl
@pytest.mark.asyncio
async def test_gap01_reserva_prime_finds_real_offer_from_quality_gated_dict():
    """Regressão: `routes_promotions.py` nunca passa o objeto `RawProductBronze`
    direto para `execute_resolved_probes()` — todo engine real (VTEX/Shopify/SFCC/
    Wake/Zara) devolve `engine.get_pdp_product()` -> `validate_single()` ->
    `RawProductBronze.model_dump()`, ou seja, um dict PLANO (sku_id/seller_id/
    price_without_discount como chaves de topo), nunca o dict aninhado
    items/sellers/commertialOffer do catálogo VTEX bruto.

    `_product_payload()` tratava QUALQUER dict como já pronto e o devolvia
    intacto, então esse dict plano nunca virava items=[...] e o probe via
    sempre `insufficient_context` mesmo com sku_id/seller_id/preço presentes —
    o caso real reportado (usereserva.com/polo-piquet-classica-0067587-312/p,
    R$299,00 -> R$254,15 Cliente Prime) só reproduz com o dict, nunca com o
    objeto usado no teste acima."""
    product = await _real_reserva_product()
    assert product is not None
    product_dict = product.model_dump()
    assert "items" not in product_dict

    brand = _reserva_brand()
    resolved = probes.resolve_brand(brand)
    session = FakeSession(_load_fixture("reserva_prime_rule.json"))

    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand=brand,
        url="https://www.usereserva.com/camisa-ml-flanela-speed",
        level="full",
        product=product_dict,
        session=session,
    )

    assert result.status == "found"
    assert not any("insufficient_context" in item for item in result.diagnostics)
    prime_offers = [offer for offer in result.offers if offer.condition_label == "Cliente Prime"]
    assert len(prime_offers) == 1
    assert prime_offers[0].price == Decimal("509.15")
    assert prime_offers[0].scope.sku_id == "638383"
    assert prime_offers[0].scope.seller_id == "1"


@requires_impl
@pytest.mark.asyncio
async def test_gap01_insufficient_context_is_typed_never_none_detected():
    """Produto sem item/seller resolvível (sku_id/seller_id/price_without_discount
    todos None) deve virar `unsupported` com diagnóstico `insufficient_context:` —
    nunca um falso `none_detected`."""
    from core.models import RawProductBronze

    product = RawProductBronze(
        url="https://www.usereserva.com/p",
        brand="Reserva",
        raw_title="Produto sem sku/seller resolvivel",
        price_full=100.0,
        image_url="https://www.usereserva.com/img.jpg",
    )
    assert product.sku_id is None and product.seller_id is None

    brand = _reserva_brand()
    resolved = probes.resolve_brand(brand)
    session = FakeSession(_load_fixture("reserva_prime_rule.json"))

    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand=brand,
        url="https://www.usereserva.com/p",
        level="full",
        product=product,
        session=session,
    )

    assert result.status == "unsupported"
    assert result.status != "none_detected"
    assert any(
        item.startswith("reserva_prime:") and "insufficient_context:" in item
        for item in result.diagnostics
    )


@requires_impl
@pytest.mark.asyncio
async def test_gap01_ineligible_rule_with_complete_context_stays_none_detected():
    """Regressão: contexto comercial completo mas regra tecnicamente inelegível
    (excludedSkus cobre o SKU) continua `none_detected` sem o prefixo
    `insufficient_context` — a distinção é sobre AUSÊNCIA de contexto, não
    sobre inelegibilidade."""
    product = await _real_reserva_product()
    assert product.sku_id == "638383" and product.seller_id == "1"

    ineligible_rule = {**_load_fixture("reserva_prime_rule.json"), "excludedSkus": ["638383"]}
    brand = _reserva_brand()
    resolved = probes.resolve_brand(brand)
    session = FakeSession(ineligible_rule)

    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand=brand,
        url="https://www.usereserva.com/camisa-ml-flanela-speed",
        level="full",
        product=product,
        session=session,
    )

    assert result.status == "none_detected"
    assert not any("insufficient_context" in item for item in result.diagnostics)


@requires_impl
@pytest.mark.asyncio
async def test_fast_runtime_skips_full_html_and_brand_rule_without_attempting(monkeypatch):
    calls = []

    async def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("full-only adapter executed on fast path")

    monkeypatch.setattr(runtime.reserva_prime, "probe", forbidden)
    resolved = probes.resolve_for_engine(
        "vtex",
        brand_key="reserva",
        promotion_probes=[{
            "type": "brand_rule_endpoint",
            "adapter": "reserva_prime",
            "path": "/_v/rsv-prime/primepromotion",
        }],
    )
    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand={"domain": "www.usereserva.com"},
        url="https://www.usereserva.com/produto/p",
        level="fast",
        product={"productId": "104962"},
    )
    assert calls == []
    assert result.sources_attempted == ["vtex_structured"]
    assert any("full-only source" in item for item in result.diagnostics)


@requires_impl
@pytest.mark.asyncio
async def test_skip_only_runtime_is_unsupported_not_none_detected():
    resolved = probes.resolve_for_engine("sfcc", brand_key="fixture-sfcc")
    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand={"domain": "example.com"},
        url="https://example.com/product",
        level="fast",
        product=None,
    )
    assert result.status == "unsupported"
    assert result.sources_attempted == []
    assert "no applicable promotion source executed" in result.diagnostics


@requires_impl
def test_registry_rejects_adapter_outside_closed_allowlist():
    with pytest.raises(ValueError):
        probes.resolve_for_engine(
            "vtex",
            brand_key="unsafe",
            promotion_probes=[{
                "type": "brand_rule_endpoint",
                "adapter": "arbitrary.module:callable",
                "path": "/offer",
            }],
        )


# ---------------------------------------------------------------------------
# Shopify structured probe — scrape-time conditional_offers (mirrors VTEX's
# GAP-01 pattern). Discovered while investigating why the Reserva fix didn't
# generalize to Shopify brands (ricardoalmeida, bck): `runtime._product_payload()`
# only ever rebuilds a VTEX-shaped dict (items/sellers/commertialOffer), never
# Shopify's native shape (variants/tags), so `shopify.parse_product(payload)`
# can never find anything if called against that rebuilt payload. VTEX sidesteps
# this because `vtex_api_scraper.parse_product_dict()` already calls
# `vtex.parse_teasers()` on the real item/seller at scrape time and stores the
# result in `RawProductBronze.conditional_offers` — `_structured_result()` checks
# that FIRST via `_existing_offers()`, before ever touching the rebuilt payload.
# Shopify's `_map_to_bronze()` didn't do the equivalent for tags/variants, so
# `conditional_offers` was always `[]` for every Shopify product in production.
# ---------------------------------------------------------------------------
def _shopify_pix_product_dict() -> dict:
    return {
        "id": 999123,
        "handle": "camiseta-teste-pix",
        "title": "Camiseta Teste Pix",
        "body_html": "<p>desc</p>",
        "vendor": "Ricardo Almeida",
        "product_type": "CAMISETA",
        "tags": ["masculino", "10% OFF no Pix"],
        "options": [{"name": "Tamanho", "position": 1}],
        "variants": [
            {"id": 555777, "price": "299.00", "compare_at_price": None, "available": True, "option1": "M"},
        ],
        "images": [{"src": "https://example.com/img.jpg"}],
    }


def test_shopify_map_to_bronze_captures_promo_tags_at_scrape_time():
    """`_map_to_bronze()` must populate `conditional_offers` from the real
    tags/variants payload — same chokepoint pattern as VTEX's `parse_product_dict()`."""
    client = ShopifyApiClient("ricardoalmeida")
    product = client._map_to_bronze(_shopify_pix_product_dict(), category="Camisetas")

    assert product is not None
    assert len(product.conditional_offers) == 1
    offer = product.conditional_offers[0]
    assert offer.condition_label == "10% OFF no Pix"
    assert offer.payment_method == "pix"
    assert offer.scope.product_id == "999123"
    assert offer.scope.sku_id == "555777"


@requires_impl
@pytest.mark.asyncio
async def test_shopify_structured_probe_finds_offer_from_quality_gated_dict():
    """End-to-end: the same flat `RawProductBronze.model_dump()` dict shape
    `engine.get_pdp_product()` produces in production (not a hand-built object)
    must still surface the Shopify tag-based offer through `execute_resolved_probes()`."""
    client = ShopifyApiClient("ricardoalmeida")
    product = client._map_to_bronze(_shopify_pix_product_dict(), category="Camisetas")
    assert product is not None
    product_dict = product.model_dump()

    resolved = probes.resolve_for_engine("shopify", brand_key="ricardoalmeida")
    result = await runtime.execute_resolved_probes(
        resolved.probes,
        resolved.configs,
        brand={"domain": "www.ricardoalmeida.com.br", "engine": "shopify"},
        url="https://www.ricardoalmeida.com.br/products/camiseta-teste-pix",
        level="full",
        product=product_dict,
    )

    assert result.status == "found"
    assert len(result.offers) == 1
    assert result.offers[0].condition_label == "10% OFF no Pix"
    assert result.offers[0].payment_method == "pix"
