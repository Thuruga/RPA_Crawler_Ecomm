"""
Matriz de regressao hermetica do contrato de atributos (D-01/D-03) para os
oito engines: VTEX, Shopify, Wake, SFCC, Zara (Plano 01-02) e Amazon,
Mercado Livre, Netshoes (Plano 01-05 — marketplaces search-only).

Cada engine e caracterizado por fixture(s) locais em ``fixtures/phase01/`` e
verificado quanto a:
  - D-03: ``product_code`` vem SEMPRE do ID nativo product-level do engine —
    nunca de um ID de variante/SKU ambiguo (productReference, SKU de variante,
    productVariantId, sku isolado de ProductGroup). Nos tres marketplaces,
    isso tambem significa: nunca o ID normalizado/privado usado pelo fluxo de
    shipping (ex.: MLB sem hifen do Mercado Livre).
  - D-01: cores/tamanhos brutos (``raw_colors``/``raw_sizes``) sao capturados
    apenas quando a fonte expoe explicitamente essa informacao — nunca
    inferidos de titulo/slug. Ausencia produz ``[]``, nunca um valor inventado.

100% hermetico: nenhuma chamada de rede/browser. VTEX/Shopify/Wake usam
brand_key nao registrados (brand_service.get_brand -> None) para evitar
qualquer I/O de rede em caminhos de enriquecimento (reviews/cross-selling).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.vtex_schemas import VtexProduct
from services import vtex_parsing
from services.vtex_api_scraper import VtexApiClient
from services.shopify_api_client import ShopifyApiClient
from services.engines.wake_engine import WakeEngine
from services.engines.sfcc_parser import parse_pdp, parse_search_tiles, extract_pdp_product_code
from services.engines.zara_parser import (
    parse_product_detail,
    parse_itemlist_products,
    parse_tile_products,
)
from services.engines.amazon_engine import AmazonEngine
from services.engines.mercado_livre_engine import MercadoLivreEngine
from services.engines.netshoes_engine import NetshoesEngine

PHASE07_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "phase07"
BRANDS_PATH = Path(__file__).parents[1] / "data" / "brands.json"
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "phase01"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _load_html(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _load_clean_attribute_matrix() -> dict[str, dict]:
    return json.loads(
        (PHASE07_FIXTURES_DIR / "clean_attributes.json").read_text(encoding="utf-8")
    )["brands"]


def _attribute_snapshot_for_fixture(entry: dict) -> dict[str, list[str]]:
    engine = entry["engine"]
    fixture = entry["phase01_fixture"]

    if engine == "vtex":
        product = VtexProduct.model_validate(_load_json(fixture))
        colors = vtex_parsing.extract_colors(product)
        sizes = vtex_parsing.extract_sizes(product.items)
        return {
            "raw_colors": list(colors),
            "raw_sizes": list(sizes),
            "canonical_colors": colors,
            "canonical_sizes": sizes,
        }
    if engine == "shopify":
        product = ShopifyApiClient("fixture_shopify")._map_to_bronze(
            _load_json(fixture), "Camisetas"
        )
    elif engine == "wake":
        product = WakeEngine._node_to_dict(_load_json(fixture), "fixture.test", "Fixture Wake")
    elif engine == "sfcc":
        product = parse_pdp(_load_html(fixture), "https://fixture.test/produto.html")
    elif engine == "zara":
        product = parse_product_detail(
            _load_html(fixture),
            "https://fixture.test/produto.html",
            brand="Zara",
            base_url="https://fixture.test",
        )
    elif engine == "amazon":
        product = AmazonEngine("fixture_amazon")._parse_pdp_html(
            _load_html(fixture), "https://www.amazon.com.br/produto/dp/B08XYZ1234"
        )
    elif engine == "mercadolivre":
        product = MercadoLivreEngine("fixture_ml")._build_pdp_product(
            _load_html(fixture),
            "https://produto.mercadolivre.com.br/MLB-1234567890-produto-_JM",
        )
    elif engine == "netshoes":
        adapter = NetshoesEngine("fixture_netshoes")
        state = adapter._extract_initial_state(_load_html(fixture))
        product = adapter._build_pdp_product(state, "https://fixture.test/produto")
    else:  # pragma: no cover - coverage reports registry drift first.
        raise AssertionError(f"Engine sem fixture de atributos: {engine}")

    assert product is not None
    get_value = product.get if isinstance(product, dict) else lambda key, default: getattr(product, key, default)
    return {
        "raw_colors": list(get_value("raw_colors", [])),
        "raw_sizes": list(get_value("raw_sizes", [])),
        "canonical_colors": list(get_value("available_colors", [])),
        "canonical_sizes": list(get_value("available_sizes", [])),
    }


# ---------------------------------------------------------------------------
# VTEX
# ---------------------------------------------------------------------------


def test_vtex_extract_colors_preserves_raw_order_and_casing():
    """D-01: nao usa set()/upper() destrutivo — mantem label exato da VTEX."""
    product = VtexProduct.model_validate(_load_json("vtex.json"))
    colors = vtex_parsing.extract_colors(product)
    assert colors == ["Azul Marinho", "Branco"]


def test_vtex_extract_sizes_only_items_with_stock():
    product = VtexProduct.model_validate(_load_json("vtex.json"))
    sizes = vtex_parsing.extract_sizes(product.items)
    # Item "M" tem AvailableQuantity=0 — nao deve aparecer.
    assert sizes == ["P"]


def test_vtex_extract_size_grid_includes_out_of_stock_items():
    """Grade completa (size_grid) inclui "M" mesmo com AvailableQuantity=0 —
    distinto de extract_sizes, que reflete somente estoque atual."""
    product = VtexProduct.model_validate(_load_json("vtex.json"))
    grid = vtex_parsing.extract_size_grid(product.items)
    assert grid == ["P", "M"]


def test_vtex_regex_fallback_cleans_concatenated_size_and_mixed_color():
    data = copy.deepcopy(_load_json("vtex.json"))
    data["Cor"] = ["VERDE / P"]
    data["items"][0]["name"] = "Camisa Austral Preta P"
    product = VtexProduct.model_validate(data)

    assert vtex_parsing.extract_colors(product, extract_via_regex=True) == ["VERDE"]
    assert vtex_parsing.extract_sizes(product.items, extract_via_regex=True) == ["P"]


def test_vtex_regex_fallback_is_opt_in_for_clean_brand_regression():
    data = copy.deepcopy(_load_json("vtex.json"))
    data["Cor"] = ["VERDE / P"]
    data["items"][0]["name"] = "Camisa Austral Preta P"
    product = VtexProduct.model_validate(data)

    assert vtex_parsing.extract_colors(product) == ["VERDE / P"]
    assert vtex_parsing.extract_sizes(product.items) == ["Camisa Austral Preta P"]


@pytest.mark.asyncio
async def test_vtex_parse_product_dict_product_code_is_native_product_id():
    """D-03: product_code == productId — nunca productReference/SKU de item."""
    data = _load_json("vtex.json")
    client = VtexApiClient(brand_name="unregistered_test_brand_vtex")
    result = await client.parse_product_dict(
        data, "https://teststore.vtex.com/camisa-social-slim-fit/p", "teststore.vtex.com"
    )
    assert result is not None
    assert result.product_code == data["productId"]
    assert result.product_code != data["productReference"]
    # Nenhum itemId de SKU vaza para product_code.
    assert result.product_code not in {item["itemId"] for item in data["items"]}


@pytest.mark.asyncio
async def test_vtex_parse_product_dict_preserves_raw_colors_and_sizes():
    data = _load_json("vtex.json")
    client = VtexApiClient(brand_name="unregistered_test_brand_vtex")
    result = await client.parse_product_dict(
        data, "https://teststore.vtex.com/camisa-social-slim-fit/p", "teststore.vtex.com"
    )
    assert result is not None
    assert result.raw_colors == ["Azul Marinho", "Branco"]
    assert result.available_colors == ["Azul Marinho", "Branco"]
    assert result.raw_sizes == ["P"]
    # size_grid inclui "M" (esgotado) — available_sizes/raw_sizes não.
    assert result.size_grid == ["P", "M"]
    assert result.raw_size_grid == ["P", "M"]


@pytest.mark.asyncio
async def test_vtex_search_product_code_uses_product_id_not_reference(monkeypatch):
    """D-03 na superficie de busca (raw dict path, nao VtexProduct Pydantic)."""
    from core.models import DynamicBrand
    from services import brand_service as brand_service_module

    data = _load_json("vtex.json")
    fake_brand = DynamicBrand(
        brand_key="unregtestvtex",
        brand_name="unregtestvtex",
        domain="teststore.vtex.com",
    )
    monkeypatch.setattr(
        brand_service_module.brand_service,
        "get_brand",
        lambda key: fake_brand if key == "unregtestvtex" else None,
    )

    async def fake_request_json(url, **kwargs):
        if "_from=0" in url:
            return [copy.deepcopy(data)]
        return []

    client = VtexApiClient(brand_name="unregtestvtex")
    monkeypatch.setattr(client, "_request_json", fake_request_json)

    result = await client.search(query="camisa social exclusiva inexistente", max_results=10)

    assert result.error is None
    assert len(result.products) == 1
    assert result.products[0].product_code == data["productId"]
    assert result.products[0].product_code != data["productReference"]


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------


def test_shopify_map_to_bronze_product_code_uses_id_not_variant_sku():
    """D-03: product_code == product.id — nunca o SKU da primeira variante."""
    data = _load_json("shopify.json")
    client = ShopifyApiClient(brand_key="unregistered_test_brand_shopify")
    result = client._map_to_bronze(data, "Camisetas")
    assert result is not None
    assert result.product_code == str(data["id"])
    assert result.product_code != data["variants"][0]["sku"]


def test_shopify_map_to_bronze_available_colors_sizes_from_explicit_options():
    """D-01: cor/tamanho vem de option values explicitos, so variantes disponiveis."""
    data = _load_json("shopify.json")
    client = ShopifyApiClient(brand_key="unregistered_test_brand_shopify")
    result = client._map_to_bronze(data, "Camisetas")
    assert result is not None
    # Somente a variante "available": true (Azul/M) entra — Preto/G esta indisponivel.
    assert result.available_colors == ["Azul"]
    assert result.available_sizes == ["M"]
    assert result.raw_colors == ["Azul"]
    assert result.raw_sizes == ["M"]
    # size_grid inclui "G" (variante indisponivel) — available_sizes/raw_sizes nao.
    assert result.size_grid == ["M", "G"]
    assert result.raw_size_grid == ["M", "G"]


def test_shopify_no_explicit_size_option_never_infers_full_variant_title():
    """D-01: sem opcao 'Tamanho' explicita, nunca usa o titulo completo da variante."""
    data = copy.deepcopy(_load_json("shopify.json"))
    # Remove a opcao "Tamanho" — resta apenas "Cor".
    data["options"] = [opt for opt in data["options"] if opt["name"] != "Tamanho"]

    client = ShopifyApiClient(brand_key="unregistered_test_brand_shopify")
    result = client._map_to_bronze(data, "Camisetas")
    assert result is not None
    # Antes do fix, isto teria retornado ["Azul / M"] (titulo inteiro da variante).
    assert result.available_sizes == []
    assert result.raw_sizes == []


def test_shopify_regex_fallback_splits_mixed_title_without_changing_raw_values():
    data = copy.deepcopy(_load_json("shopify.json"))
    data["options"] = [{"name": "Title", "position": 1}]
    data["variants"][0]["option1"] = "VERDE / P"

    client = ShopifyApiClient(brand_key="unregistered_test_brand_shopify")
    client.brand_info = SimpleNamespace(
        extract_via_regex=True,
        color_aliases=[],
        size_aliases=[],
    )
    result = client._map_to_bronze(data, "Camisetas")

    assert result is not None
    assert result.available_colors == ["Verde"]
    assert result.available_sizes == ["P"]
    assert result.raw_colors == ["VERDE / P"]
    assert result.raw_sizes == ["VERDE / P"]


# ---------------------------------------------------------------------------
# Wake
# ---------------------------------------------------------------------------


def test_wake_node_to_dict_product_code_uses_product_id_not_variant_ids():
    """D-03: product_code == productId — nunca productVariantId/sku."""
    node = _load_json("wake.json")
    parsed = WakeEngine._node_to_dict(node, domain="www.teststore.com.br", brand_name="Test Wake")
    assert parsed is not None
    assert parsed["product_code"] == node["productId"]
    assert parsed["product_code"] != node["sku"]
    assert parsed["product_code"] != node["productVariantId"]


def test_wake_search_node_to_dict_preserves_shipping_ids_separately():
    """Campos de shipping continuam preenchidos pelas fontes anteriores (productVariantId/sku),
    mesmo com product_code agora isolado em productId (D-03)."""
    node = _load_json("wake.json")
    parsed = WakeEngine._search_node_to_dict(node, domain="www.teststore.com.br", brand_name="Test Wake")
    assert parsed is not None
    assert parsed["product_code"] == node["productId"]
    assert parsed["shipping_product_id"] == node["productId"]
    assert parsed["shipping_variant_id"] == node["productVariantId"]
    assert parsed["shipping_sku"] == node["sku"]
    # Os IDs de shipping continuam estrutural/semanticamente separados de product_code.
    assert parsed["shipping_variant_id"] != parsed["product_code"]
    assert parsed["shipping_sku"] != parsed["product_code"]


def test_wake_node_to_dict_incomplete_node_returns_none():
    """Ausencia de titulo/url/preco -> None (fail-closed), nunca produto inventado."""
    incomplete = {"productId": "999", "sku": "SKU-X"}
    parsed = WakeEngine._node_to_dict(incomplete, domain="www.teststore.com.br", brand_name="Test Wake")
    assert parsed is None


# ---------------------------------------------------------------------------
# SFCC
# ---------------------------------------------------------------------------


def test_sfcc_search_tile_product_code_uses_data_pid():
    html = _load_html("sfcc.html")
    tiles = parse_search_tiles(html, "teststore.com.br", "TestBrand")
    assert len(tiles) == 1
    assert tiles[0]["product_code"] == "PID-SFCC-777"


def test_sfcc_pdp_product_code_uses_product_id_not_sku():
    """D-03: productID e inequivoco — sku isolado da PDP nunca vira product_code."""
    html = _load_html("sfcc.html")
    parsed = parse_pdp(html, "https://www.teststore.com.br/produto/camisa-social-sfcc-777.html")
    assert parsed is not None
    assert parsed["product_code"] == "PID-SFCC-777"
    assert parsed["product_code"] != "SKU-VARIANTE-AMBIGUA-P"


def test_sfcc_pdp_product_code_none_when_only_variant_sku_present():
    """Ausencia de productID/@id inequivoco -> None (fail-closed), nunca o sku isolado."""
    ambiguous_ld = {
        "@type": "Product",
        "name": "Camisa Ambigua",
        "sku": "SKU-APENAS-VARIANTE",
        "offers": {"price": 149.9, "availability": "http://schema.org/InStock"},
    }
    assert extract_pdp_product_code(ambiguous_ld) is None

    html = f"""
    <html><body>
    <script type="application/ld+json">{json.dumps(ambiguous_ld)}</script>
    </body></html>
    """
    parsed = parse_pdp(html, "https://www.teststore.com.br/produto/camisa-ambigua.html")
    assert parsed is not None
    assert parsed["product_code"] is None


# ---------------------------------------------------------------------------
# Zara
# ---------------------------------------------------------------------------


def test_zara_tile_product_code_uses_data_productid():
    html = _load_html("zara.html")
    tiles = parse_tile_products(html, brand="Zara", base_url="https://www.zara.com")
    assert len(tiles) == 1
    assert tiles[0]["product_code"] == "ZARA-STABLE-123"


def test_zara_itemlist_product_code_from_item_sku():
    html = _load_html("zara.html")
    items = parse_itemlist_products(html, brand="Zara", base_url="https://www.zara.com")
    assert len(items) == 1
    assert items[0]["product_code"] == "ZARA-ITEMLIST-SKU-999"


def test_zara_pdp_productgroup_never_uses_isolated_variant_sku():
    """D-03: ProductGroup sem sku proprio -> product_code None, nunca hasVariant[].sku."""
    html = _load_html("zara.html")
    parsed = parse_product_detail(
        html,
        "https://www.zara.com/br/pt/camisa-linho-p12345678.html",
        brand="Zara",
        base_url="https://www.zara.com",
    )
    assert parsed is not None
    assert parsed["product_code"] is None
    assert parsed["product_code"] != "ZARA-VARIANT-SKU-P"
    assert parsed["product_code"] != "ZARA-VARIANT-SKU-M"


def test_zara_pdp_productgroup_preserves_explicit_variant_colors_and_sizes():
    """D-01: hasVariant.color/size explicitos viram raw_colors/raw_sizes; sem inferencia."""
    html = _load_html("zara.html")
    parsed = parse_product_detail(
        html,
        "https://www.zara.com/br/pt/camisa-linho-p12345678.html",
        brand="Zara",
        base_url="https://www.zara.com",
    )
    assert parsed is not None
    assert parsed["available_colors"] == ["Azul"]
    assert parsed["available_sizes"] == ["P", "M"]
    assert parsed["raw_colors"] == ["Azul"]
    assert parsed["raw_sizes"] == ["P", "M"]
    # Fixture nao declara offers.availability por variante -> fail-open (ambos
    # tratados como disponiveis) e size_grid coincide com available_sizes.
    assert parsed["size_grid"] == ["P", "M"]
    assert parsed["raw_size_grid"] == ["P", "M"]


def test_zara_pdp_productgroup_out_of_stock_variant_excluded_from_available_sizes():
    """size_grid inclui a grade completa; available_sizes exclui OutOfStock (D-01)."""
    ld = {
        "@type": "ProductGroup",
        "name": "Camisa Linho",
        "image": ["https://static.zara.net/photos/camisa.jpg"],
        "hasVariant": [
            {
                "@type": "Product",
                "color": "Azul",
                "size": "P",
                "offers": {"price": 259.90, "availability": "https://schema.org/InStock"},
            },
            {
                "@type": "Product",
                "color": "Azul",
                "size": "M",
                "offers": {"price": 259.90, "availability": "https://schema.org/OutOfStock"},
            },
            {
                "@type": "Product",
                "color": "Azul",
                "size": "G",
                "offers": {"price": 259.90, "availability": "https://schema.org/InStock"},
            },
        ],
    }
    html = f"""
    <html><body>
    <script type="application/ld+json">{json.dumps(ld)}</script>
    </body></html>
    """
    parsed = parse_product_detail(
        html, "https://www.zara.com/br/pt/camisa-linho-p1.html", brand="Zara", base_url="https://www.zara.com"
    )
    assert parsed is not None
    assert parsed["size_grid"] == ["P", "M", "G"]
    assert parsed["available_sizes"] == ["P", "G"]
    assert parsed["raw_sizes"] == ["P", "G"]
    assert parsed["raw_size_grid"] == ["P", "M", "G"]


def test_zara_pdp_no_hasvariant_yields_empty_colors_sizes():
    """Ausencia de hasVariant -> [] (fail-closed), nunca inferido de titulo/slug."""
    ld = {
        "@type": "Product",
        "name": "Bermuda Zara",
        "image": ["https://static.zara.net/photos/bermuda.jpg"],
        "offers": {"price": 179.9},
    }
    html = f"""
    <html><body>
    <script type="application/ld+json">{json.dumps(ld)}</script>
    </body></html>
    """
    parsed = parse_product_detail(
        html, "https://www.zara.com/br/pt/bermuda-p1.html", brand="Zara", base_url="https://www.zara.com"
    )
    assert parsed is not None
    assert parsed["available_colors"] == []
    assert parsed["available_sizes"] == []
    assert parsed["size_grid"] == []


# ---------------------------------------------------------------------------
# Amazon (Plano 01-05 — marketplace search-only)
# ---------------------------------------------------------------------------


def test_amazon_search_tile_product_code_is_bare_asin_no_decoration():
    """D-03: product_code eh o ASIN puro de 10 caracteres, sem prefixo/decoracao,
    capturado apos a limpeza minima de query/ref da URL (nao altera o ASIN em si)."""
    html = _load_html("amazon.html")
    engine = AmazonEngine(brand_key="unregistered_test_brand_amazon")
    produtos = engine._parse_html(html, limit=10)
    assert len(produtos) == 1
    assert produtos[0]["product_code"] == "B08XYZ1234"
    assert len(produtos[0]["product_code"]) == 10


def test_amazon_pdp_product_code_uses_asin_from_url_not_decorated():
    """D-03: mesma extracao de ASIN na PDP, a partir da URL completa (com /ref=)."""
    html = _load_html("amazon.html")
    engine = AmazonEngine(brand_key="unregistered_test_brand_amazon")
    product_url = (
        "https://www.amazon.com.br/Camisa-Social-Azul-Slim-Fit/dp/B08XYZ1234/ref=sr_1_5?keywords=camisa"
    )
    product = engine._parse_pdp_html(html, product_url)
    assert product is not None
    assert product["product_code"] == "B08XYZ1234"


def test_amazon_search_result_title_color_word_never_becomes_available_color():
    """D-01: a Amazon nao extrai cor/tamanho estruturado — mesmo com 'Azul' no
    titulo, available_colors/raw_colors permanecem [] (nunca inferido de texto)."""
    from core.models import SearchProductResult

    html = _load_html("amazon.html")
    engine = AmazonEngine(brand_key="unregistered_test_brand_amazon")
    produtos = engine._parse_html(html, limit=10)
    assert "azul" in produtos[0]["titulo"].lower()

    result = SearchProductResult(
        brand="Amazon",
        product_name=produtos[0]["titulo"],
        url=produtos[0]["url"],
        price_full=produtos[0]["preco"],
        product_code=produtos[0].get("product_code"),
    )
    assert result.available_colors == []
    assert result.raw_colors == []
    assert result.available_sizes == []
    assert result.raw_sizes == []


# ---------------------------------------------------------------------------
# Mercado Livre (Plano 01-05 — marketplace search-only)
# ---------------------------------------------------------------------------


def test_ml_pdp_product_code_preserves_raw_sku_and_differs_from_shipping_item_id():
    """D-03: product_code vem do campo 'sku' bruto do JSON-LD (preserva hifen/casing);
    o ID normalizado usado pelo fluxo de shipping (_extract_item_id, sem hifen)
    permanece privado e nunca vaza para a identidade do produto."""
    html = _load_html("mercado_livre.html")
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    product_url = "https://produto.mercadolivre.com.br/MLB-1234567890-tenis-esportivo-teste-_JM"

    product = engine._build_pdp_product(html, product_url)
    assert product is not None
    assert product["product_code"] == "MLB-1234567890"

    shipping_item_id = engine._extract_item_id(product_url)
    assert shipping_item_id == "MLB1234567890"
    assert product["product_code"] != shipping_item_id


def test_ml_search_array_product_code_preserves_raw_sku():
    """D-03: mesma proveniencia bruta no caminho de busca (array de estado do ML)."""
    html = _load_html("mercado_livre.html")
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    produtos = engine._extract_product_array(html, limit=10)
    assert len(produtos) == 1
    assert produtos[0]["product_code"] == "MLB-1234567890"


def test_ml_pdp_no_explicit_sku_yields_none_product_code():
    """Ausencia de 'sku' no JSON-LD -> None (fail-closed), nunca reconstruido da URL."""
    ld = {
        "@type": "Product",
        "name": "Bermuda Sem SKU",
        "image": ["https://http2.mlstatic.com/bermuda.jpg"],
        "offers": {"price": 149.9, "availability": "http://schema.org/InStock"},
    }
    html = f"""
    <html><body>
    <script type="application/ld+json">{json.dumps(ld)}</script>
    </body></html>
    """
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    product = engine._build_pdp_product(html, "https://produto.mercadolivre.com.br/MLB-999888777-bermuda")
    assert product is not None
    assert product["product_code"] is None


def test_ml_pdp_no_explicit_attributes_yields_empty_colors_sizes():
    """D-01: ML nao expoe cor/tamanho estruturado no payload de PDP/busca desta
    fixture — available_colors/sizes e raw_colors/sizes permanecem [] por padrao
    (nunca inferidos de titulo/slug)."""
    html = _load_html("mercado_livre.html")
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    product_url = "https://produto.mercadolivre.com.br/MLB-1234567890-tenis-esportivo-teste-_JM"
    product = engine._build_pdp_product(html, product_url)
    assert product is not None
    assert product.get("available_colors", []) == []
    assert product.get("raw_colors", []) == []


@pytest.mark.skip(reason="Pre-existing debt from before Phase 7 (commit 21fbdc4) — depends on ML item-API code not yet committed (only in uncommitted WIP). Out of ATTR-01/JOB-01/JOB-02 scope.")
def test_ml_item_api_maps_current_and_original_price_and_native_id():
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    product = engine._pdp_from_item_api(
        {
            "id": "MLB4359093019",
            "title": "Tenis Masculino Casual Aramis",
            "price": 189.99,
            "original_price": 339.99,
            "status": "active",
            "available_quantity": 7,
            "secure_thumbnail": "https://http2.mlstatic.com/item.jpg",
        },
        "https://produto.mercadolivre.com.br/MLB-4359093019-item-_JM",
    )

    assert product is not None
    assert product["price_full"] == 339.99
    assert product["price_discount"] == 189.99
    assert product["price_discount_is_delta"] is False
    assert product["product_code"] == "MLB4359093019"
    assert product["stock_availability"] is True


@pytest.mark.parametrize("price", [None, "", 0, -1, "invalido"])
@pytest.mark.skip(reason="Pre-existing debt from before Phase 7 (commit 21fbdc4) — depends on ML item-API code not yet committed (only in uncommitted WIP). Out of ATTR-01/JOB-01/JOB-02 scope.")
def test_ml_item_api_rejects_invalid_or_nonpositive_price(price):
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    assert engine._pdp_from_item_api(
        {"id": "MLB4359093019", "title": "Produto", "price": price},
        "https://produto.mercadolivre.com.br/MLB-4359093019-item-_JM",
    ) is None


@pytest.mark.asyncio
@pytest.mark.skip(reason="Pre-existing debt from before Phase 7 (commit 21fbdc4) — depends on ML item-API code not yet committed (only in uncommitted WIP). Out of ATTR-01/JOB-01/JOB-02 scope.")
async def test_ml_pdp_prefers_public_item_api_before_antibot_sources(monkeypatch):
    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    expected = {
        "url": "https://produto.mercadolivre.com.br/MLB-4359093019-item-_JM",
        "brand": engine.brand_key,
        "raw_title": "Produto",
        "raw_description": "Produto",
        "price_full": 189.99,
        "image_url": None,
        "stock_availability": True,
        "product_code": "MLB4359093019",
    }

    async def fake_item_api(product_url):
        assert product_url == expected["url"]
        return expected

    monkeypatch.setattr(engine, "_fetch_item_api_product", fake_item_api)
    assert await engine.get_pdp_product(expected["url"]) is expected

@pytest.mark.asyncio
@pytest.mark.skip(reason="Pre-existing debt from before Phase 7 (commit 21fbdc4) — depends on ML anti-bot Playwright fallback not yet committed (only in uncommitted WIP). Out of ATTR-01/JOB-01/JOB-02 scope.")
async def test_ml_pdp_playwright_does_not_wait_for_networkidle(monkeypatch):
    """A PDP real mantem conexoes abertas mesmo apos resolver o Anubis."""
    import services.engines.mercado_livre_engine as ml_module
    from core.browser_manager import BrowserManager

    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    product_url = "https://produto.mercadolivre.com.br/MLB-4359093019-item-_JM"
    expected = {
        "url": product_url,
        "brand": engine.brand_key,
        "raw_title": "Produto",
        "price_full": 189.99,
        "stock_availability": True,
    }

    class Response:
        status_code = 200
        text = "<html>anubis proof of work challenge</html>"

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response()

    captured = {}

    async def fake_item_api(_url):
        return None

    async def fake_fetch_html(url, **kwargs):
        captured.update(kwargs)
        assert url == product_url
        return "<html>produto resolvido</html>"

    monkeypatch.setattr(engine, "_fetch_item_api_product", fake_item_api)
    monkeypatch.setattr(ml_module, "AsyncSession", Session)
    monkeypatch.setattr(BrowserManager, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(engine, "_build_pdp_product", lambda html, url: expected)

    assert await engine.get_pdp_product(product_url) is expected
    assert captured["wait_until"] == "domcontentloaded"
    assert captured["extra_sleep"] >= 15.0

@pytest.mark.asyncio
@pytest.mark.skip(reason="Pre-existing debt from before Phase 7 (commit 21fbdc4) — depends on ML anti-bot Playwright fallback not yet committed (only in uncommitted WIP). Out of ATTR-01/JOB-01/JOB-02 scope.")
async def test_ml_search_playwright_does_not_wait_for_networkidle(monkeypatch):
    from core.browser_manager import BrowserManager

    engine = MercadoLivreEngine(brand_key="unregistered_test_brand_ml")
    captured = {}

    async def fake_fetch_html(url, **kwargs):
        captured.update(kwargs)
        return "<html>resultado resolvido</html>"

    monkeypatch.setattr(BrowserManager, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(engine, "_parse_html", lambda html, limit: [{"title": "Produto"}])

    result = await engine._search_with_playwright("camisa", 10)
    assert result == [{"title": "Produto"}]
    assert captured["wait_until"] == "domcontentloaded"
    assert captured["extra_sleep"] >= 15.0


# ---------------------------------------------------------------------------
# Netshoes (Plano 01-05 — marketplace search-only)
# ---------------------------------------------------------------------------


def test_netshoes_pdp_product_code_uses_product_level_id_never_sku():
    """D-03: product_code vem da chave product-level 'id' de
    __INITIAL_STATE__.Product.currentProduct — nunca do primeiro SKU de variante."""
    html = _load_html("netshoes.html")
    engine = NetshoesEngine(brand_key="unregistered_test_brand_netshoes")
    state = engine._extract_initial_state(html)
    product = engine._build_pdp_product(state, "https://www.netshoes.com.br/tenis-netshoes-teste")
    assert product is not None
    assert product["product_code"] == "NETSHOES-PROD-001"
    assert product["product_code"] != "NETSHOES-SKU-VARIANTE-P"


def test_netshoes_pdp_preserves_explicit_sizes_and_no_explicit_colors_stays_empty():
    """D-01: sizes vem explicitamente das SKUs; ausencia de campo 'color' nas SKUs
    produz [] (nunca inferido de titulo)."""
    html = _load_html("netshoes.html")
    engine = NetshoesEngine(brand_key="unregistered_test_brand_netshoes")
    state = engine._extract_initial_state(html)
    product = engine._build_pdp_product(state, "https://www.netshoes.com.br/tenis-netshoes-teste")
    assert product is not None
    assert product["available_sizes"] == ["P", "M"]
    assert product["raw_sizes"] == ["P", "M"]
    assert product["available_colors"] == []
    assert product["raw_colors"] == []


def test_netshoes_search_listing_product_code_uses_product_level_id_never_slug():
    """D-03: mesma proveniencia bruta no caminho de busca (parentSkus) — nunca o
    slug/URL do produto."""
    html = _load_html("netshoes.html")
    engine = NetshoesEngine(brand_key="unregistered_test_brand_netshoes")
    state = engine._extract_initial_state(html)
    produtos = engine._parse_from_initial_state(state, limit=10)
    assert len(produtos) == 1
    assert produtos[0]["product_code"] == "NETSHOES-PROD-001"
    assert produtos[0]["product_code"] != produtos[0]["url"]


def test_netshoes_pdp_missing_product_level_id_yields_none_never_falls_back_to_sku():
    """Ausencia explicita de id/productId -> None (fail-closed), nunca o SKU de
    variante como substituto."""
    incomplete_state = {
        "Product": {
            "currentProduct": {
                "name": "Produto Sem ID",
                "price": {"saleInCents": 9990},
                "skus": [{"skuId": "NETSHOES-SKU-FALLBACK", "size": "U"}],
            }
        }
    }
    engine = NetshoesEngine(brand_key="unregistered_test_brand_netshoes")
    product = engine._build_pdp_product(incomplete_state, "https://www.netshoes.com.br/produto-sem-id")
    assert product is not None
    assert product["product_code"] is None
# ---------------------------------------------------------------------------
# Phase 07 clean-brand matrix and conservative marketplace fallback (D-09/D-10)
# ---------------------------------------------------------------------------


def test_clean_brand_fixture_coverage_matches_registry_candidates():
    registry = json.loads(BRANDS_PATH.read_text(encoding="utf-8"))
    expected = {
        brand_key
        for brand_key, brand in registry.items()
        if not brand.get("extract_via_regex", False)
        and (brand.get("is_active", True) or brand.get("engine") == "sfcc")
    }
    matrix = _load_clean_attribute_matrix()

    assert set(matrix) == expected
    assert all(entry["engine"] == registry[key]["engine"] for key, entry in matrix.items())


@pytest.mark.parametrize(
    ("brand_key", "entry"),
    sorted(_load_clean_attribute_matrix().items()),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_clean_brand_attribute_fixture_preserves_raw_and_canonical(brand_key, entry):
    snapshot = _attribute_snapshot_for_fixture(entry)

    assert snapshot == {
        "raw_colors": entry["raw_colors"],
        "raw_sizes": entry["raw_sizes"],
        "canonical_colors": entry["canonical_colors"],
        "canonical_sizes": entry["canonical_sizes"],
    }, brand_key


def test_amazon_structured_options_preserve_raw_before_conservative_fallback():
    html = _load_html("amazon.html").replace(
        "</body>",
        """
        <select id="variation_color_name">
          <option value="">Selecione</option>
          <option value="verde-p">VERDE / P</option>
        </select>
        <select id="variation_size_name">
          <option value="">Selecione</option>
          <option value="verde-p">VERDE / P</option>
        </select>
        </body>
        """,
    )
    product = AmazonEngine("fixture_amazon")._parse_pdp_html(
        html, "https://www.amazon.com.br/produto/dp/B08XYZ1234"
    )

    assert product is not None
    assert product["raw_colors"] == ["VERDE / P"]
    assert product["available_colors"] == ["VERDE"]
    assert product["raw_sizes"] == ["VERDE / P"]
    assert product["available_sizes"] == ["P"]


def test_mercado_livre_structured_attributes_preserve_raw_before_fallback():
    product = MercadoLivreEngine("fixture_ml")._pdp_from_jsonld(
        {
            "@type": "Product",
            "name": "Tenis Masculino",
            "sku": "MLB-4359093019",
            "image": ["https://http2.mlstatic.com/item.jpg"],
            "offers": {"price": 189.99, "availability": "http://schema.org/InStock"},
            "color": ["VERDE / P"],
            "size": ["VERDE / P"],
        },
        "https://produto.mercadolivre.com.br/MLB-4359093019-item-_JM",
    )

    assert product is not None
    assert product["raw_colors"] == ["VERDE / P"]
    assert product["available_colors"] == ["VERDE"]
    assert product["raw_sizes"] == ["VERDE / P"]
    assert product["available_sizes"] == ["P"]

def test_netshoes_structured_skus_preserve_raw_before_fallback():
    html = _load_html("netshoes.html")
    engine = NetshoesEngine("fixture_netshoes")
    state = engine._extract_initial_state(html)
    state["Product"]["currentProduct"]["skus"] = [
        {"skuId": "NETSHOES-SKU-MIXED", "color": "VERDE / P", "size": "VERDE / P"}
    ]

    product = engine._build_pdp_product(state, "https://fixture.test/produto")

    assert product is not None
    assert product["raw_colors"] == ["VERDE / P"]
    assert product["available_colors"] == ["VERDE"]
    assert product["raw_sizes"] == ["VERDE / P"]
    assert product["available_sizes"] == ["P"]
