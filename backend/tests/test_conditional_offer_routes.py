"""Progressive authenticated route contracts (activated by Plan 03-04)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api import routes_promotions as routes
from core.models import (
    ConditionalOffer,
    ConditionalOfferResult,
    OfferScope,
    OfferSource,
    PriceMonitorConfig,
)


def test_wave0_route_contract_is_collectable_before_implementation():
    assert hasattr(routes, "conditional_offers")


def _brand(**changes):
    values = {"brand_key": "reserva", "domain": "www.usereserva.com", "engine": "vtex", "is_active": True}
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_d10_route_requires_active_own_brand_and_matching_url():
    request = routes.ConditionalOfferRequest(brand="reserva", url="https://evil.example/p", trigger="product_opened")
    with patch.object(routes.brand_service, "get_brand", return_value=_brand()):
        with pytest.raises(HTTPException) as exc:
            await routes.conditional_offers(request)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_d07_marketplaces_return_typed_unsupported_offer_result():
    request = routes.ConditionalOfferRequest(brand="amazon", url="https://amazon.com.br/p", trigger="product_opened")
    result = await routes.conditional_offers(request)

    assert result.status.value == "unsupported"
    assert result.offers == []
    assert result.diagnostics == ["marketplace excluded from conditional-promotion coverage"]


@pytest.mark.asyncio
async def test_marketplace_backed_brand_returns_typed_unsupported_offer_result():
    brand = _brand(brand_key="marketplace-store", domain="shop.example", engine="amazon")
    request = routes.ConditionalOfferRequest(
        brand="marketplace-store",
        url="https://shop.example/p",
        trigger="product_opened",
    )

    with (
        patch.object(routes.brand_service, "get_brand", return_value=brand),
        patch.object(routes, "validate_promotion_target", return_value=None),
    ):
        result = await routes.conditional_offers(request)

    assert result.status.value == "unsupported"
    assert result.offers == []
    assert result.diagnostics == ["marketplace excluded from conditional-promotion coverage"]


@pytest.mark.asyncio
async def test_privalia_is_not_rejected_as_marketplace_and_resolves_offers(monkeypatch):
    routes._clear_orchestrators()
    brand = SimpleNamespace(
        brand_key="privalia",
        domain="privalia.com",
        engine="privalia",
        is_active=True,
    )
    offer = ConditionalOffer(
        condition_type="payment_method",
        condition_label="Desconto Pix (20%)",
        eligibility="public",
        price="359.92",
        original_price="449.90",
        discount_percentage="20",
        payment_method="PIX",
        state="confirmed",
        source=OfferSource(
            kind="dom",
            ref="pdp_pix_price",
            authority="authoritative",
        ),
    )
    engine = SimpleNamespace(
        get_pdp_product=AsyncMock(return_value={"conditional_offers": [offer]})
    )
    monkeypatch.setattr(routes.brand_service, "get_brand", lambda _k: brand)
    monkeypatch.setattr(type(routes.engine_factory), "get_engine", staticmethod(lambda _k: engine))

    request = routes.ConditionalOfferRequest(
        brand="privalia",
        url="https://br.privalia.com/lojas/pdp/productId/559005",
        trigger="product_opened",
    )
    result = await routes.conditional_offers(request)
    assert result.status == "found"
    assert len(result.offers) == 1
    assert result.offers[0].condition_label == "Desconto Pix (20%)"
    assert str(result.offers[0].price) == "359.92"



@pytest.mark.asyncio
async def test_d14_force_is_only_valid_for_manual_revalidate():
    with pytest.raises(ValueError):
        routes.ConditionalOfferRequest(brand="reserva", url="https://www.usereserva.com/p", trigger="product_opened", force=True)
    assert routes.ConditionalOfferRequest(brand="reserva", url="https://www.usereserva.com/p", trigger="manual_revalidate", force=True).force is True


@pytest.mark.asyncio
async def test_d13_offer_failure_is_independent_from_existing_product_details_route():
    request = routes.ConditionalOfferRequest(brand="reserva", url="https://www.usereserva.com/p", trigger="product_opened")
    fake = AsyncMock(side_effect=TimeoutError("fixture timeout"))
    with patch.object(routes.brand_service, "get_brand", return_value=_brand()), patch.object(routes, "promotion_orchestrator", fake):
        result = await routes.conditional_offers(request)
    assert result.status == "error"
    assert "product" not in result.model_dump()


@pytest.mark.asyncio
async def test_d10_search_route_never_dispatches_full_browser_or_checkout_probe():
    with patch.object(routes, "promotion_orchestrator", AsyncMock()) as orchestrator:
        await routes.fast_offers_for_search([], trigger="search")
    assert all(call.kwargs.get("level") == "fast" for call in orchestrator.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "brand_value",
    [None, _brand(is_active=False)],
)
async def test_d10_route_rejects_unknown_and_inactive_brand(brand_value):
    request = routes.ConditionalOfferRequest(
        brand="reserva", url="https://www.usereserva.com/p", trigger="product_opened"
    )
    with patch.object(routes.brand_service, "get_brand", return_value=brand_value):
        with pytest.raises(HTTPException) as exc:
            await routes.conditional_offers(request)
    assert exc.value.status_code == 400


def test_d03_route_is_registered_only_on_authenticated_api_router():
    from api import api_router, public_router

    protected = [route for route in api_router.routes if route.path == "/search/product/conditional-offers"]
    public = [route for route in public_router.routes if route.path == "/search/product/conditional-offers"]
    assert len(protected) == 1 and protected[0].dependencies
    assert public == []


def _sentinel_offer():
    return ConditionalOffer(
        condition_type="membership",
        condition_label="Cliente Prime",
        eligibility="requires_membership",
        price="509.15",
        scope=OfferScope(product_id="104962", sku_id="638383", seller_id="1"),
        state="confirmed",
        source=OfferSource(
            kind="brand_custom_api",
            ref="/_v/rsv-prime/primepromotion",
            authority="authoritative",
        ),
    )


@pytest.mark.asyncio
async def test_gap01_resolved_configured_probe_is_executed_by_shared_route_runtime(monkeypatch):
    routes._clear_orchestrators()
    configured_probe_calls = []
    resolved = SimpleNamespace(
        status="ready",
        probes=(object(),),
        configs=(object(),),
        diagnostics=(),
    )

    async def execute(probes, configs, **context):
        configured_probe_calls.append((probes, configs, context))
        return ConditionalOfferResult(
            status="found",
            offers=[_sentinel_offer()],
            probe_level="full",
            sources_attempted=["configured_probe"],
        )

    engine = SimpleNamespace(get_pdp_product=AsyncMock(return_value={"conditional_offers": []}))
    monkeypatch.setattr(routes, "resolve_brand", lambda _brand: resolved)
    monkeypatch.setattr(routes, "execute_resolved_probes", execute)
    monkeypatch.setattr(
        type(routes.engine_factory), "get_engine", staticmethod(lambda _key: engine)
    )

    result = await routes.promotion_orchestrator(
        brand=_brand(),
        brand_key="reserva",
        url="https://www.usereserva.com/p",
        trigger="product_opened",
        force=True,
    )

    assert len(configured_probe_calls) == 1
    assert configured_probe_calls[0][0] is resolved.probes
    assert configured_probe_calls[0][1] is resolved.configs
    assert result.status == "found"
    assert result.sources_attempted == ["configured_probe"]


@pytest.mark.asyncio
async def test_endpoint_monitor_and_audit_share_sources_and_status(monkeypatch):
    from scripts import audit_conditional_offers as audit
    from services import price_monitor_service as monitor

    routes._clear_orchestrators()
    resolved = SimpleNamespace(
        status="ready",
        probes=(object(),),
        configs=(object(),),
        diagnostics=(),
    )

    async def execute(*_args, **_kwargs):
        return ConditionalOfferResult(
            status="found",
            offers=[_sentinel_offer()],
            probe_level="full",
            sources_attempted=["configured_probe"],
        )

    class ImmediateThrottle:
        async def run(self, _domain, callback):
            return await callback()

    brand = _brand()
    engine = SimpleNamespace(get_pdp_product=AsyncMock(return_value={"conditional_offers": []}))
    monkeypatch.setattr(routes, "resolve_brand", lambda _brand: resolved)
    monkeypatch.setattr(routes, "execute_resolved_probes", execute)
    monkeypatch.setattr(
        type(routes.engine_factory), "get_engine", staticmethod(lambda _key: engine)
    )
    monkeypatch.setattr(routes.brand_service, "get_brand", lambda _key: brand)

    endpoint = await routes.promotion_orchestrator(
        brand=brand,
        brand_key="reserva",
        url="https://www.usereserva.com/p",
        trigger="product_opened",
        force=True,
    )
    monitor_result = await monitor.collect_conditional_offers_for_monitor(
        PriceMonitorConfig(
            job_id="job-1",
            url="https://www.usereserva.com/p",
            brand="reserva",
        ),
        trigger="monitor_cycle",
    )
    sample = audit.AuditSample(
        slot="ordinary",
        selection_method="fixture",
        url="https://www.usereserva.com/p",
        status="none_detected",
    )
    audited = await audit._enrich_sample(brand, sample, throttle=ImmediateThrottle())

    assert endpoint.status.value == monitor_result.status.value == audited.status == "found"
    assert endpoint.sources_attempted == monitor_result.sources_attempted == audited.sources_attempted == [
        "configured_probe"
    ]


def test_orchestrator_registry_reuses_canonical_url_variants():
    routes._clear_orchestrators()
    created = []

    def factory():
        value = SimpleNamespace(identity=len(created))
        created.append(value)
        return value

    first_key = routes._orchestrator_key(
        "Reserva", "https://www.usereserva.com/produto/p?utm_source=a#price"
    )
    second_key = routes._orchestrator_key(
        "reserva", "https://www.usereserva.com/produto/p"
    )
    first = routes._get_or_create_orchestrator(first_key, factory)
    second = routes._get_or_create_orchestrator(second_key, factory)
    assert first_key == second_key
    assert first is second
    assert routes._orchestrator_registry_size() == 1
    assert len(created) == 1


def test_orchestrator_key_changes_when_brand_config_fingerprint_changes():
    routes._clear_orchestrators()
    url = "https://www.usereserva.com/produto/p"
    brand_v1 = _brand(engine="vtex")
    brand_v2 = _brand(engine="shopify")  # simulate an operator edit to the brand config

    key_v1 = routes._orchestrator_key("reserva", url, routes._brand_fingerprint(brand_v1))
    key_v2 = routes._orchestrator_key("reserva", url, routes._brand_fingerprint(brand_v2))
    assert key_v1 != key_v2

    first = routes._get_or_create_orchestrator(key_v1, lambda: SimpleNamespace(version=1))
    second = routes._get_or_create_orchestrator(key_v2, lambda: SimpleNamespace(version=2))
    assert first is not second
    assert routes._orchestrator_registry_size() == 2

    # Re-resolving the original (unchanged) config still hits the same cached entry.
    third = routes._get_or_create_orchestrator(
        routes._orchestrator_key("reserva", url, routes._brand_fingerprint(_brand(engine="vtex"))),
        lambda: pytest.fail("unchanged brand config should reuse the cached orchestrator"),
    )
    assert third is first


def test_orchestrator_registry_isolates_brand_keys():
    routes._clear_orchestrators()
    url = "https://shop.example/produto/p"
    first = routes._get_or_create_orchestrator(
        routes._orchestrator_key("brand-a", url), lambda: SimpleNamespace(brand="a")
    )
    second = routes._get_or_create_orchestrator(
        routes._orchestrator_key("brand-b", url), lambda: SimpleNamespace(brand="b")
    )
    assert first is not second
    assert routes._orchestrator_registry_size() == 2


def test_orchestrator_registry_evicts_least_recently_used_at_257():
    routes._clear_orchestrators()
    keys = []
    in_flight_reference = None
    for index in range(routes._ORCHESTRATOR_LIMIT):
        key = routes._orchestrator_key("brand", f"https://shop.example/p/{index}")
        keys.append(key)
        value = routes._get_or_create_orchestrator(
            key, lambda index=index: SimpleNamespace(index=index)
        )
        if index == 1:
            in_flight_reference = value
    assert routes._orchestrator_registry_size() == 256

    # Touch the oldest key, making the second inserted key the eviction target.
    routes._get_or_create_orchestrator(keys[0], lambda: pytest.fail("LRU hit recreated"))
    newest = routes._orchestrator_key("brand", "https://shop.example/p/256")
    routes._get_or_create_orchestrator(newest, lambda: SimpleNamespace(index=256))

    assert routes._orchestrator_registry_size() == 256
    assert keys[0] in routes._orchestrators
    assert keys[1] not in routes._orchestrators
    assert newest in routes._orchestrators
    assert in_flight_reference.index == 1  # eviction never cancels/mutates a live request reference
