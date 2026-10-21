"""
Lógica pura do motor de relevância cross-marketplace.

Funções determinísticas (sem rede, sem IA, sem estado) extraídas de
``CrossMarketplaceService.compare_product``. Decidem o score final de um
candidato, o ponto de corte, a normalização dos resultados dos engines, a
deduplicação e o vencedor da buybox.

Mantidas puras de propósito: permitem teste unitário da regra de negócio de
relevância sem depender de rede, Playwright ou modelos de IA.
"""
import logging
from collections import defaultdict
from typing import Any, Dict, List

from config import relevance_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Decision gates — limiares nomeados (sem números mágicos espalhados no fluxo)
# ---------------------------------------------------------------------------
STRONG_TEXT_SCORE = 90.0       # título bate as model-words exatas -> texto domina
HIGH_TEXT_SCORE = 85.0
HIGH_IMAGE_SCORE = 85.0
MED_IMAGE_FLOOR = 45.0
MED_TEXT_FLOOR = 40.0
TEXT_ONLY_RESCUE_FLOOR = 80.0  # gate anti-WAF: imagem indisponível mas texto forte


def compute_final_match_score(
    text_score: float,
    image_score: float,
    text_weight: float = None,
    image_weight: float = None,
) -> float:
    """
    Combina o score de texto (NLP) e o de imagem (CLIP) no score final.

    Replica a régua de decisão original: texto muito forte domina, imagem forte
    com texto razoável também aprova, e há um gate anti-WAF que salva o produto
    pela nota de texto quando o download da imagem falha (image_score == 0).
    Sem nenhum desses casos, cai na média ponderada configurável via .env.
    """
    if text_weight is None:
        text_weight = relevance_settings.FINAL_TEXT_WEIGHT
    if image_weight is None:
        image_weight = relevance_settings.FINAL_IMAGE_WEIGHT

    t, i = text_score, image_score

    if t >= STRONG_TEXT_SCORE:
        return max(t, i)
    if i >= HIGH_IMAGE_SCORE and t >= MED_TEXT_FLOOR:
        return max(i, t)
    if t >= HIGH_TEXT_SCORE and i >= MED_IMAGE_FLOOR:
        return max(i, t)
    if i == 0.0 and t >= TEXT_ONLY_RESCUE_FLOOR:
        return t
    return t * text_weight + i * image_weight


def compute_min_score_cutoff(requested_min: float, has_vision: bool) -> float:
    """
    Ponto de corte efetivo: o maior entre o mínimo pedido e o piso configurado.

    O piso é mais alto quando a IA visual está ativa (há imagem de referência),
    pois temos mais sinal para exigir maior confiança.
    """
    base_min = (
        relevance_settings.CROSS_MIN_SCORE_WITH_VISION
        if has_vision
        else relevance_settings.CROSS_MIN_SCORE_WITHOUT_VISION
    )
    return max(requested_min, base_min)


def normalize_engine_products(brand_result: Any, fallback_platform: str) -> List[Dict[str, Any]]:
    """
    Converte um ``BrandSearchResult`` (com ``.products``) na lista de dicts
    padronizados que o pipeline cross-marketplace consome.
    """
    dict_prods: List[Dict[str, Any]] = []
    for p in brand_result.products:
        dict_prods.append(
            {
                "plataforma": p.brand or fallback_platform,
                "titulo": p.product_name,
                "preco": getattr(p, "price_full", 0.0)
                or getattr(p, "price_discounted", 0.0)
                or 0.0,
                "url": p.url,
                "imagem": p.image_url,
                "seller": getattr(p, "seller", None) or "N/A",
                "rating": getattr(p, "rating", None),
                "review_count": getattr(p, "review_count", None),
                "reviews_state": getattr(p, "reviews_state", None),
                "review_product_id": getattr(p, "review_product_id", None),
                "scan_product_id": getattr(p, "scan_product_id", None),
            }
        )
    return dict_prods


def dedup_results(formatted_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Colapsa produtos com a mesma (marketplace, url). Mantém o primeiro exemplar
    e incrementa ``variant_count`` para os duplicados. Produtos sem URL nunca
    são deduplicados entre si.
    """
    seen_url: Dict[tuple, int] = {}
    dedup: List[Dict[str, Any]] = []

    for r in formatted_results:
        url_key = (r["marketplace"], r["url"])
        existing_idx = seen_url.get(url_key) if url_key[1] else None

        if existing_idx is None:
            idx = len(dedup)
            if r["url"]:
                seen_url[url_key] = idx
            dedup.append(r)
        else:
            dedup[existing_idx]["variant_count"] += 1

    return dedup


def mark_buybox_winner(formatted_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Marca como vencedor da buybox o produto de menor preço (in-place).

    Considera apenas matches ESTRITOS (``is_similar`` falsy) quando houver — um produto
    "similar" de baixo score (fallback per-plataforma) não deve ser anunciado como o melhor
    preço do item buscado. Se todos forem similares (ou nenhum carregar a flag ``is_similar``,
    p.ex. chamadores antigos/testes), considera todos — preserva o comportamento anterior.
    """
    if not formatted_results:
        return formatted_results
    candidatos = [r for r in formatted_results if not r.get("is_similar")] or formatted_results
    vencedor = min(candidatos, key=lambda r: r["price"])
    vencedor["is_buybox_winner"] = True
    return formatted_results


# ---------------------------------------------------------------------------
# Shaping de candidatos (extraído de compare_product — Workstream 1)
# ---------------------------------------------------------------------------
def select_top_candidates_per_platform(
    produtos: List[Dict[str, Any]],
    max_per_engine: int = None,
) -> List[Dict[str, Any]]:
    """
    Agrupa os produtos por plataforma, ordena cada grupo por ``-text_match_score``
    e retorna os top-N (``max_per_engine``) de cada plataforma concatenados.

    Esses são os candidatos que seguem para validação visual. Mantém a semântica
    original (a ordenação por texto define quais entram no orçamento de visão).
    ``max_per_engine`` default = relevance_settings.CROSS_MAX_RESULTS_PER_ENGINE.
    """
    if max_per_engine is None:
        max_per_engine = relevance_settings.CROSS_MAX_RESULTS_PER_ENGINE

    por_plataforma: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in produtos:
        por_plataforma[p["plataforma"]].append(p)

    top: List[Dict[str, Any]] = []
    for prods in por_plataforma.values():
        prods.sort(key=lambda x: -x["text_match_score"])
        top.extend(prods[:max_per_engine])
    return top


def apply_similar_fallback(
    produtos_filtrados: List[Dict[str, Any]],
    todos_produtos: List[Dict[str, Any]],
    enabled: bool = None,
    min_score: float = None,
) -> tuple:
    """
    Fallback "produtos similares" por-plataforma (S1).

    Para cada plataforma que retornou produtos brutos mas NÃO teve nenhum match
    estrito (não está em ``produtos_filtrados``), recupera os produtos daquela
    plataforma com ``final_match_score >= min_score`` e ``preco > 0``, marca-os
    com ``_is_similar = True`` e os anexa a ``produtos_filtrados`` (in-place).

    É per-plataforma de propósito: um fallback global não dispararia quando OUTRA
    plataforma tem match estrito. Plataformas com match estrito mantêm a precisão.

    ``enabled`` default = CROSS_SIMILAR_FALLBACK_ENABLED; ``min_score`` default =
    CROSS_SIMILAR_MIN_SCORE (passáveis explicitamente para teste).

    Returns:
        (produtos_filtrados, used_similar_fallback)
    """
    if enabled is None:
        enabled = relevance_settings.CROSS_SIMILAR_FALLBACK_ENABLED
    if min_score is None:
        min_score = relevance_settings.CROSS_SIMILAR_MIN_SCORE

    used = False
    if not enabled:
        return produtos_filtrados, used

    plats_com_estrito = {p.get("plataforma") for p in produtos_filtrados}
    todas_plataformas = {p.get("plataforma") for p in todos_produtos}
    for plat in todas_plataformas - plats_com_estrito:
        similares = [
            p
            for p in todos_produtos
            if p.get("plataforma") == plat
            and p.get("final_match_score", 0) >= min_score
            and p.get("preco", 0) > 0
        ]
        if similares:
            for s in similares:
                s["_is_similar"] = True  # não compete no buybox/cheapest; vira badge no front
            produtos_filtrados.extend(similares)
            used = True
            logger.info(
                f"{plat}: 0 match estrito; exibindo {len(similares)} produtos similares "
                f"(fallback per-plataforma, corte={min_score})."
            )

    return produtos_filtrados, used


def cap_results_per_platform(
    produtos: List[Dict[str, Any]],
    max_per_platform: int = None,
) -> List[Dict[str, Any]]:
    """
    Limita o número de produtos por plataforma no resultado final, preservando a
    ordem de entrada. ``max_per_platform`` default = CROSS_MAX_RESULTS_PER_PLATFORM_FINAL.
    """
    if max_per_platform is None:
        max_per_platform = relevance_settings.CROSS_MAX_RESULTS_PER_PLATFORM_FINAL

    contador: Dict[str, int] = defaultdict(int)
    capped: List[Dict[str, Any]] = []
    for p in produtos:
        plat = p["plataforma"]
        if contador[plat] >= max_per_platform:
            continue
        contador[plat] += 1
        capped.append(p)
    return capped


def build_formatted_results(produtos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Constrói a lista de dicts de saída (contrato do frontend) a partir dos produtos
    enriquecidos. Função pura — não lê config nem faz I/O. ``landed_price`` = preço +
    frete quando há ``shipping_price``, senão apenas o preço.
    """
    formatted: List[Dict[str, Any]] = []
    for p in produtos:
        price = p["preco"]
        formatted.append(
            {
                "marketplace": p["plataforma"],
                "seller": p.get("seller") or "N/A",
                "title": p["titulo"],
                "price": price,
                "url": p["url"],
                "image_url": p.get("imagem"),
                "text_match_score": round(p.get("text_match_score", 0), 1),
                "image_match_score": round(p.get("image_match_score", 0), 1),
                "final_match_score": round(p.get("final_match_score", 0), 1),
                "match_score": round(
                    p.get("final_match_score", 0), 1
                ),  # Mantido por compatibilidade
                # Produto trazido pelo fallback per-plataforma (similar, não match exato).
                "is_similar": bool(p.get("_is_similar", False)),
                "is_buybox_winner": False,
                "variant_count": 1,
                "is_free_shipping": p.get("is_free_shipping", False),
                "rating": p.get("rating"),
                "review_count": p.get("review_count"),
                "reviews_state": p.get("reviews_state"),
                "review_product_id": p.get("review_product_id"),
                "scan_product_id": p.get("scan_product_id"),
                "shipping_price": p.get("shipping_price"),
                "landed_price": price + p.get("shipping_price")
                if p.get("shipping_price") is not None
                else price,
                # Delivery-time (prazo) + blocked-state surfacing (FRET-08/FRET-09,
                # Phase 42 Plan 03) — only set when present upstream; a missing key
                # degrades gracefully (no fake value substituted).
                **({"estimated_delivery_days": p["estimated_delivery_days"]} if "estimated_delivery_days" in p else {}),
                **({"shipping_raw_text": p["shipping_raw_text"]} if "shipping_raw_text" in p else {}),
                **({"_shipping_state": p["_shipping_state"]} if "_shipping_state" in p else {}),
            }
        )
    return formatted
