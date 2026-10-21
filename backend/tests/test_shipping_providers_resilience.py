"""D-04 (Phase 08 Plan 04) Wave 0 gap: prova que os 3 providers de frete de
marketplace resolvem a engine pela factory (chokepoint compartilhado com
busca/detalhe/monitor) e tratam leituras degradadas do coordinator de forma
honesta — nunca reportando uma cotacao stale/blocked/unsupported como
ShippingState.AVAILABLE."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.models import SearchProductResult
from services.engines.resilient_marketplace import ResilientMarketplaceEngine
from services.shipping.amazon import AmazonShipping
from services.shipping.base import (
    DEFAULT_MESSAGES,
    ShippingState,
    apply_shipping_calculation,
)
from services.shipping.mercado_livre import MercadoLivreShipping
from services.shipping.netshoes import NetshoesShipping

_PROVIDERS = [
    (MercadoLivreShipping, "mercado_livre", "mercadolivre.com.br"),
    (AmazonShipping, "amazon", "amazon.com.br"),
    (NetshoesShipping, "netshoes", "netshoes.com.br"),
]

_PROVIDER_IDS = ["mercado_livre", "amazon", "netshoes"]


class _FakeEngine:
    """Engine falsa que devolve um dict fixo para calculate_shipping_advanced,
    sem tocar rede."""

    def __init__(self, result: Any) -> None:
        self._result = result

    async def calculate_shipping_advanced(self, url: str, zipcode: str) -> Any:
        return self._result


def _brand(domain: str) -> SimpleNamespace:
    return SimpleNamespace(brand_key="fake-brand", domain=domain)


def _product(domain: str) -> dict:
    return {"url": f"https://{domain}/produto/123"}


def _full_shipping_dict(**overrides: Any) -> dict:
    payload = {
        "shipping_price": 25.5,
        "is_free_shipping": False,
        "estimated_delivery_days": 5,
        "delivery_raw_text": "Chega em 5 dias",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,expected_marketplace,domain", _PROVIDERS, ids=_PROVIDER_IDS)
async def test_provider_resolves_engine_through_factory(
    provider_cls, expected_marketplace, domain
):
    provider = provider_cls()
    engine = provider.engine
    assert isinstance(engine, ResilientMarketplaceEngine)
    assert engine._marketplace == expected_marketplace


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,expected_marketplace,domain", _PROVIDERS, ids=_PROVIDER_IDS)
async def test_blocked_read_never_reported_as_available(provider_cls, expected_marketplace, domain):
    fake_result = {
        "_marketplace_read_state": "blocked",
        "next_retry_at": "2026-07-29T00:00:00Z",
        "last_success_at": None,
    }
    provider = provider_cls(engine=_FakeEngine(fake_result))
    calc = await provider.calculate(_product(domain), "01310100", _brand(domain))

    assert calc.state == ShippingState.BLOCKED
    assert calc.shipping_options == []
    assert calc.message == DEFAULT_MESSAGES[ShippingState.BLOCKED]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,expected_marketplace,domain", _PROVIDERS, ids=_PROVIDER_IDS)
async def test_stale_read_preserves_cached_quote_as_stale(provider_cls, expected_marketplace, domain):
    fake_result = _full_shipping_dict(
        _marketplace_read_state="stale",
        next_retry_at="2026-07-29T00:00:00Z",
        last_success_at="2026-07-28T12:00:00Z",
    )
    provider = provider_cls(engine=_FakeEngine(fake_result))
    calc = await provider.calculate(_product(domain), "01310100", _brand(domain))

    assert calc.state == ShippingState.STALE
    assert len(calc.shipping_options) == 1
    assert calc.shipping_options[0].price == fake_result["shipping_price"]
    assert calc.message == DEFAULT_MESSAGES[ShippingState.STALE]

    product = SearchProductResult(brand="fake-brand", product_name="Produto", url=_product(domain)["url"], price_full=199.9)
    product = apply_shipping_calculation(product, calc)
    assert product.shipping_price is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,expected_marketplace,domain", _PROVIDERS, ids=_PROVIDER_IDS)
async def test_unsupported_read_maps_to_unsupported_state(provider_cls, expected_marketplace, domain):
    fake_result = {
        "_marketplace_read_state": "unsupported",
        "next_retry_at": None,
        "last_success_at": None,
    }
    provider = provider_cls(engine=_FakeEngine(fake_result))
    calc = await provider.calculate(_product(domain), "01310100", _brand(domain))

    assert calc.state == ShippingState.UNSUPPORTED
    assert calc.message == DEFAULT_MESSAGES[ShippingState.UNSUPPORTED]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,expected_marketplace,domain", _PROVIDERS, ids=_PROVIDER_IDS)
async def test_current_read_still_reports_available(provider_cls, expected_marketplace, domain):
    fake_result = _full_shipping_dict(
        _marketplace_read_state="current",
        next_retry_at=None,
        last_success_at="2026-07-28T12:00:00Z",
    )
    provider = provider_cls(engine=_FakeEngine(fake_result))
    calc = await provider.calculate(_product(domain), "01310100", _brand(domain))

    assert calc.state == ShippingState.AVAILABLE
    assert len(calc.shipping_options) == 1
    assert calc.shipping_options[0].price == fake_result["shipping_price"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,expected_marketplace,domain", _PROVIDERS, ids=_PROVIDER_IDS)
async def test_dict_without_read_state_still_reports_available(provider_cls, expected_marketplace, domain):
    fake_result = _full_shipping_dict()
    provider = provider_cls(engine=_FakeEngine(fake_result))
    calc = await provider.calculate(_product(domain), "01310100", _brand(domain))

    assert calc.state == ShippingState.AVAILABLE
    assert len(calc.shipping_options) == 1
    assert calc.shipping_options[0].price == fake_result["shipping_price"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_cls,marketplace,domain", [
    (AmazonShipping, "amazon", "amazon.com.br"),
    (NetshoesShipping, "netshoes", "netshoes.com.br"),
])
async def test_missing_quote_is_temporary_failure(provider_cls, marketplace, domain):
    provider = provider_cls(engine=_FakeEngine(None))
    calc = await provider.calculate(_product(domain), "01310100", _brand(domain))

    assert calc.state == ShippingState.TEMPORARY_FAILURE
    assert calc.shipping_options == []


def test_amazon_engine_parse_shipping_text():
    from services.engines.amazon_engine import AmazonEngine
    engine = AmazonEngine()
    
    # Test 1: Explicit free shipping
    res = engine._parse_shipping_text("Entrega GRÁTIS: quinta-feira, 25 de Agosto")
    assert res is not None
    assert res["is_free_shipping"] is True
    assert res["shipping_price"] == 0.0
    
    # Explicit positive shipping
    res = engine._parse_shipping_text("Frete R$ 15,90. Chegará entre 10 e 15 dias.")
    assert res is not None
    assert res["is_free_shipping"] is False
    assert res["shipping_price"] == 15.90
    assert res.get("estimated_delivery_days") == 15
    
    # Test 3: Missing delivery text
    res = engine._parse_shipping_text("Produto disponível em estoque.")
    assert res is None


@pytest.mark.asyncio
async def test_amazon_engine_captcha_returns_blocked(monkeypatch):
    from services.engines.amazon_engine import AmazonEngine
    engine = AmazonEngine()
    
    class FakePage:
        async def content(self):
            return "<html><body>Enter the characters you see below to continue.</body></html>"
            
        async def goto(self, *args, **kwargs):
            pass
            
    class FakeContext:
        async def new_page(self):
            return FakePage()
            
    class FakeBrowser:
        async def new_context(self, *args, **kwargs):
            return FakeContext()
            
        async def close(self):
            pass
            
    class FakePlaywright:
        class Chromium:
            async def launch(self, *args, **kwargs):
                return FakeBrowser()
        chromium = Chromium()
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    # Mock playwright async_api to use our FakePlaywright
    import sys
    # This might be tricky if we don't mock it at the right level.
    # We can mock async_playwright directly.
    import core.browser_manager
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakePlaywright())
    
    # But wait, AmazonEngine imports it locally inside the method:
    # from playwright.async_api import async_playwright
    # So we patch sys.modules or use unittest.mock.patch
    from unittest.mock import patch
    with patch("playwright.async_api.async_playwright", return_value=FakePlaywright()):
        res = await engine.calculate_shipping_advanced("https://amazon.com.br/dp/TEST", "01310100")
        
    assert res is not None
    assert res["_marketplace_read_state"] == "blocked"
    assert "error" in res
