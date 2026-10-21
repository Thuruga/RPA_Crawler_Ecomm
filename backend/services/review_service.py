"""
Serviço de Avaliações de Produtos — Multi-Provider.

Suporta os seguintes provedores de avaliação:
  - Trustvox (Aramis): via API pública do widget (trustvox.com.br/widget/root)
  - YourViews / VTEX Native (Reserva): via REST /reviews-and-ratings/api/rating/
  - None (Tommy): sem avaliações disponíveis

Estratégia de performance:
  - Trustvox: chamada individual por produto ao /widget/root (retorna rating + count)
  - VTEX Native: chamada individual por produto ao REST endpoint
  - Ambos disparam em paralelo via asyncio.gather para minimizar latência
"""

import asyncio
import hashlib
import logging
import json
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

import aiohttp

from config import settings
from core.models import ReviewComment, ReviewCommentsResult, ReviewState
from services.brand_service import brand_service

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

logger = logging.getLogger("ReviewService")

_REVIEW_TIMEOUT = aiohttp.ClientTimeout(total=8)
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(".", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def review_comment_key(item: ReviewComment | dict[str, Any]) -> str:
    """Stable key for provider comments, using provider ID or compact fields."""
    if isinstance(item, ReviewComment):
        if item.review_id:
            return str(item.review_id)
        raw = "|".join(
            str(value or "")
            for value in (
                item.rating,
                item.title,
                item.text,
                item.author,
                item.created_at,
            )
        )
    else:
        stable_id = _first_value(item, ("review_id", "reviewId", "id", "code"))
        if stable_id:
            return str(stable_id)
        raw = "|".join(
            str(item.get(key) or "")
            for key in ("rating", "title", "text", "author", "created_at")
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def dedupe_review_comments(comments: List[ReviewComment]) -> List[ReviewComment]:
    seen: set[str] = set()
    deduped: List[ReviewComment] = []
    for comment in comments:
        key = review_comment_key(comment)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(comment)
    return deduped


def _is_review_like(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    review_keys = {
        "review_id",
        "reviewId",
        "id",
        "rating",
        "rate",
        "score",
        "stars",
        "reviewRating",
        "title",
        "text",
        "comment",
        "review",
        "content",
        "message",
        "liked",
    }
    return any(key in item for key in review_keys)


def _extract_comment_entries(data: Any) -> List[dict[str, Any]]:
    if isinstance(data, list):
        entries: List[dict[str, Any]] = []
        for item in data:
            if _is_review_like(item):
                entries.append(item)
            else:
                entries.extend(_extract_comment_entries(item))
        return entries

    if not isinstance(data, dict):
        return []

    for key in ("reviews", "comments", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if _is_review_like(item)]
        if isinstance(value, dict):
            nested = _extract_comment_entries(value)
            if nested:
                return nested

    nested_data = data.get("data")
    if isinstance(nested_data, (dict, list)):
        nested = _extract_comment_entries(nested_data)
        if nested:
            return nested

    return [data] if _is_review_like(data) else []


def _normalize_comment(
    item: dict[str, Any],
    *,
    provider: str,
    product_id: str,
) -> ReviewComment:
    raw_id = _first_value(item, ("review_id", "reviewId", "id", "code", "hashId"))
    raw_rating = _first_value(item, ("rating", "rate", "score", "stars", "reviewRating"))
    if isinstance(raw_rating, dict):
        rating = _safe_float(raw_rating.get("ratingValue") or raw_rating.get("value"))
    else:
        rating = _safe_float(raw_rating)

    title = _safe_str(_first_value(item, ("title", "headline", "subject")))
    text = _safe_str(
        _first_value(item, ("text", "comment", "review", "content", "message", "reviewBody"))
    )
    if not text and isinstance(item.get("liked"), list) and item["liked"]:
        text = " • ".join(str(x) for x in item["liked"] if x)

    author = None
    if isinstance(item.get("authorInfo"), dict) and item["authorInfo"].get("name"):
        author = _safe_str(item["authorInfo"].get("name"))
        loc = item.get("location")
        if isinstance(loc, dict) and loc.get("city"):
            author = f"{author} ({loc.get('city')})"
    elif isinstance(item.get("user"), dict) and item["user"].get("name"):
        author = _safe_str(item["user"].get("name"))

    if not author:
        author = _safe_str(
            _first_value(item, ("reviewerName", "customerName", "name", "userName", "author"))
        )
    created_at = _safe_str(
        _first_value(
            item,
            (
                "created_at",
                "createdAt",
                "date",
                "created",
                "publishedAt",
                "datePublished",
                "reviewDateTime",
                "searchDate",
            ),
        )
    )
    source_ref = _safe_str(_first_value(item, ("source_ref", "url", "permalink")))

    compact = {
        "rating": rating,
        "title": title,
        "text": text,
        "author": author,
        "created_at": created_at,
    }
    return ReviewComment(
        review_id=str(raw_id) if raw_id else review_comment_key(compact),
        rating=rating,
        title=title,
        text=text,
        author=author,
        created_at=created_at,
        source_provider=provider,
        source_ref=source_ref or product_id,
    )


def _extract_summary(data: Any) -> tuple[Optional[float], Optional[int]]:
    if not isinstance(data, dict):
        return None, None

    # Suporte ao schema Vurdere AI
    agg = data.get("aggregateRating")
    if isinstance(agg, dict):
        raw_val = agg.get("ratingValue") if agg.get("ratingValue") is not None else agg.get("average")
        rating = _safe_float(raw_val)
        raw_count = (
            data.get("total")
            if data.get("total") is not None
            else (
                agg.get("reviewCount")
                if agg.get("reviewCount") is not None
                else agg.get("recommendedBy")
            )
        )
        count = _safe_int(raw_count)
        if rating is not None or count is not None:
            return (round(rating, 1) if rating is not None else None, count)

    rate_info = data.get("rate")
    if isinstance(rate_info, dict):
        raw_val = rate_info.get("average")
        raw_count = rate_info.get("count")
        rating = _safe_float(raw_val)
        count = _safe_int(raw_count)
        if rating is not None or count is not None:
            return (round(rating, 1) if rating is not None else None, count)

    # Suporte a range.total da VTEX Reviews & Ratings
    range_info = data.get("range")
    vtex_total = None
    if isinstance(range_info, dict):
        vtex_total = _safe_int(range_info.get("total"))

    rating = _safe_float(_first_value(data, ("average", "rating", "score")))
    count = _safe_int(_first_value(data, ("totalCount", "count", "review_count"))) or vtex_total
    if rating is not None:
        rating = round(rating, 1)
    return rating, count


def _unsupported_result(
    *,
    provider: Optional[str],
    product_id: Optional[str],
    max_pages: int = 0,
) -> ReviewCommentsResult:
    return ReviewCommentsResult(
        reviews_state=ReviewState.UNSUPPORTED,
        comments=[],
        review_product_id=product_id,
        source_provider=provider,
        max_pages=max_pages,
    )


def _blocked_result(
    *,
    provider: str,
    product_id: str,
    max_pages: int,
    rating: Optional[float] = None,
    review_count: Optional[int] = None,
) -> ReviewCommentsResult:
    """HTTP 403/429 — bloqueio transitorio de anti-bot (D-11), nunca permanente."""
    return ReviewCommentsResult(
        reviews_state=ReviewState.BLOCKED,
        comments=[],
        rating=rating,
        review_count=review_count,
        review_product_id=product_id,
        source_provider=provider,
        max_pages=max_pages,
    )


def _error_result(
    *,
    provider: Optional[str],
    product_id: Optional[str],
    max_pages: int,
    rating: Optional[float] = None,
    review_count: Optional[int] = None,
) -> ReviewCommentsResult:
    """Excecao de transporte ou HTTP nao mapeado (D-11)."""
    return ReviewCommentsResult(
        reviews_state=ReviewState.ERROR,
        comments=[],
        rating=rating,
        review_count=review_count,
        review_product_id=product_id,
        source_provider=provider,
        max_pages=max_pages,
    )


# ---------------------------------------------------------------------------
# Driver: Trustvox (Aramis)
# ---------------------------------------------------------------------------

async def _fetch_trustvox_single(
    session: aiohttp.ClientSession,
    store_id: str,
    product_id: str,
) -> Tuple[str, Tuple[Optional[float], Optional[int]]]:
    """
    Busca a nota média e total de avaliações de um produto na Trustvox.
    Usa o endpoint /widget/root que retorna o resumo consolidado.
    """
    url = "https://trustvox.com.br/widget/root"
    params = {
        "store_id": store_id,
        "code": product_id,
    }
    headers = {
        "Accept": "application/vnd.trustvox-v2+json",
        "User-Agent": _USER_AGENT,
    }

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status in (404, 400):
                return product_id, (None, 0)
            if resp.status != 200:
                logger.debug(f"[Trustvox] HTTP {resp.status} para product {product_id}")
                return product_id, (None, None)

            data = await resp.json(content_type=None)
            rate_info = data.get("rate", {})
            average = rate_info.get("average")
            count = rate_info.get("count", 0)

            if average is not None:
                return product_id, (round(float(average), 1), int(count))

    except Exception as e:
        logger.debug(f"[Trustvox] Erro para product {product_id}: {e}")

    return product_id, (None, None)


async def _fetch_trustvox_bulk(
    store_id: str,
    product_ids: List[str],
) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """Dispara chamadas Trustvox em paralelo para todos os product_ids."""
    if not product_ids:
        return {}

    results: Dict[str, Tuple[Optional[float], Optional[int]]] = {}
    async with aiohttp.ClientSession(timeout=_REVIEW_TIMEOUT) as session:
        tasks = [
            _fetch_trustvox_single(session, store_id, pid)
            for pid in product_ids
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for res in responses:
            if isinstance(res, tuple) and len(res) == 2:
                pid, rating_data = res
                results[pid] = rating_data

    return results


# ---------------------------------------------------------------------------
# Driver: VTEX Native Reviews & Ratings (Reserva)
# ---------------------------------------------------------------------------

async def _fetch_vtex_native_single(
    session: aiohttp.ClientSession,
    domain: str,
    product_id: str,
) -> Tuple[str, Tuple[Optional[float], Optional[int]]]:
    """
    Busca avaliação de um produto via endpoint REST nativo do VTEX Reviews & Ratings.
    Endpoint: https://{domain}/reviews-and-ratings/api/rating/{productId}
    """
    url = f"https://{domain}/reviews-and-ratings/api/rating/{product_id}"
    headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 404:
                return product_id, (None, 0)
            if resp.status != 200:
                logger.debug(f"[VTEX Reviews] HTTP {resp.status} para {product_id} em {domain}")
                return product_id, (None, None)

            if "html" in resp.headers.get("Content-Type", "").lower():
                return product_id, (None, 0)
            try:
                data = await resp.json(content_type=None)
            except Exception:
                return product_id, (None, 0)
            average = data.get("average", 0)
            total = data.get("totalCount", 0)

            if total == 0:
                return product_id, (None, 0)
            if average is not None and total > 0:
                return product_id, (round(float(average), 1), int(total))

    except Exception as e:
        logger.debug(f"[VTEX Reviews] Erro para {product_id} em {domain}: {e}")

    return product_id, (None, None)


async def _fetch_vtex_native_bulk(
    domain: str,
    product_ids: List[str],
) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """Dispara chamadas VTEX Reviews em paralelo para todos os product_ids."""
    if not product_ids:
        return {}

    results: Dict[str, Tuple[Optional[float], Optional[int]]] = {}
    async with aiohttp.ClientSession(timeout=_REVIEW_TIMEOUT) as session:
        tasks = [
            _fetch_vtex_native_single(session, domain, pid)
            for pid in product_ids
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for res in responses:
            if isinstance(res, tuple) and len(res) == 2:
                pid, rating_data = res
                results[pid] = rating_data

    return results


# ---------------------------------------------------------------------------
# Entrypoint Público
# ---------------------------------------------------------------------------

async def _fetch_trustvox_comments(
    brand,
    product_id: str,
    max_pages: int,
) -> ReviewCommentsResult:
    store_id = brand.review_store_id
    if not store_id:
        return _unsupported_result(
            provider="trustvox",
            product_id=product_id,
            max_pages=max_pages,
        )

    comments: List[ReviewComment] = []
    rating: Optional[float] = None
    review_count: Optional[int] = None
    headers = {
        "Accept": "application/vnd.trustvox-v2+json",
        "User-Agent": _USER_AGENT,
    }

    async with aiohttp.ClientSession(timeout=_REVIEW_TIMEOUT) as session:
        for page in range(1, max_pages + 1):
            params = {
                "store_id": store_id,
                "code": product_id,
                "page": page,
            }
            try:
                async with session.get(
                    "https://trustvox.com.br/widget/root",
                    params=params,
                    headers=headers,
                ) as resp:
                    if resp.status in (403, 429):
                        return _blocked_result(
                            provider="trustvox",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status in (404, 400):
                        return _unsupported_result(
                            provider="trustvox",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status == 401:
                        # A1: credential-gated endpoint, tratado como 404 (nunca satisfeito pelo scraper).
                        return _unsupported_result(
                            provider="trustvox",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status != 200:
                        logger.debug(
                            "[Trustvox] HTTP %s ao buscar comentarios de %s",
                            resp.status,
                            product_id,
                        )
                        return _error_result(
                            provider="trustvox",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    data = await resp.json(content_type=None)
            except Exception as exc:
                logger.debug("[Trustvox] Erro ao buscar comentarios: %s", exc)
                return _error_result(
                    provider="trustvox",
                    product_id=product_id,
                    max_pages=max_pages,
                )

            if page == 1:
                rating, review_count = _extract_summary(data)

            entries, opinions_ok = await _fetch_trustvox_opinion_entries(
                session,
                store_id,
                product_id,
                page,
                headers,
            )
            if not opinions_ok:
                return _error_result(
                    provider="trustvox",
                    product_id=product_id,
                    max_pages=max_pages,
                    rating=rating,
                    review_count=review_count,
                )
            if not entries:
                if page == 1 and (review_count or 0) > 0:
                    return _error_result(
                        provider="trustvox",
                        product_id=product_id,
                        max_pages=max_pages,
                        rating=rating,
                        review_count=review_count,
                    )
                break
            comments.extend(
                _normalize_comment(entry, provider="trustvox", product_id=product_id)
                for entry in entries
            )

    comments = dedupe_review_comments(comments)
    return ReviewCommentsResult(
        reviews_state=ReviewState.AVAILABLE,
        comments=comments,
        rating=rating,
        review_count=review_count,
        review_product_id=product_id,
        source_provider="trustvox",
        max_pages=max_pages,
    )


async def _fetch_trustvox_opinion_entries(
    session: aiohttp.ClientSession,
    store_id: str,
    product_id: str,
    page: int,
    headers: dict[str, str],
) -> tuple[List[dict[str, Any]], bool]:
    params = {
        "store_id": store_id,
        "code": product_id,
        "page": page,
    }
    try:
        async with session.get(
            "https://trustvox.com.br/widget/opinions",
            params=params,
            headers=headers,
        ) as resp:
            if resp.status in (404, 400):
                return [], True
            if resp.status != 200:
                logger.debug(
                    "[Trustvox] HTTP %s ao buscar opinioes de %s",
                    resp.status,
                    product_id,
                )
                return [], False
            data = await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("[Trustvox] Erro ao buscar opinioes: %s", exc)
        return [], False
    return _extract_comment_entries(data), True


async def _fetch_vtex_native_comments(
    brand,
    product_id: str,
    max_pages: int,
) -> ReviewCommentsResult:
    domain = str(brand.domain or "").replace("https://", "").replace("http://", "").strip("/")
    if not domain:
        return _unsupported_result(
            provider="vtex_native",
            product_id=product_id,
            max_pages=max_pages,
        )

    comments: List[ReviewComment] = []
    rating: Optional[float] = None
    review_count: Optional[int] = None
    headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    async with aiohttp.ClientSession(timeout=_REVIEW_TIMEOUT) as session:
        for page in range(1, max_pages + 1):
            url = f"https://{domain}/reviews-and-ratings/api/reviews/{product_id}"
            params = {"page": page}
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status in (403, 429):
                        return _blocked_result(
                            provider="vtex_native",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status in (404, 400):
                        return _unsupported_result(
                            provider="vtex_native",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status == 401:
                        # A1: credential-gated endpoint, tratado como 404 (nunca satisfeito pelo scraper).
                        return _unsupported_result(
                            provider="vtex_native",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status != 200:
                        return _error_result(
                            provider="vtex_native",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if "html" in resp.headers.get("Content-Type", "").lower():
                        return _unsupported_result(
                            provider="vtex_native",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        return _unsupported_result(
                            provider="vtex_native",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
            except Exception as exc:
                logger.debug("[VTEX Reviews] Erro ao buscar comentarios: %s", exc)
                return _error_result(
                    provider="vtex_native",
                    product_id=product_id,
                    max_pages=max_pages,
                )

            if page == 1:
                rating, review_count = _extract_summary(data)

            entries = _extract_comment_entries(data)
            if not entries:
                if page == 1:
                    if rating is not None or review_count is not None:
                        # Resumo valido (rating/count) com lista de comentarios vazia:
                        # a loja tem 0/N avaliacoes reais, nao e "unsupported" (Pitfall 1).
                        return ReviewCommentsResult(
                            reviews_state=ReviewState.AVAILABLE,
                            comments=[],
                            rating=rating,
                            review_count=review_count,
                            review_product_id=product_id,
                            source_provider="vtex_native",
                            max_pages=max_pages,
                        )
                    return _unsupported_result(
                        provider="vtex_native",
                        product_id=product_id,
                        max_pages=max_pages,
                    )
                # Pagina > 1 sem mais entradas: fim da paginacao, nao um erro —
                # preserva os comentarios ja acumulados nas paginas anteriores
                # (bug real encontrado no code review: um `return comments=[]`
                # aqui descartava silenciosamente tudo que ja tinha sido lido).
                break
            comments.extend(
                _normalize_comment(
                    entry,
                    provider="vtex_native",
                    product_id=product_id,
                )
                for entry in entries
            )

    comments = dedupe_review_comments(comments)
    if rating is None and comments:
        valid_ratings = [c.rating for c in comments if c.rating is not None]
        if valid_ratings:
            rating = round(sum(valid_ratings) / len(valid_ratings), 1)
    if review_count is None and comments:
        review_count = len(comments)

    return ReviewCommentsResult(
        reviews_state=ReviewState.AVAILABLE,
        comments=comments,
        rating=rating,
        review_count=review_count,
        review_product_id=product_id,
        source_provider="vtex_native",
        max_pages=max_pages,
    )


# ---------------------------------------------------------------------------
# Driver: Vurdere AI (Richards)
# ---------------------------------------------------------------------------

async def _fetch_vurdere_single(
    ecommerce_id: str,
    domain: str,
    product_id: str,
) -> Tuple[str, Tuple[Optional[float], Optional[int]]]:
    """Busca a nota média e total de avaliações de um produto na Vurdere."""
    bulk = await _fetch_vurdere_bulk(ecommerce_id, domain, [product_id])
    return product_id, bulk.get(product_id, (None, None))


async def _fetch_vurdere_bulk(
    ecommerce_id: str,
    domain: str,
    product_ids: List[str],
) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """Dispara chamada à API da Vurdere para recuperar notas em lote."""
    if not product_ids:
        return {}

    url = f"https://{ecommerce_id}-br.mais.social/api/pdp/reviews/rating"
    headers = {
        "Referer": f"https://{domain}/" if domain else f"https://www.richards.com.br/",
        "Origin": f"https://{domain}" if domain else "https://www.richards.com.br",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    results: Dict[str, Tuple[Optional[float], Optional[int]]] = {
        pid: (None, None) for pid in product_ids
    }

    chunk_size = 20
    for i in range(0, len(product_ids), chunk_size):
        chunk = product_ids[i : i + chunk_size]
        params = {
            "ecommerceId": ecommerce_id,
            "productIds": ",".join(str(p) for p in chunk),
            "locale": "ptBr",
        }
        try:
            from curl_cffi.requests import AsyncSession

            async with AsyncSession(impersonate="chrome", timeout=8) as session:
                resp = await session.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    products = data.get("products") or {}
                    for pid in chunk:
                        info = products.get(str(pid)) or products.get(pid)
                        if isinstance(info, dict):
                            val = _safe_float(info.get("ratingValue"))
                            cnt = _safe_int(info.get("reviewsCount"))
                            if val is not None or cnt is not None:
                                results[pid] = (
                                    round(val, 1) if val is not None else None,
                                    cnt or 0,
                                )
        except Exception as exc:
            logger.debug("[Vurdere] Erro ao buscar rating em lote: %s", exc)

    return results


async def _fetch_vurdere_comments(
    brand,
    product_id: str,
    max_pages: int,
) -> ReviewCommentsResult:
    ecommerce_id = getattr(brand, "review_store_id", None) or getattr(brand, "brand_key", "richards")
    domain = str(getattr(brand, "domain", "") or "").replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{ecommerce_id}-br.mais.social/api/pdp/reviews"
    headers = {
        "Referer": f"https://{domain}/" if domain else "https://www.richards.com.br/",
        "Origin": f"https://{domain}" if domain else "https://www.richards.com.br",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    comments: List[ReviewComment] = []
    rating: Optional[float] = None
    review_count: Optional[int] = None

    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome", timeout=10) as session:
            for page in range(1, max_pages + 1):
                params = {
                    "ecommerceId": ecommerce_id,
                    "productId": product_id,
                    "locale": "ptBr",
                    "page": page,
                }
                resp = await session.get(url, params=params, headers=headers)
                if resp.status_code in (403, 429):
                    return _blocked_result(
                        provider="vurdere",
                        product_id=product_id,
                        max_pages=max_pages,
                    )
                if resp.status_code in (404, 400):
                    return _unsupported_result(
                        provider="vurdere",
                        product_id=product_id,
                        max_pages=max_pages,
                    )
                if resp.status_code != 200:
                    return _error_result(
                        provider="vurdere",
                        product_id=product_id,
                        max_pages=max_pages,
                    )

                data = resp.json()
                if page == 1:
                    rating, review_count = _extract_summary(data)

                entries = _extract_comment_entries(data)
                if not entries:
                    if page == 1:
                        if rating is not None or review_count is not None:
                            return ReviewCommentsResult(
                                reviews_state=ReviewState.AVAILABLE,
                                comments=[],
                                rating=rating,
                                review_count=review_count,
                                review_product_id=product_id,
                                source_provider="vurdere",
                                max_pages=max_pages,
                            )
                        return _unsupported_result(
                            provider="vurdere",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    break

                comments.extend(
                    _normalize_comment(
                        entry,
                        provider="vurdere",
                        product_id=product_id,
                    )
                    for entry in entries
                )

    except Exception as exc:
        logger.debug("[Vurdere] Erro ao buscar comentarios: %s", exc)
        return _error_result(
            provider="vurdere",
            product_id=product_id,
            max_pages=max_pages,
        )

    comments = dedupe_review_comments(comments)
    if rating is None and comments:
        valid_ratings = [c.rating for c in comments if c.rating is not None]
        if valid_ratings:
            rating = round(sum(valid_ratings) / len(valid_ratings), 1)
    if review_count is None and comments:
        review_count = len(comments)

    return ReviewCommentsResult(
        reviews_state=ReviewState.AVAILABLE,
        comments=comments,
        rating=rating,
        review_count=review_count,
        review_product_id=product_id,
        source_provider="vurdere",
        max_pages=max_pages,
    )


# ---------------------------------------------------------------------------
# Driver: YourViews (Levi's)
# ---------------------------------------------------------------------------

async def _fetch_yourviews_single(
    session: aiohttp.ClientSession,
    store_id: str,
    product_id: str,
) -> Tuple[str, Tuple[Optional[float], Optional[int]]]:
    """
    Busca a nota média e total de avaliações de um produto na YourViews.
    Usa o endpoint /review/GetReview que retorna HTML com microdata schema.org.
    """
    url = "https://service2.yourviews.com.br/review/GetReview"
    params = {
        "storeKey": store_id,
        "productStoreId": product_id,
    }
    headers = {
        "Accept": "application/json, text/javascript, */*",
        "User-Agent": _USER_AGENT,
    }
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status in (404, 400):
                return product_id, (None, 0)
            if resp.status != 200:
                logger.debug(f"[YourViews] HTTP {resp.status} para product {product_id}")
                return product_id, (None, None)

            text = (await resp.text()).strip()
            if text.startswith("(") and text.endswith(")"):
                text = text[1:-1]
            try:
                data = json.loads(text)
            except Exception:
                return product_id, (None, None)

            if not data.get("hasResults") or not data.get("html"):
                return product_id, (None, 0)

            from bs4 import BeautifulSoup

            soup = BeautifulSoup(data["html"], "html.parser")
            agg = soup.find(attrs={"itemprop": "aggregateRating"})
            if agg:
                rating_el = agg.find(attrs={"itemprop": "ratingValue"})
                count_el = agg.find(attrs={"itemprop": "ratingCount"})
                rating = _safe_float(rating_el.get("content") if rating_el else None)
                count = _safe_int(count_el.get_text(strip=True) if count_el else None)
                if rating is not None or count is not None:
                    return product_id, (round(rating, 1) if rating is not None else None, count or 0)

            reviews = soup.find_all(attrs={"itemprop": "review"})
            if reviews:
                return product_id, (None, len(reviews))

    except Exception as e:
        logger.debug(f"[YourViews] Erro para product {product_id}: {e}")

    return product_id, (None, None)


async def _fetch_yourviews_bulk(
    store_id: str,
    product_ids: List[str],
) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """Dispara chamadas YourViews em paralelo para todos os product_ids."""
    if not product_ids:
        return {}

    results: Dict[str, Tuple[Optional[float], Optional[int]]] = {}
    async with aiohttp.ClientSession(timeout=_REVIEW_TIMEOUT) as session:
        tasks = [
            _fetch_yourviews_single(session, store_id, pid)
            for pid in product_ids
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for res in responses:
            if isinstance(res, tuple) and len(res) == 2:
                pid, rating_data = res
                results[pid] = rating_data

    return results


async def _fetch_yourviews_comments(
    brand: Any,
    product_id: str,
    max_pages: int,
) -> ReviewCommentsResult:
    """Coleta avaliações e comentários paginados da YourViews."""
    store_key = getattr(brand, "review_store_id", None) or "9ca1b06f-2d54-4dc9-b9f0-a54df72b12e6"
    url = "https://service2.yourviews.com.br/review/GetReview"
    headers = {
        "Accept": "application/json, text/javascript, */*",
        "User-Agent": _USER_AGENT,
    }

    comments: List[ReviewComment] = []
    rating: Optional[float] = None
    review_count: Optional[int] = None

    from bs4 import BeautifulSoup

    async with aiohttp.ClientSession(timeout=_REVIEW_TIMEOUT) as session:
        for page in range(1, max_pages + 1):
            params = {
                "storeKey": store_key,
                "productStoreId": product_id,
                "page": page,
            }
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status in (403, 429):
                        return _blocked_result(
                            provider="yourviews",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status in (404, 400):
                        return _unsupported_result(
                            provider="yourviews",
                            product_id=product_id,
                            max_pages=max_pages,
                        )
                    if resp.status != 200:
                        return _error_result(
                            provider="yourviews",
                            product_id=product_id,
                            max_pages=max_pages,
                        )

                    text = (await resp.text()).strip()
                    if text.startswith("(") and text.endswith(")"):
                        text = text[1:-1]
                    try:
                        data = json.loads(text)
                    except Exception:
                        return _error_result(
                            provider="yourviews",
                            product_id=product_id,
                            max_pages=max_pages,
                        )

                    if not data.get("hasResults") or not data.get("html"):
                        if page == 1:
                            return _unsupported_result(
                                provider="yourviews",
                                product_id=product_id,
                                max_pages=max_pages,
                            )
                        break

                    soup = BeautifulSoup(data["html"], "html.parser")

                    if page == 1:
                        agg = soup.find(attrs={"itemprop": "aggregateRating"})
                        if agg:
                            rating_el = agg.find(attrs={"itemprop": "ratingValue"})
                            count_el = agg.find(attrs={"itemprop": "ratingCount"})
                            rating = _safe_float(rating_el.get("content") if rating_el else None)
                            review_count = _safe_int(count_el.get_text(strip=True) if count_el else None)

                    review_nodes = soup.find_all(attrs={"itemprop": "review"})
                    if not review_nodes:
                        if page == 1 and (rating is not None or review_count is not None):
                            return ReviewCommentsResult(
                                reviews_state=ReviewState.AVAILABLE,
                                comments=[],
                                rating=rating,
                                review_count=review_count,
                                review_product_id=product_id,
                                source_provider="yourviews",
                                max_pages=max_pages,
                            )
                        break

                    for node in review_nodes:
                        rid = node.get("name") or node.get("data-yv-rid")
                        author_el = node.find(attrs={"itemprop": "author"})
                        author = author_el.get_text(strip=True) if author_el else None

                        rate_node = node.find(attrs={"itemprop": "reviewRating"})
                        rate_val_el = rate_node.find(attrs={"itemprop": "ratingValue"}) if rate_node else None
                        r_rating = _safe_float(rate_val_el.get("content") if rate_val_el else None)

                        date_el = node.find(attrs={"itemprop": "datePublished"})
                        created_at = date_el.get("content") if date_el else None

                        title_el = node.find(attrs={"itemprop": "name"})
                        title = title_el.get_text(strip=True) if title_el else None

                        desc_el = node.find(attrs={"itemprop": "description"})
                        body = desc_el.get_text(strip=True) if desc_el else None

                        c_entry = {
                            "review_id": rid,
                            "rating": r_rating,
                            "title": title,
                            "text": body,
                            "author": author,
                            "created_at": created_at,
                        }
                        comments.append(
                            _normalize_comment(
                                c_entry,
                                provider="yourviews",
                                product_id=product_id,
                            )
                        )

            except Exception as exc:
                logger.debug("[YourViews] Erro ao buscar comentarios: %s", exc)
                return _error_result(
                    provider="yourviews",
                    product_id=product_id,
                    max_pages=max_pages,
                )

    comments = dedupe_review_comments(comments)
    if rating is None and comments:
        valid_ratings = [c.rating for c in comments if c.rating is not None]
        if valid_ratings:
            rating = round(sum(valid_ratings) / len(valid_ratings), 1)
    if review_count is None and comments:
        review_count = len(comments)

    return ReviewCommentsResult(
        reviews_state=ReviewState.AVAILABLE,
        comments=comments,
        rating=rating,
        review_count=review_count,
        review_product_id=product_id,
        source_provider="yourviews",
        max_pages=max_pages,
    )


def cap_review_pages(max_pages: Optional[int]) -> int:
    """Clamp a requested page count to the configured MAX_REVIEW_PAGES ceiling.

    Shared by routes_monitor.py and routes_search.py so the review-page cap
    exists in exactly one place (Don't Hand-Roll).
    """
    configured_max = max(1, int(settings.MAX_REVIEW_PAGES))
    if max_pages is None:
        return configured_max
    return min(max(1, int(max_pages)), configured_max)


async def get_review_comments(
    brand_key: str,
    product_id: str,
    max_pages: Optional[int] = None,
) -> ReviewCommentsResult:
    """Fetch compact review comments on demand for an audited provider."""
    if not product_id:
        return _unsupported_result(provider=None, product_id=product_id)

    capped_pages = cap_review_pages(max_pages)

    brand_config = brand_service.get_brand(brand_key)
    if not brand_config:
        return _unsupported_result(
            provider=None,
            product_id=product_id,
            max_pages=capped_pages,
        )

    provider = brand_config.review_provider or "none"
    has_evidence = bool(getattr(brand_config, "review_provider_evidence", None))
    if provider == "none" or not has_evidence:
        return _unsupported_result(
            provider=provider,
            product_id=product_id,
            max_pages=capped_pages,
        )

    try:
        if provider == "trustvox" and brand_config.review_store_id:
            result = await _fetch_trustvox_comments(
                brand_config,
                product_id,
                capped_pages,
            )
        elif provider == "vtex_native":
            result = await _fetch_vtex_native_comments(
                brand_config,
                product_id,
                capped_pages,
            )
        elif provider == "vurdere":
            result = await _fetch_vurdere_comments(
                brand_config,
                product_id,
                capped_pages,
            )
        elif provider == "yourviews":
            result = await _fetch_yourviews_comments(
                brand_config,
                product_id,
                capped_pages,
            )
        else:
            return _unsupported_result(
                provider=provider,
                product_id=product_id,
                max_pages=capped_pages,
            )
    except Exception as exc:
        logger.debug("[ReviewService] Erro ao buscar comentarios: %s", exc)
        return _error_result(
            provider=provider,
            product_id=product_id,
            max_pages=capped_pages,
        )

    result.comments = dedupe_review_comments(result.comments)
    result.max_pages = capped_pages
    result.review_product_id = result.review_product_id or product_id
    result.source_provider = result.source_provider or provider
    return result


async def fetch_scan_product_review_comments(
    monitor_id: str,
    scan_product_id: str,
    max_pages: Optional[int] = None,
) -> ReviewCommentsResult:
    from core.db import db

    # 1. Tentar buscar monitor no banco de dados
    monitor = None
    brand_key = ""
    try:
        mon_rec = await db.category_monitors.find_first(
            where={"id": int(monitor_id)},
            include={"brands": True}
        )
        if mon_rec:
            brand_key = (mon_rec.brands.brand_key if mon_rec.brands else "").lower().strip()
            monitor = {
                "id": str(mon_rec.id),
                "brand": brand_key,
                "url": mon_rec.category_url,
            }
    except Exception:
        pass

    if not monitor:
        # Fallback legado para testes que ainda usam JSON mockado em tmp_path
        try:
            monitor = _find_monitor(monitor_id)
            brand_key = str(monitor.get("brand") or "").lower().strip()
        except Exception:
            raise ValueError("Monitor de categoria nao encontrado.")

    if not brand_key:
        raise ValueError("Monitor nao possui marca persistida.")

    # 2. Tentar buscar produto no banco de dados (tabela scan_products)
    product = None
    product_db_record = None
    try:
        product_db_record = await db.scan_products.find_first(
            where={"scan_product_id": scan_product_id}
        )
        if product_db_record:
            product = {
                "id": str(product_db_record.id),
                "scan_product_id": product_db_record.scan_product_id,
                "url": product_db_record.product_url,
                "raw_title": product_db_record.raw_title,
                "product_code": product_db_record.product_code,
                "review_product_id": product_db_record.review_product_id,
                "rating": float(product_db_record.rating) if product_db_record.rating is not None else None,
                "review_count": product_db_record.review_count,
                "reviews_state": product_db_record.reviews_state,
            }
    except Exception:
        pass

    products = []
    product_index = -1
    if not product:
        # Fallback legado para testes
        products = _load_products(monitor_id)
        product_index, product = _find_scan_product(products, scan_product_id)

    capped_pages = cap_review_pages(max_pages)
    review_product_id = _safe_str(product.get("review_product_id"))
    if not review_product_id:
        result = await _fetch_engine_review_summary(
            brand_key,
            product,
            max_pages=capped_pages,
        )
        if result is None:
            result = _summary_result_from_product(
                product,
                provider="persisted-summary",
                max_pages=capped_pages,
            )
        if result is None:
            result = _unsupported_result(
                provider=None,
                product_id=None,
                max_pages=0,
            )
    else:
        result = await get_review_comments(
            brand_key,
            review_product_id,
            max_pages=capped_pages,
        )
        if result.reviews_state == ReviewState.UNSUPPORTED:
            summary_result = await _fetch_engine_review_summary(
                brand_key,
                product,
                max_pages=capped_pages,
            )
            if summary_result is None:
                summary_result = _summary_result_from_product(
                    product,
                    provider="persisted-summary",
                    max_pages=capped_pages,
                )
            if summary_result is not None:
                result = summary_result

    # 3. Salvar no banco de dados se tiver registro em scan_products
    if product_db_record:
        try:
            state_val = result.reviews_state.value if hasattr(result.reviews_state, "value") else str(result.reviews_state)
            await db.scan_products.update(
                where={"id": product_db_record.id},
                data={
                    "reviews_state": state_val,
                    "rating": result.rating,
                    "review_count": result.review_count,
                    "review_product_id": result.review_product_id or product_db_record.review_product_id,
                }
            )

            # Persiste comentários individuais na tabela review_comments
            if result.comments:
                for c in result.comments:
                    try:
                        provider_val = str(c.source_provider or result.source_provider or "unknown")[:50]
                        rev_id_val = str(c.review_id or hashlib.sha256((c.text or "").encode()).hexdigest())[:200]
                        author_val = str(c.author or "")[:200] if c.author else None
                        await db.execute_raw(
                            """
                            INSERT INTO scraping.review_comments 
                                (scan_product_id, source_provider, review_id, rating, title, body, author_name, source_ref, captured_at)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                            ON CONFLICT (source_provider, review_id) 
                            DO UPDATE SET rating = EXCLUDED.rating, body = EXCLUDED.body, author_name = EXCLUDED.author_name;
                            """,
                            product_db_record.id,
                            provider_val,
                            rev_id_val,
                            c.rating,
                            c.title,
                            c.text,
                            author_val,
                            c.source_ref,
                        )
                    except Exception as err_c:
                        logger.debug("Erro ao persistir review comment individual: %s", err_c)
        except Exception as err:
            logger.warning(f"Erro ao persistir reviews no banco: {err}")
    elif products and product_index >= 0:
        products[product_index] = _apply_review_result(product, result)
        _write_products(monitor_id, products)

    return result


async def _fetch_engine_review_summary(
    brand_key: str,
    product: dict[str, Any],
    *,
    max_pages: int,
) -> ReviewCommentsResult | None:
    url = _safe_str(product.get("url"))
    if not url:
        return None
    try:
        from services.engines.factory import engine_factory

        engine = engine_factory.get_engine(brand_key)
        detail = None
        if hasattr(engine, "get_pdp_product"):
            detail = await engine.get_pdp_product(url)
        if not detail and hasattr(engine, "get_product_details"):
            detail = await engine.get_product_details(url)
    except Exception as exc:
        logger.debug("[ReviewService] Erro ao buscar resumo via engine: %s", exc)
        return None
    if not detail:
        return None
    return _summary_result_from_product(
        detail,
        provider="engine-summary",
        max_pages=max_pages,
    )


def _summary_result_from_product(
    product: dict[str, Any] | Any,
    *,
    provider: str,
    max_pages: int,
) -> ReviewCommentsResult | None:
    rating = _safe_float(
        _first_product_value(product, ("rating", "rating_value", "ratingValue"))
    )
    review_count = _safe_int(
        _first_product_value(
            product,
            (
                "review_count",
                "reviews_count",
                "reviewCount",
                "rating_count",
                "ratingCount",
            ),
        )
    )
    if rating is None and review_count is None:
        return None
    return ReviewCommentsResult(
        reviews_state=ReviewState.AVAILABLE,
        comments=[],
        rating=round(rating, 1) if rating is not None else None,
        review_count=review_count,
        review_product_id=_safe_str(_first_product_value(product, ("review_product_id",))),
        source_provider=provider,
        max_pages=max_pages,
    )


def _first_product_value(product: dict[str, Any] | Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = product.get(key) if isinstance(product, dict) else getattr(product, key, None)
        if value not in (None, ""):
            return value
    aggregate = (
        product.get("aggregateRating")
        if isinstance(product, dict)
        else getattr(product, "aggregateRating", None)
    )
    if isinstance(aggregate, dict):
        for key in keys:
            value = aggregate.get(key)
            if value not in (None, ""):
                return value
    return None


def _find_monitor(monitor_id: str) -> dict[str, Any]:
    monitors_file = DATA_DIR / "monitored_categories.json"
    if not monitors_file.exists():
        raise ValueError("Monitor de categoria nao encontrado.")
    try:
        monitors = json.loads(monitors_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("Arquivo de monitores invalido.") from exc
    for monitor in monitors if isinstance(monitors, list) else []:
        if monitor.get("id") == monitor_id:
            return monitor
    raise ValueError("Monitor de categoria nao encontrado.")


def _load_products(monitor_id: str) -> list[dict[str, Any]]:
    products_file = _products_file(monitor_id)
    if not products_file.exists():
        raise ValueError("Produtos monitorados nao encontrados.")
    try:
        products = json.loads(products_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("Arquivo de produtos monitorados invalido.") from exc
    if not isinstance(products, list):
        raise ValueError("Arquivo de produtos monitorados invalido.")
    return [dict(product) for product in products if isinstance(product, dict)]


def _find_scan_product(
    products: list[dict[str, Any]],
    scan_product_id: str,
) -> tuple[int, dict[str, Any]]:
    matches = [
        (index, product)
        for index, product in enumerate(products)
        if product.get("scan_product_id") == scan_product_id
    ]
    if not matches:
        raise ValueError("Produto do scan nao encontrado.")
    if len(matches) > 1:
        raise ValueError("Produto do scan duplicado.")
    return matches[0]


def _apply_review_result(
    product: dict[str, Any],
    result: ReviewCommentsResult,
) -> dict[str, Any]:
    updated = dict(product)
    updated.update(
        {
            "reviews_state": result.reviews_state,
            "review_comments": [
                comment.model_dump(mode="json") for comment in result.comments
            ],
            "rating": result.rating,
            "review_count": result.review_count,
        }
    )
    return updated


def _write_products(monitor_id: str, products: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _products_file(monitor_id).write_text(
        json.dumps(products, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _products_file(monitor_id: str) -> Path:
    return DATA_DIR / f"monitored_products_{monitor_id}.json"


async def get_bulk_reviews(
    brand_key: str,
    product_ids: List[str],
) -> Dict[str, Tuple[Optional[float], Optional[int]]]:
    """
    Entrypoint principal para coletar reviews em lote.
    Verifica o provedor no BRAND_REGISTRY e roteia para o driver adequado.

    Args:
        brand_key: Identificador da marca (ex: 'aramis', 'reserva')
        product_ids: Lista de IDs únicos de produto (ex: productId da VTEX)

    Returns:
        Um dicionário mapeando productId -> (rating, count)
        Se não houver avaliação, retorna (None, None) para aquele product_id.
    """
    if not product_ids:
        return {}

    brand_config = brand_service.get_brand(brand_key)
    if not brand_config:
        return {}

    provider = brand_config.review_provider
    logger.debug(f"[DEBUG] Buscando reviews para {brand_key} usando provedor: {provider}")

    if provider == "trustvox":
        store_id = brand_config.review_store_id
        if store_id:
            return await _fetch_trustvox_bulk(store_id, product_ids)

    elif provider == "vtex_native":
        domain = brand_config.domain
        if domain:
            return await _fetch_vtex_native_bulk(domain, product_ids)

    elif provider == "vurdere":
        ecommerce_id = brand_config.review_store_id or brand_key
        domain = brand_config.domain or ""
        return await _fetch_vurdere_bulk(ecommerce_id, domain, product_ids)

    elif provider == "yourviews":
        store_id = brand_config.review_store_id or "9ca1b06f-2d54-4dc9-b9f0-a54df72b12e6"
        return await _fetch_yourviews_bulk(store_id, product_ids)

    # Provedor não suportado ou 'none' (ex: Tommy)
    return {}


async def get_single_review(
    brand_key: str,
    product_id: str,
) -> Tuple[Optional[float], Optional[int]]:
    """
    Helper para consultas unitárias (ex: no scraper de página única).
    Retorna (rating, count) ou (None, None).
    """
    if not product_id:
        return None, None

    res = await get_bulk_reviews(brand_key, [product_id])
    return res.get(str(product_id), (None, None))
