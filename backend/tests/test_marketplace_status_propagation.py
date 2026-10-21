"""
POST /product/details e POST /calculate-shipping — propagacao do estado de
leitura do coordinator (_marketplace_read_state) nas rotas sob demanda que a
Fase 7 nao reescreveu.

Split de Categoria 4 (08-RESEARCH.md): 2 dos 4 testes recuperaveis do arquivo
original do stash ficam aqui (nivel de rota); os outros 2 (nivel de chokepoint
da factory) ficam em test_marketplace_resilient_factory.py (plano 08-02). Os
3 testes de infraestrutura de rede/classificacao Netshoes do arquivo original
foram descartados (fora de escopo desta fase).

Alem dos 2 testes recuperados do stash (que so cobrem o caso blocked), este
arquivo adiciona 4 testes novos para cobrir os quatro estados do contrato
(ok/stale/blocked/unsupported) nas duas rotas.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from unittest.mock import patch

from api import routes_search


class _BlockedEngine:
    async def get_pdp_product(self, _url: str):
        return {
            "_marketplace_read_state": "blocked",
            "last_success_at": None,
            "next_retry_at": "2026-07-18T13:34:58Z",
        }

    async def calculate_shipping_advanced(self, _url: str, _zipcode: str):
        return {
            "_marketplace_read_state": "blocked",
            "last_success_at": None,
            "next_retry_at": "2026-07-18T13:34:58Z",
        }


@pytest.mark.asyncio
async def test_product_details_propagates_marketplace_block_before_product_validation():
    request = routes_search.ProductDetailsRequest(
        brand="netshoes",
        url="https://www.netshoes.com.br/produto",
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=None), patch.object(
        routes_search.engine_factory, "get_engine", return_value=_BlockedEngine()
    ):
        result = await routes_search.product_details(request)

    assert result["state"] == "blocked"
    assert result["product"] is None
    assert result["next_retry_at"] == "2026-07-18T13:34:58Z"


@pytest.mark.asyncio
async def test_shipping_route_never_reports_blocked_marketplace_as_success():
    request = routes_search.CalculateShippingRequest(
        marketplace="netshoes",
        url="https://www.netshoes.com.br/produto",
        zipcode="01310100",
    )

    with patch.object(routes_search.engine_factory, "get_engine", return_value=_BlockedEngine()):
        result = await routes_search.calculate_shipping_single(request)

    assert result["status"] == "blocked"
    assert result["shipping_info"] is None
    assert result["next_retry_at"] == "2026-07-18T13:34:58Z"


@pytest.mark.asyncio
async def test_shipping_route_rejects_url_outside_requested_marketplace():
    request = routes_search.CalculateShippingRequest(
        marketplace="netshoes",
        url="https://www.amazon.com.br/produto",
        zipcode="01310100",
    )

    with patch.object(routes_search.engine_factory, "get_engine", side_effect=AssertionError("engine should not resolve")):
        with pytest.raises(HTTPException) as exc_info:
            await routes_search.calculate_shipping_single(request)

    assert exc_info.value.status_code == 400
    assert "marketplace solicitado" in exc_info.value.detail


def test_calculate_shipping_request_normalizes_marketplace_alias_and_validates_cep():
    request = routes_search.CalculateShippingRequest(
        marketplace="Mercado Livre",
        url="https://produto.mercadolivre.com.br/produto",
        zipcode="01310-100",
    )

    assert request.marketplace == "mercado_livre"
    assert request.zipcode == "01310-100"

class _StaleProductEngine:
    async def get_pdp_product(self, url: str):
        return {
            "_marketplace_read_state": "stale",
            "last_success_at": "2026-07-17T10:00:00Z",
            "next_retry_at": "2026-07-18T13:34:58Z",
            "url": url,
            "brand": "netshoes",
            "raw_title": "Tenis Netshoes",
            "raw_description": "Descricao do tenis",
            "price_full": 299.90,
            "price_discount": 0.0,
            "price_discount_is_delta": True,
            "stock_availability": True,
            "image_url": "https://www.netshoes.com.br/img/tenis.jpg",
            "available_colors": ["Preto"],
            "available_sizes": ["40", "41", "42"],
        }


class _PlainProductEngine:
    """Produto completo sem nenhuma chave de metadado do coordinator (marca propria)."""

    async def get_pdp_product(self, url: str):
        return {
            "url": url,
            "brand": "exemplo",
            "raw_title": "Camisa Exemplo",
            "raw_description": "Descricao da camisa",
            "price_full": 149.90,
            "price_discount": 0.0,
            "price_discount_is_delta": True,
            "stock_availability": True,
            "image_url": "https://www.example.com/img/camisa.jpg",
            "available_colors": ["Azul"],
            "available_sizes": ["P", "M", "G"],
        }


@pytest.mark.asyncio
async def test_product_details_reports_stale_without_losing_product():
    request = routes_search.ProductDetailsRequest(
        brand="netshoes",
        url="https://www.netshoes.com.br/produto",
    )

    with patch.object(routes_search.brand_service, "get_brand", return_value=None), patch.object(
        routes_search.engine_factory, "get_engine", return_value=_StaleProductEngine()
    ):
        result = await routes_search.product_details(request)

    assert result["state"] == "stale"
    assert result["product"] is not None
    assert result["product"]["raw_title"] == "Tenis Netshoes"
    assert result["last_success_at"] == "2026-07-17T10:00:00Z"
    assert result["next_retry_at"] == "2026-07-18T13:34:58Z"


@pytest.mark.asyncio
async def test_product_details_still_reports_ok_for_plain_engine():
    request = routes_search.ProductDetailsRequest(
        brand="exemplo",
        url="https://www.example.com/camisa-exemplo",
    )
    fake_brand = type(
        "FakeBrand", (), {"brand_key": "exemplo", "domain": "www.example.com", "is_active": True, "engine": "vtex"}
    )()

    with patch.object(routes_search.brand_service, "get_brand", return_value=fake_brand), patch.object(
        routes_search.engine_factory, "get_engine", return_value=_PlainProductEngine()
    ):
        result = await routes_search.product_details(request)

    assert result["state"] == "ok"
    assert result["product"] is not None
    assert result["last_success_at"] is None
    assert result["next_retry_at"] is None


class _StaleShippingEngine:
    async def calculate_shipping_advanced(self, _url: str, _zipcode: str):
        return {
            "_marketplace_read_state": "stale",
            "last_success_at": "2026-07-17T10:00:00Z",
            "next_retry_at": "2026-07-18T13:34:58Z",
            "shipping_cost": 19.90,
            "shipping_time": "5 dias uteis",
            "is_free_shipping": False,
        }


class _PlainShippingEngine:
    async def calculate_shipping_advanced(self, _url: str, _zipcode: str):
        return {
            "shipping_cost": 0.0,
            "shipping_time": "3 dias uteis",
            "is_free_shipping": True,
        }


@pytest.mark.asyncio
async def test_shipping_route_reports_stale_preserving_cached_quote():
    request = routes_search.CalculateShippingRequest(
        marketplace="netshoes",
        url="https://www.netshoes.com.br/produto",
        zipcode="01310100",
    )

    with patch.object(routes_search.engine_factory, "get_engine", return_value=_StaleShippingEngine()):
        result = await routes_search.calculate_shipping_single(request)

    assert result["status"] == "stale"
    assert result["shipping_info"] is not None
    assert result["shipping_info"] == {
        "shipping_cost": 19.90,
        "shipping_time": "5 dias uteis",
        "is_free_shipping": False,
    }
    assert "_marketplace_read_state" not in result["shipping_info"]
    assert "last_success_at" not in result["shipping_info"]
    assert "next_retry_at" not in result["shipping_info"]
    assert result["last_success_at"] == "2026-07-17T10:00:00Z"
    assert result["next_retry_at"] == "2026-07-18T13:34:58Z"


@pytest.mark.asyncio
async def test_shipping_route_still_reports_success_for_plain_engine():
    request = routes_search.CalculateShippingRequest(
        marketplace="mercado_livre",
        url="https://www.mercadolivre.com.br/produto",
        zipcode="01310100",
    )

    with patch.object(routes_search.engine_factory, "get_engine", return_value=_PlainShippingEngine()):
        result = await routes_search.calculate_shipping_single(request)

    assert result["status"] == "success"
    assert result["shipping_info"] == {
        "shipping_cost": 0.0,
        "shipping_time": "3 dias uteis",
        "is_free_shipping": True,
    }
