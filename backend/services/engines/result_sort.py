"""Ordenação client-side dos resultados da Busca Comparativa.

Garante ordenação uniforme para TODAS as marcas no ponto único de fan-in
(`EngineFactory.search_all_brands`). Vários engines não implementam sort nativo
(Shopify/Wake/SFCC/Mercado Livre); este módulo assegura que, no mínimo, as
ordenações por preço — as únicas computáveis a partir do payload de busca —
afetem toda marca de forma consistente.

`relevance`/`recent`/`top_selling` dependem de score/data/vendas que a busca
não retorna: ficam a cargo do parâmetro nativo do engine (VTEX/Amazon/Netshoes),
e aqui apenas preservamos a ordem que o engine devolveu.
"""
from typing import Any, List, Optional, Tuple

from core.models import resolve_effective_price

# Opções computáveis client-side (a partir do preço presente em cada produto).
PRICE_SORTS = {"price_asc", "price_desc"}


def _effective_price(product: Any) -> Optional[float]:
    """Preço efetivo positivo do produto (desconto quando houver), ou None.

    Espelha a lógica de exibição do frontend (`getProductPriceView`/
    `toPositivePrice`): preço <= 0 é tratado como ausente para ordenarmos pelo
    mesmo valor que o usuário vê no card.
    """
    price = resolve_effective_price(
        getattr(product, "price_full", None),
        getattr(product, "price_discount", None),
        getattr(product, "price_discount_is_delta", False),
    )
    if price is None or price <= 0:
        return None
    return price


def sort_brand_products(products: List[Any], sort: Optional[str]) -> List[Any]:
    """Reordena os produtos de uma marca conforme a opção de ordenação.

    - ``price_asc``/``price_desc``: ordena pelo preço efetivo; produtos sem preço
      vão sempre ao final (nas duas direções).
    - demais opções (``relevance``/``recent``/``top_selling``/``None``/desconhecida):
      preserva a ordem retornada pelo engine (ordem nativa).
    """
    if not products or sort not in PRICE_SORTS:
        return products

    priced: List[Tuple[float, Any]] = []
    unpriced: List[Any] = []
    for product in products:
        price = _effective_price(product)
        if price is None:
            unpriced.append(product)
        else:
            priced.append((price, product))

    priced.sort(key=lambda item: item[0], reverse=(sort == "price_desc"))
    return [product for _, product in priced] + unpriced
