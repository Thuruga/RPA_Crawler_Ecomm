"""Unit and integration tests for Dafiti and Lacoste shipping providers."""

from __future__ import annotations

import pytest
from core.models import DynamicBrand, SearchProductResult
from services.shipping.base import (
    ShippingState,
    apply_shipping_calculation,
)
from services.shipping.dafiti import (
    DafitiShipping,
    DAFITI_FREE_SHIPPING_THRESHOLD_SOUTH_SOUTHEAST,
    DAFITI_FREE_SHIPPING_THRESHOLD_OTHER,
    DAFITI_STANDARD_PRICE_SP,
    DAFITI_STANDARD_PRICE_SOUTH_SOUTHEAST,
    DAFITI_STANDARD_PRICE_OTHER,
    DAFITI_EXPRESS_PRICE,
)
from services.shipping.lacoste import (
    LacosteShipping,
    LACOSTE_FREE_SHIPPING_THRESHOLD,
    LACOSTE_STANDARD_SHIPPING_PRICE,
    LACOSTE_EXPRESS_SHIPPING_PRICE,
)
from services.shipping.resolver import resolve_shipping_provider


@pytest.fixture
def dafiti_brand() -> DynamicBrand:
    return DynamicBrand(
        brand_key="dafiti",
        brand_name="Dafiti",
        domain="www.dafiti.com.br",
        engine="dafiti",
        domain_aliases=["dafiti.com.br", "www.dafiti.com.br", "secure.dafiti.com.br"],
    )


@pytest.fixture
def lacoste_brand() -> DynamicBrand:
    return DynamicBrand(
        brand_key="lacoste",
        brand_name="Lacoste",
        domain="lacoste.com.br",
        engine="algolia",
        domain_aliases=["lacoste.com", "www.lacoste.com", "lacoste.com.br", "www.lacoste.com.br"],
    )


# ==============================================================================
# DafitiShipping Tests
# ==============================================================================

class TestDafitiShipping:
    @pytest.mark.asyncio
    async def test_invalid_zipcode(self, dafiti_brand):
        provider = DafitiShipping()
        product = SearchProductResult(
            brand="dafiti",
            product_name="Tenis",
            url="https://www.dafiti.com.br/tenis-p1.html",
        )
        res = await provider.calculate(product, "bad-cep", dafiti_brand)
        assert res.state == ShippingState.UNAVAILABLE_FOR_CEP

    @pytest.mark.asyncio
    async def test_invalid_domain(self, dafiti_brand):
        provider = DafitiShipping()
        product = SearchProductResult(
            brand="dafiti",
            product_name="Tenis",
            url="https://www.sitefalso.com.br/tenis-p1.html",
        )
        res = await provider.calculate(product, "01310-100", dafiti_brand)
        assert res.state == ShippingState.UNSUPPORTED

    @pytest.mark.asyncio
    async def test_sp_capital_under_threshold(self, dafiti_brand):
        provider = DafitiShipping()
        product = SearchProductResult(
            brand="dafiti",
            product_name="Camiseta",
            url="https://www.dafiti.com.br/camiseta-p1.html",
            price_full=59.90,
        )
        # SP Capital CEP: 01310-100
        res = await provider.calculate(product, "01310-100", dafiti_brand)
        assert res.state == ShippingState.AVAILABLE
        assert len(res.shipping_options) == 3

        service_names = [o.service_name for o in res.shipping_options]
        assert "Retira Fácil" in service_names
        assert "Entrega Padrão" in service_names
        assert "Entrega Rápida" in service_names

        padrao = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert padrao.price == DAFITI_STANDARD_PRICE_SP
        assert padrao.is_free_shipping is False

        retira = next(o for o in res.shipping_options if o.service_name == "Retira Fácil")
        assert retira.price == 0.0
        assert retira.is_free_shipping is True

    @pytest.mark.asyncio
    async def test_south_southeast_free_shipping_above_threshold(self, dafiti_brand):
        provider = DafitiShipping()
        product = SearchProductResult(
            brand="dafiti",
            product_name="Tenis",
            url="https://www.dafiti.com.br/tenis-p1.html",
            price_full=DAFITI_FREE_SHIPPING_THRESHOLD_SOUTH_SOUTHEAST + 20.0,
        )
        # Curitiba CEP: 80010-000
        res = await provider.calculate(product, "80010-000", dafiti_brand)
        assert res.state == ShippingState.AVAILABLE

        padrao = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert padrao.price == 0.0
        assert padrao.is_free_shipping is True

    @pytest.mark.asyncio
    async def test_north_northeast_under_threshold(self, dafiti_brand):
        provider = DafitiShipping()
        product = SearchProductResult(
            brand="dafiti",
            product_name="Tenis",
            url="https://www.dafiti.com.br/tenis-p1.html",
            price_full=120.0,  # Below North/Northeast threshold (199.90)
        )
        # Salvador - BA: 40010-000
        res = await provider.calculate(product, "40010-000", dafiti_brand)
        assert res.state == ShippingState.AVAILABLE

        padrao = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert padrao.price == DAFITI_STANDARD_PRICE_OTHER
        assert padrao.is_free_shipping is False


# ==============================================================================
# LacosteShipping Tests
# ==============================================================================

class TestLacosteShipping:
    @pytest.mark.asyncio
    async def test_invalid_zipcode(self, lacoste_brand):
        provider = LacosteShipping()
        product = SearchProductResult(
            brand="lacoste",
            product_name="Polo",
            url="https://www.lacoste.com/br/polo-p1.html",
        )
        res = await provider.calculate(product, "abc", lacoste_brand)
        assert res.state == ShippingState.UNAVAILABLE_FOR_CEP

    @pytest.mark.asyncio
    async def test_standard_and_clique_retire_under_threshold(self, lacoste_brand):
        provider = LacosteShipping()
        product = SearchProductResult(
            brand="lacoste",
            product_name="Camiseta",
            url="https://www.lacoste.com/br/camiseta-p1.html",
            price_full=289.0,
        )
        # Interior SP: 14000-000
        res = await provider.calculate(product, "14000-000", lacoste_brand)
        assert res.state == ShippingState.AVAILABLE
        assert len(res.shipping_options) == 2

        padrao = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert padrao.price == LACOSTE_STANDARD_SHIPPING_PRICE
        assert padrao.is_free_shipping is False

        retire = next(o for o in res.shipping_options if o.service_name == "Clique e Retire")
        assert retire.price == 0.0
        assert retire.is_free_shipping is True

    @pytest.mark.asyncio
    async def test_free_standard_shipping_above_threshold(self, lacoste_brand):
        provider = LacosteShipping()
        product = SearchProductResult(
            brand="lacoste",
            product_name="Polo Classic Fit",
            url="https://www.lacoste.com/br/polo-p1.html",
            price_full=LACOSTE_FREE_SHIPPING_THRESHOLD + 10.0,
        )
        res = await provider.calculate(product, "14000-000", lacoste_brand)
        assert res.state == ShippingState.AVAILABLE

        padrao = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert padrao.price == 0.0
        assert padrao.is_free_shipping is True

    @pytest.mark.asyncio
    async def test_metro_express_option_added(self, lacoste_brand):
        provider = LacosteShipping()
        product = SearchProductResult(
            brand="lacoste",
            product_name="Jaqueta",
            url="https://www.lacoste.com/br/jaqueta-p1.html",
            price_full=899.0,
        )
        # Sao Paulo Capital: 01310-100
        res = await provider.calculate(product, "01310-100", lacoste_brand)
        assert res.state == ShippingState.AVAILABLE
        assert len(res.shipping_options) == 3

        express = next(o for o in res.shipping_options if o.service_name == "Entrega Expressa")
        assert express.price == LACOSTE_EXPRESS_SHIPPING_PRICE
        assert express.estimated_delivery_days == 1


# ==============================================================================
# Resolver Integration Tests
# ==============================================================================

class TestResolverDafitiLacoste:
    def test_resolve_dafiti(self, dafiti_brand):
        provider = resolve_shipping_provider(dafiti_brand)
        assert isinstance(provider, DafitiShipping)

    def test_resolve_lacoste(self, lacoste_brand):
        provider = resolve_shipping_provider(lacoste_brand)
        assert isinstance(provider, LacosteShipping)

    def test_apply_shipping_calculation(self, lacoste_brand):
        product = SearchProductResult(
            brand="lacoste",
            product_name="Polo",
            url="https://www.lacoste.com/br/polo-p1.html",
            price_full=699.0,
        )
        provider = resolve_shipping_provider(lacoste_brand)
        import asyncio
        calc = asyncio.run(provider.calculate(product, "01310-100", lacoste_brand))

        apply_shipping_calculation(product, calc)
        assert product.shipping is not None
        assert product.shipping_price == 0.0
        assert product.is_free_shipping is True
        assert len(product.shipping_options) == 3


# ==============================================================================
# PrivaliaShipping Tests
# ==============================================================================

@pytest.fixture
def privalia_brand() -> DynamicBrand:
    return DynamicBrand(
        brand_key="privalia",
        brand_name="Privalia",
        domain="brandsplace.privalia.com",
        engine="privalia",
        domain_aliases=["brandsplace.privalia.com", "br.privalia.com", "privalia.com", "privalia.com.br"],
    )


class TestPrivaliaShipping:
    @pytest.mark.asyncio
    async def test_invalid_zipcode(self, privalia_brand):
        from services.shipping.privalia import PrivaliaShipping
        provider = PrivaliaShipping()
        product = SearchProductResult(
            brand="privalia",
            product_name="Camisa Nike",
            url="https://brandsplace.privalia.com/item/camisa-nike",
            price_full=150.0,
        )
        res = await provider.calculate(product, "abc", privalia_brand)
        assert res.state == ShippingState.UNAVAILABLE_FOR_CEP

    @pytest.mark.asyncio
    async def test_sp_capital_under_threshold(self, privalia_brand):
        from services.shipping.privalia import PrivaliaShipping
        provider = PrivaliaShipping()
        product = SearchProductResult(
            brand="privalia",
            product_name="Camisa Nike",
            url="https://brandsplace.privalia.com/item/camisa-nike",
            price_full=150.0,
        )
        # SP: 01310-100
        res = await provider.calculate(product, "01310100", privalia_brand)
        assert res.state == ShippingState.AVAILABLE
        standard = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert standard.price == 19.90
        assert standard.is_free_shipping is False

    @pytest.mark.asyncio
    async def test_south_southeast_free_shipping_premium(self, privalia_brand):
        from services.shipping.privalia import PrivaliaShipping
        provider = PrivaliaShipping()
        product = SearchProductResult(
            brand="privalia",
            product_name="Tênis Luxo",
            url="https://brandsplace.privalia.com/item/tenis-luxo",
            price_full=350.0,
        )
        # Curitiba (Sul): 80010-000
        res = await provider.calculate(product, "80010-000", privalia_brand)
        assert res.state == ShippingState.AVAILABLE
        standard = next(o for o in res.shipping_options if o.service_name == "Entrega Padrão")
        assert standard.price == 0.0
        assert standard.is_free_shipping is True

    def test_resolve_privalia(self, privalia_brand):
        from services.shipping.privalia import PrivaliaShipping
        provider = resolve_shipping_provider(privalia_brand)
        assert isinstance(provider, PrivaliaShipping)
