"""Testes da ordenação client-side garantida para todas as marcas (Busca Comparativa).

Cobre `services.engines.result_sort.sort_brand_products`, aplicada no ponto único
de fan-in `EngineFactory.search_all_brands` para que TODAS as marcas reordenem por
preço — inclusive engines que não implementam sort nativo.
"""
from core.models import SearchProductResult
from services.engines.result_sort import sort_brand_products


def _p(name, full=None, discount=None, is_delta=False):
    return SearchProductResult(
        brand="b",
        product_name=name,
        url=f"http://x/{name}",
        price_full=full,
        price_discount=discount,
        price_discount_is_delta=is_delta,
    )


def _names(products):
    return [p.product_name for p in products]


def test_price_asc_orders_by_effective_price():
    out = sort_brand_products([_p("c", 300.0), _p("a", 100.0), _p("b", 200.0)], "price_asc")
    assert _names(out) == ["a", "b", "c"]


def test_price_desc_orders_by_effective_price():
    out = sort_brand_products([_p("a", 100.0), _p("c", 300.0), _p("b", 200.0)], "price_desc")
    assert _names(out) == ["c", "b", "a"]


def test_uses_discount_price_when_present():
    # 'a' custa 90 (com desconto) < 'b' 100 -> 'a' primeiro no asc.
    out = sort_brand_products([_p("b", 100.0), _p("a", 500.0, 90.0)], "price_asc")
    assert _names(out) == ["a", "b"]


def test_delta_discount_uses_full_as_effective():
    # is_delta=True: o preço efetivo é price_full (o discount é o valor abatido,
    # não o preço final). 'a' efetivo=100, 'b' efetivo=80 -> 'b' primeiro.
    out = sort_brand_products(
        [_p("a", 100.0, 40.0, is_delta=True), _p("b", 80.0)], "price_asc"
    )
    assert _names(out) == ["b", "a"]


def test_missing_price_sorts_last_in_both_directions():
    products = [_p("np"), _p("a", 100.0), _p("b", 200.0)]
    assert _names(sort_brand_products(products, "price_asc")) == ["a", "b", "np"]
    assert _names(sort_brand_products(products, "price_desc")) == ["b", "a", "np"]


def test_non_positive_price_treated_as_missing():
    # Preço <= 0 é tratado como ausente (espelha toPositivePrice do frontend).
    products = [_p("zero", 0.0), _p("a", 50.0)]
    assert _names(sort_brand_products(products, "price_asc")) == ["a", "zero"]


def test_relevance_and_others_preserve_native_order():
    products = [_p("c", 300.0), _p("a", 100.0), _p("b", 200.0)]
    for option in ("relevance", "recent", "top_selling", None, "desconhecida"):
        out = sort_brand_products(products, option)
        assert _names(out) == ["c", "a", "b"], f"opção {option!r} não deveria reordenar"


def test_empty_list_is_noop():
    assert sort_brand_products([], "price_asc") == []
