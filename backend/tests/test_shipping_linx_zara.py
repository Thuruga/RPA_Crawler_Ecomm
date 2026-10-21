"""Unit and integration tests for Linx and Zara shipping providers."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.models import DynamicBrand, SearchProductResult
from services.shipping.base import (
    ShippingCalculation,
    ShippingState,
    apply_shipping_calculation,
)
from services.shipping.linx import LinxShipping
from services.shipping.resolver import resolve_shipping_provider
from services.shipping.zara import (
    ZaraShipping,
    ZARA_FREE_SHIPPING_THRESHOLD,
    ZARA_STANDARD_SHIPPING_PRICE,
    ZARA_EXPRESS_SHIPPING_PRICE,
)


@pytest.fixture
def brooksfield_brand() -> DynamicBrand:
    return DynamicBrand(
        brand_key="brooksfield",
        brand_name="Brooksfield",
        domain="www.brooksfield.com.br",
        engine="linx",
        domain_aliases=[
            "brooksfield.com.br",
            "www.brooksfield.com.br",
            "brooksfieldjunior.com.br",
            "www.brooksfieldjunior.com.br",
        ],
    )


@pytest.fixture
def zara_brand() -> DynamicBrand:
    return DynamicBrand(
        brand_key="zara",
        brand_name="Zara",
        domain="www.zara.com",
        engine="zara",
        domain_aliases=["zara.com", "www.zara.com", "kids.zara.com"],
    )


# ==============================================================================
# LinxShipping Tests
# ==============================================================================

class TestLinxShipping:
    def test_parse_currency(self):
        provider = LinxShipping()
        assert provider._parse_currency("Frete Grátis") == 0.0
        assert provider._parse_currency("Entrega gratis") == 0.0
        assert provider._parse_currency("R$ 46,70") == 46.70
        assert provider._parse_currency("Sedex - R$ 1.250,50") == 1250.50
        assert provider._parse_currency("R$ 0,00") == 0.0
        assert provider._parse_currency("Indisponivel") is None
        assert provider._parse_currency("") is None

    def test_parse_days(self):
        provider = LinxShipping()
        assert provider._parse_days("Em até 5 dias úteis") == 5
        assert provider._parse_days("1 dia útil") == 1
        assert provider._parse_days("12 dias") == 12
        assert provider._parse_days("Entrega imediata") is None
        assert provider._parse_days("") is None

    @pytest.mark.asyncio
    async def test_invalid_zipcode(self, brooksfield_brand):
        provider = LinxShipping()
        product = SearchProductResult(
            brand="brooksfield",
            product_name="Camisa",
            url="https://www.brooksfield.com.br/camisa-p123",
        )
        res = await provider.calculate(product, "invalid-cep", brooksfield_brand)
        assert res.state == ShippingState.UNAVAILABLE_FOR_CEP
        assert "invalido" in (res.message or "").lower()

    @pytest.mark.asyncio
    async def test_invalid_domain(self, brooksfield_brand):
        provider = LinxShipping()
        product = SearchProductResult(
            brand="brooksfield",
            product_name="Camisa",
            url="https://www.loja-estranha.com.br/camisa-p123",
        )
        res = await provider.calculate(product, "01310100", brooksfield_brand)
        assert res.state == ShippingState.UNSUPPORTED

    @pytest.mark.asyncio
    async def test_successful_quote_parsing(self, brooksfield_brand):
        html_response = """
        <div class="deliveryfee-result">
            <ul class="items">
                <li class="item">
                    <strong>Expressa</strong>
                    <span>R$ 0,00</span>
                    <p>Entrega em até 3 dias úteis</p>
                </li>
                <li class="item">
                    <strong>Sedex</strong>
                    <span>R$ 46,70</span>
                    <p>Entrega em até 2 dias úteis</p>
                </li>
            </ul>
        </div>
        """
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html_response

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        provider = LinxShipping(session_factory=lambda: mock_session)
        product = SearchProductResult(
            brand="brooksfield",
            product_name="Camisa",
            url="https://www.brooksfield.com.br/camisa-p46143?v=116496",
            shipping_product_id="46143",
            shipping_variant_id="116496",
        )

        res = await provider.calculate(product, "01310100", brooksfield_brand)
        assert res.state == ShippingState.AVAILABLE
        assert len(res.shipping_options) == 2

        # Cheapest first
        cheapest = res.shipping_options[0]
        assert cheapest.price == 0.0
        assert cheapest.is_free_shipping is True
        assert cheapest.service_name == "Expressa"
        assert cheapest.estimated_delivery_days == 3

        second = res.shipping_options[1]
        assert second.price == 46.70
        assert second.service_name == "Sedex"
        assert second.estimated_delivery_days == 2

    @pytest.mark.asyncio
    async def test_insufficient_stock(self, brooksfield_brand):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<div class='error'>Estoque insuficiente para esta localidade</div>"

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = None

        provider = LinxShipping(session_factory=lambda: mock_session)
        product = SearchProductResult(
            brand="brooksfield",
            product_name="Camisa",
            url="https://www.brooksfield.com.br/camisa-p46143?v=116496",
            shipping_product_id="46143",
            shipping_variant_id="116496",
        )

        res = await provider.calculate(product, "01310100", brooksfield_brand)
        assert res.state == ShippingState.UNAVAILABLE_FOR_CEP


# ==============================================================================
# ZaraShipping Tests
# ==============================================================================

class TestZaraShipping:
    @pytest.mark.asyncio
    async def test_invalid_zipcode(self, zara_brand):
        provider = ZaraShipping()
        product = SearchProductResult(
            brand="zara",
            product_name="Camisa",
            url="https://www.zara.com/br/pt/camisa-p123.html",
        )
        res = await provider.calculate(product, "123", zara_brand)
        assert res.state == ShippingState.UNAVAILABLE_FOR_CEP

    @pytest.mark.asyncio
    async def test_standard_and_store_pickup_under_threshold(self, zara_brand):
        provider = ZaraShipping()
        product = SearchProductResult(
            brand="zara",
            product_name="Camisa",
            url="https://www.zara.com/br/pt/camisa-p123.html",
            price_full=199.00,
        )
        # Non-metro CEP (Manaus - AM: 69000-000)
        res = await provider.calculate(product, "69000-000", zara_brand)
        assert res.state == ShippingState.AVAILABLE
        assert len(res.shipping_options) == 2

        # 1. Retirada em Loja (R$ 0,00)
        assert res.shipping_options[0].service_name == "Retirada em Loja"
        assert res.shipping_options[0].price == 0.0
        assert res.shipping_options[0].is_free_shipping is True

        # 2. Entrega Padrao (R$ 14,00)
        assert res.shipping_options[1].service_name == "Entrega Padrão"
        assert res.shipping_options[1].price == ZARA_STANDARD_SHIPPING_PRICE
        assert res.shipping_options[1].is_free_shipping is False

    @pytest.mark.asyncio
    async def test_free_standard_shipping_over_threshold(self, zara_brand):
        provider = ZaraShipping()
        product = SearchProductResult(
            brand="zara",
            product_name="Casaco Sobretudo",
            url="https://www.zara.com/br/pt/casaco-p123.html",
            price_full=ZARA_FREE_SHIPPING_THRESHOLD + 10.0,
        )
        res = await provider.calculate(product, "69000-000", zara_brand)
        assert res.state == ShippingState.AVAILABLE

        # Both options should be free
        for opt in res.shipping_options:
            assert opt.price == 0.0
            assert opt.is_free_shipping is True

    @pytest.mark.asyncio
    async def test_metro_express_option_added(self, zara_brand):
        provider = ZaraShipping()
        product = SearchProductResult(
            brand="zara",
            product_name="Camisa",
            url="https://www.zara.com/br/pt/camisa-p123.html",
            price_full=250.00,
        )
        # Sao Paulo Capital CEP: 01310-100
        res = await provider.calculate(product, "01310-100", zara_brand)
        assert res.state == ShippingState.AVAILABLE
        assert len(res.shipping_options) == 3

        service_names = [opt.service_name for opt in res.shipping_options]
        assert "Entrega Expressa" in service_names
        express = next(opt for opt in res.shipping_options if opt.service_name == "Entrega Expressa")
        assert express.price == ZARA_EXPRESS_SHIPPING_PRICE
        assert express.estimated_delivery_days == 1


# ==============================================================================
# Resolver & Model Application Tests
# ==============================================================================

class TestResolverAndApplication:
    def test_resolve_linx(self, brooksfield_brand):
        provider = resolve_shipping_provider(brooksfield_brand)
        assert isinstance(provider, LinxShipping)

    def test_resolve_zara(self, zara_brand):
        provider = resolve_shipping_provider(zara_brand)
        assert isinstance(provider, ZaraShipping)

    def test_apply_shipping_calculation(self):
        product = SearchProductResult(
            brand="zara",
            product_name="Jaqueta",
            url="https://www.zara.com/br/pt/jaqueta-p1.html",
            price_full=599.0,
        )
        provider = ZaraShipping()
        import asyncio
        calc = asyncio.run(provider.calculate(product, "01310-100", DynamicBrand(brand_key="zara", brand_name="Zara", domain="www.zara.com", engine="zara")))

        apply_shipping_calculation(product, calc)
        assert product.shipping is not None
        assert product.shipping_price == 0.0
        assert product.is_free_shipping is True
        assert len(product.shipping_options) >= 2
