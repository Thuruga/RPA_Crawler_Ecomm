from __future__ import annotations

import pytest

from core.models import BrandSearchResult
from services.engines.resilient_marketplace import ResilientMarketplaceEngine


def test_cross_marketplace_service_uses_resilient_factory_engines():
    from services.cross_marketplace_service import CrossMarketplaceService

    service = CrossMarketplaceService()

    assert service._engine_instances
    assert all(
        isinstance(engine, ResilientMarketplaceEngine)
        for engine in service._engine_instances.values()
    )


@pytest.mark.asyncio
async def test_cross_marketplace_preserves_structured_engine_error():
    from services.cross_marketplace_service import CrossMarketplaceService

    class _UnavailableEngine:
        async def search(self, **_kwargs):
            return BrandSearchResult(
                brand_key="netshoes",
                brand_name="Netshoes",
                error="marketplace blocked (proxima tentativa: amanha)",
            )

    service = CrossMarketplaceService()
    service._active_engines = lambda: {"Netshoes": _UnavailableEngine()}

    products, errors = await service._fetch_all_engines("camisa")

    assert products == []
    assert errors == [
        {
            "marketplace": "Netshoes",
            "reason": "marketplace blocked (proxima tentativa: amanha)",
        }
    ]


@pytest.mark.asyncio
async def test_enrich_pdp_and_shipping_surfaces_blocked_state_instead_of_faking_no_shipping():
    """CR-02 regression: ResilientMarketplaceEngine never returns None anymore —
    a blocked/unsupported read comes back as a truthy dict carrying
    `_marketplace_read_state`. _enrich_pdp_and_shipping must gate on that field
    before the truthiness check (Pitfall 4), or a genuinely blocked marketplace
    silently degrades to "no shipping info, not free" instead of surfacing the
    `_shipping_state == 'blocked'` indicator the UI relies on."""
    from services.cross_marketplace_service import CrossMarketplaceService

    class _BlockedEngine:
        SHIPPING_TIER2_BLOCKS_ON_NONE = True

        async def get_product_details(self, _url):
            return {
                "_marketplace_read_state": "blocked",
                "last_success_at": None,
                "next_retry_at": "2026-07-29T00:00:00Z",
            }

        async def calculate_shipping(self, _product, _zipcode):
            return {
                "_marketplace_read_state": "blocked",
                "last_success_at": None,
                "next_retry_at": "2026-07-29T00:00:00Z",
            }

    class _UnsupportedShippingEngine:
        SHIPPING_TIER2_BLOCKS_ON_NONE = False

        async def get_product_details(self, _url):
            return {"seller": "Loja Oficial", "price": 199.9}

        async def calculate_shipping(self, _product, _zipcode):
            return {
                "_marketplace_read_state": "unsupported",
                "last_success_at": None,
                "next_retry_at": None,
            }

    service = CrossMarketplaceService()
    service._by_display = {
        "Netshoes": _BlockedEngine(),
        "Amazon": _UnsupportedShippingEngine(),
    }

    blocked_product = {
        "plataforma": "Netshoes",
        "url": "https://www.netshoes.com.br/produto",
        "seller": "Vendedor da listagem",
        "preco": 100.0,
        "is_free_shipping": True,
        "shipping_price": 0.0,
    }
    unsupported_product = {
        "plataforma": "Amazon",
        "url": "https://www.amazon.com.br/produto",
        "seller": "Vendedor da listagem",
        "preco": 100.0,
    }

    await service._enrich_pdp_and_shipping(
        [blocked_product, unsupported_product], "01310100"
    )

    # Blocked: PDP data untouched (listing seller/price preserved) and the
    # shipping-blocked indicator surfaced instead of a faked "not free" quote.
    assert blocked_product["seller"] == "Vendedor da listagem"
    assert blocked_product["preco"] == 100.0
    assert blocked_product["_shipping_state"] == "blocked"
    assert blocked_product["is_free_shipping"] is True  # not overwritten to False
    assert blocked_product["shipping_price"] == 0.0  # not overwritten to None

    # Unsupported (Amazon's documented no-op stub): PDP data applies normally,
    # shipping surfaces "unsupported" rather than faking "not free".
    assert unsupported_product["seller"] == "Loja Oficial"
    assert unsupported_product["preco"] == 199.9
    assert unsupported_product["_shipping_state"] == "unsupported"
    assert "is_free_shipping" not in unsupported_product
