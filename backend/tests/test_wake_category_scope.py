from services.engines.wake_engine import WakeEngine
from services.wake_category_paths import canonicalize_wake_category_source
import pytest


def _wake_node(*, gender: str, category: str = "Calças") -> dict:
    return {
        "productName": "Calça Reta Vintage Denim Lagosta Branco",
        "aliasComplete": "produto/calca-reta-vintage-denim-lagosta-branco-198856",
        "productId": 198856,
        "prices": {"price": 499.0, "listPrice": 499.0},
        "images": [{"url": "https://cdn.example.test/calca.jpg"}],
        "available": True,
        "attributes": [
            {"name": "Genero", "value": gender},
            {"name": "Categoria", "value": category},
            {"name": "Cor", "value": "Branco"},
            {"name": "Tamanho", "value": "36"},
        ],
        "informations": [],
    }


def test_richards_legacy_paths_resolve_to_official_mens_collection():
    assert canonicalize_wake_category_source("richards", "/calcas") == "/masculino/calcas"
    assert canonicalize_wake_category_source("richards", "outlet") == "/sale/masculino"
    assert canonicalize_wake_category_source("richards", "t-shirts") == "/masculino/t-shirts"
    assert (
        canonicalize_wake_category_source("richards", "tricos-e-blusas")
        == "/masculino/tricots-e-blusas"
    )
    assert (
        canonicalize_wake_category_source(
            "richards", "https://www.richards.com.br/tricot?sort=price"
        )
        == "https://www.richards.com.br/masculino/tricots-e-blusas?sort=price"
    )
    assert canonicalize_wake_category_source("oriba", "/calcas") == "/calcas"


def test_wake_category_node_preserves_gender_for_quality_gate():
    engine = WakeEngine("richards")
    parsed = engine._node_to_dict(
        _wake_node(gender="FEMININO"), "www.richards.com.br", "Richards"
    )

    assert parsed is not None
    assert parsed["gender"] == "FEMININO"
    assert parsed["category"] == "Calças"
    assert engine.filter_mens_fashion([parsed]) == []


def test_wake_category_node_keeps_mens_product_after_gender_filter():
    engine = WakeEngine("richards")
    parsed = engine._node_to_dict(
        _wake_node(gender="MASCULINO"), "www.richards.com.br", "Richards"
    )

    assert parsed is not None
    assert engine.filter_mens_fashion([parsed]) == [parsed]


def test_mens_flex_pants_are_not_mistaken_for_automotive_products():
    product = {
        "raw_title": "Calça Interlock Knitted Flex Cinza Claro",
        "url": "https://www.richards.com.br/produto/calca-interlock-knitted-flex-184735",
        "gender": "MASCULINO",
        "product_code": "184735",
    }

    assert WakeEngine("richards").filter_mens_fashion([product]) == [product]


@pytest.mark.asyncio
async def test_wake_category_scan_does_not_emit_partial_50_item_search_fallback(monkeypatch):
    """A non-hotsite path is not an authoritative category result."""
    engine = WakeEngine("richards")
    monkeypatch.setattr(engine, "_resolve_token", lambda *_args: _async_value("token"))
    monkeypatch.setattr(engine, "_resolve_domain_and_name", lambda _brand: ("www.richards.com.br", "Richards"))
    monkeypatch.setattr(engine, "_fetch_hotsite_products", lambda *_args: _async_value(None))

    logs = []
    products = [
        product async for product in engine.run_bulk_scrape(
            "https://www.richards.com.br/masculino/nao-existe",
            log_callback=logs.append,
        )
    ]

    assert products == []
    assert any(
        isinstance(entry, dict) and entry.get("type") == "error_done"
        and "busca textual" in entry.get("message", "")
        for entry in logs
    )


@pytest.mark.asyncio
async def test_shared_http_session_ignores_environment_proxy(monkeypatch):
    """A dead developer proxy must not make a valid Wake category look absent."""
    from curl_cffi import CurlOpt
    from core.session_manager import SessionManager

    await SessionManager.close_session()
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    session = await SessionManager.get_session()
    try:
        assert session.trust_env is False
        # curl_cffi 0.15 still lets libcurl read proxy environment variables
        # despite trust_env=False; the explicit option is the effective guard.
        assert session.curl_options[CurlOpt.PROXY] == ""
    finally:
        await SessionManager.close_session()


async def _async_value(value):
    return value
