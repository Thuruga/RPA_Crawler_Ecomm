import asyncio
import json
from unittest.mock import AsyncMock, patch
import pytest

from services.brand_service import brand_service
from services.engines.factory import EngineFactory
from services.engines.linx_engine import LinxEngine, LinxProduct


def test_factory_returns_linx_engine_for_brooksfield():
    """Valida que o EngineFactory instancia LinxEngine para a marca 'brooksfield'."""
    engine = EngineFactory.get_engine("brooksfield")
    assert isinstance(engine, LinxEngine)
    assert engine.brand_key == "brooksfield"
    assert engine.get_engine_name() == "LinxCommerce"


def test_brooksfield_mappings_exist():
    """Valida que as categorias canônicas essenciais adultas e infantis estão mapeadas."""
    brand_data = brand_service.get_brand("brooksfield")
    assert brand_data is not None
    mappings = brand_data.get("mappings") if isinstance(brand_data, dict) else getattr(brand_data, "mappings", [])
    assert len(mappings) > 0

    slugs = {m["canonical_slug"] if isinstance(m, dict) else getattr(m, "canonical_slug") for m in mappings}
    required_slugs = {
        "camisas",
        "camisetas",
        "polos",
        "calcas",
        "bermudas",
        "jaquetas",
        "alfaiataria",
        "infantil-camisas",
        "infantil-camisetas",
        "infantil-polos",
        "infantil-calcas",
        "infantil-bermudas",
        "infantil-jaquetas",
        "infantil-alfaiataria",
    }
    for slug in required_slugs:
        assert slug in slugs, f"Categoria {slug} deve estar mapeada para Brooksfield"


def test_domain_resolution_behavior():
    """Valida que o roteamento de domínio separa adulto e infantil conforme especificado."""
    engine = LinxEngine(brand_key="brooksfield")

    # Adulto
    assert engine._resolve_domain_for_path("/adulto/camisas") == "www.brooksfield.com.br"
    assert engine._resolve_domain_for_path("/adulto/polos") == "www.brooksfield.com.br"
    assert engine._resolve_domain_for_path("/alfaiataria") == "www.brooksfield.com.br"
    assert engine._resolve_domain_for_path("https://www.brooksfield.com.br/adulto/camisas") == "www.brooksfield.com.br"

    # Infantil (Brooksfield Junior)
    assert engine._resolve_domain_for_path("/junior/camisas-jr") == "www.brooksfieldjunior.com.br"
    assert engine._resolve_domain_for_path("/junior/polos-jr") == "www.brooksfieldjunior.com.br"
    assert engine._resolve_domain_for_path("infantil-camisas") == "www.brooksfieldjunior.com.br"
    assert engine._resolve_domain_for_path("https://www.brooksfieldjunior.com.br/junior/polos-jr") == "www.brooksfieldjunior.com.br"
    assert engine._resolve_domain_for_path("polo infantil menino") == "www.brooksfieldjunior.com.br"


@pytest.mark.asyncio
async def test_pagination_stop_condition_empty_page():
    """Testa que a paginação encerra imediatamente ao encontrar uma página sem produtos."""
    engine = LinxEngine(brand_key="brooksfield")

    empty_html = "<html><body><ul class='products'></ul></body></html>"

    with patch.object(engine, "_fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = empty_html

        collected = []
        async for item in engine.run_bulk_scrape("/adulto/camisas"):
            collected.append(item)

        assert len(collected) == 0
        assert mock_fetch.call_count == 1
        assert "pg=1" in mock_fetch.call_args[0][0]


@pytest.mark.asyncio
async def test_pagination_multiple_pages_and_duplicate_stop():
    """Testa paginação por múltiplas páginas e parada em cards duplicados."""
    engine = LinxEngine(brand_key="brooksfield")

    page1_html = """
    <html><body><ul>
      <li class="purchasable" data-product-id="101" data-name="Camisa 101" data-brand="Brooksfield" data-stock="5" data-availability="true">
        <a href="/camisa-101.html"><span class="name">Camisa 101</span></a>
        <div class="sale-price"><span>R$ 390,00</span></div>
        <img src="https://www.brooksfield.com.br/img101.jpg" />
      </li>
    </ul></body></html>
    """

    page2_html = """
    <html><body><ul>
      <li class="purchasable" data-product-id="102" data-name="Camisa 102" data-brand="Brooksfield" data-stock="3" data-availability="true">
        <a href="/camisa-102.html"><span class="name">Camisa 102</span></a>
        <div class="sale-price"><span>R$ 420,00</span></div>
        <img src="https://www.brooksfield.com.br/img102.jpg" />
      </li>
    </ul></body></html>
    """

    # Página 3 repete 102 (sem novos produtos)
    page3_html = page2_html

    with patch.object(engine, "_fetch_html", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.side_effect = [page1_html, page2_html, page3_html]

        collected = []
        async for item in engine.run_bulk_scrape("/adulto/camisas"):
            collected.append(item)

        assert len(collected) == 2
        pcode0 = collected[0]["product_code"] if isinstance(collected[0], dict) else collected[0].product_code
        pcode1 = collected[1]["product_code"] if isinstance(collected[1], dict) else collected[1].product_code
        assert pcode0 == "101"
        assert pcode1 == "102"
        # Deve ter chamado pg=1, pg=2, pg=3 e encerrado no 3 por repetição
        assert mock_fetch.call_count == 3
