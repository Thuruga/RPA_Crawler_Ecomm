from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import settings
from core.session_manager import SessionManager
from services.brand_service import brand_service
from services.engines.factory import engine_factory
from services.review_service import get_review_comments


QUERIES = [
    "camisa",
    "polo",
    "calca",
    "camiseta",
    "jaqueta",
    "bermuda",
    "tenis",
]
MAX_PRODUCTS_PER_BRAND = 3
CONCURRENCY = 4
PER_BRAND_TIMEOUT_SECONDS = 90


@dataclass
class ReviewAudit:
    state: str
    source: str | None = None
    rating: float | None = None
    review_count: int | None = None
    review_product_id: str | None = None

    def score(self) -> int:
        if self.state == "available":
            return 3
        if self.state == "temporary_failure":
            return 2
        return 1


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", ".")), 1)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


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


def _extract_review_summary(product: dict[str, Any] | Any) -> ReviewAudit | None:
    rating = _safe_float(
        _first_product_value(product, ("rating", "rating_value", "ratingValue"))
    )
    review_count = _safe_int(
        _first_product_value(
            product,
            ("review_count", "reviews_count", "reviewCount", "rating_count", "ratingCount"),
        )
    )
    if rating is None and review_count is None:
        return None
    return ReviewAudit(
        state="available",
        source="engine-summary",
        rating=rating,
        review_count=review_count,
        review_product_id=_field(product, "review_product_id"),
    )




async def _collect_sample_urls(engine: Any) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()

    for query in QUERIES:
        try:
            result = await engine.search(query, max_results=5)
        except Exception as exc:
            errors.append(f"{query}: {exc}")
            continue
        for product in getattr(result, "products", []) or []:
            url = _field(product, "url")
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= MAX_PRODUCTS_PER_BRAND:
                return urls, errors
    return urls, errors


async def _audit_brand(brand: Any) -> dict[str, Any]:
    brand_key = str(_field(brand, "brand_key") or "").lower()
    engine_name = str(_field(brand, "engine") or "")
    review_provider = str(_field(brand, "review_provider") or "none")
    engine = engine_factory.get_engine(brand_key)

    urls, search_errors = await _collect_sample_urls(engine)
    result: dict[str, Any] = {
        "brand_key": brand_key,
        "brand_name": _field(brand, "brand_name"),
        "engine": engine_name,
        "domain": _field(brand, "domain"),
        "review_provider": review_provider,
        "search_queries": QUERIES,
        "search_errors": search_errors,
        "sample_urls": urls,
        "sample_size": len(urls),
    }

    if not urls:
        result.update(
            {
                "status": "search_failed",
                "stock_state": None,
                "stock_source": None,
                "stock_supported": False,
                "reviews_state": None,
                "reviews_source": None,
                "reviews_supported": False,
                "rating": None,
                "review_count": None,
                "notes": "Nenhum produto encontrado nas buscas de amostra.",
            }
        )
        return result

    best_review: ReviewAudit | None = None
    detail_errors: list[str] = []

    for url in urls:
        try:
            detail = await engine.get_pdp_product(url)
        except Exception as exc:
            detail_errors.append(f"{url}: {exc}")
            continue
        if not detail:
            detail_errors.append(f"{url}: sem detalhe da PDP")
            continue



        summary = _extract_review_summary(detail)
        if summary and (best_review is None or summary.score() > best_review.score()):
            best_review = summary

        review_product_id = _field(detail, "review_product_id")
        if review_provider != "none" and review_product_id:
            try:
                provider_result = await get_review_comments(brand_key, str(review_product_id), max_pages=1)
                candidate = ReviewAudit(
                    state=provider_result.reviews_state,
                    source=provider_result.source_provider,
                    rating=provider_result.rating,
                    review_count=provider_result.review_count,
                    review_product_id=provider_result.review_product_id,
                )
                if best_review is None or candidate.score() > best_review.score():
                    best_review = candidate
            except Exception as exc:
                candidate = ReviewAudit(
                    state="temporary_failure",
                    source=review_provider,
                    review_product_id=str(review_product_id),
                )
                if best_review is None or candidate.score() > best_review.score():
                    best_review = candidate
                detail_errors.append(f"{url}: reviews provider falhou ({exc})")

            "status": "ok" if best_review else "detail_failed",
            "stock_state": None,
            "stock_source": None,
            "stock_label": None,
            "stock_supported": False,
            "reviews_state": reviews_state,
            "reviews_source": best_review.source if best_review else None,
            "reviews_supported": reviews_supported,
            "rating": best_review.rating if best_review else None,
            "review_count": best_review.review_count if best_review else None,
            "review_product_id": best_review.review_product_id if best_review else None,
            "detail_errors": detail_errors,
        }
    )
    return result


async def main() -> None:
    brands = sorted(
        brand_service.list_brands(active_only=False),
        key=lambda brand: str(_field(brand, "brand_name") or _field(brand, "brand_key") or "").lower(),
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(brand: Any) -> dict[str, Any]:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    _audit_brand(brand),
                    timeout=PER_BRAND_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                return {
                    "brand_key": str(_field(brand, "brand_key") or "").lower(),
                    "brand_name": _field(brand, "brand_name"),
                    "engine": _field(brand, "engine"),
                    "domain": _field(brand, "domain"),
                    "review_provider": _field(brand, "review_provider"),
                    "search_queries": QUERIES,
                    "search_errors": [],
                    "sample_urls": [],
                    "sample_size": 0,
                    "status": "timeout",
                    "stock_state": None,
                    "stock_source": None,
                    "stock_label": None,
                    "stock_supported": False,
                    "reviews_state": None,
                    "reviews_source": None,
                    "reviews_supported": False,
                    "rating": None,
                    "review_count": None,
                    "review_product_id": None,
                    "detail_errors": [
                        f"Timeout de {PER_BRAND_TIMEOUT_SECONDS}s na auditoria da marca."
                    ],
                }

    report = await asyncio.gather(*(_guarded(brand) for brand in brands))

    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path("data") / "audits"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"brand_stock_reviews_{generated_at}.json"
    payload = {
        "generated_at": generated_at,
        "brands_tested": len(report),
        "report": report,
    }
    output_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"REPORT_FILE={output_file.resolve()}")
    for item in report:
        print(
            "\t".join(
                [
                    item["brand_key"],
                    str(item["engine"]),
                    f"stock={item['stock_state']}",
                    f"reviews={item['reviews_state']}",
                    f"rating={item['rating']}",
                    f"count={item['review_count']}",
                    f"samples={item['sample_size']}",
                ]
            )
        )

    await SessionManager.close_session()


if __name__ == "__main__":
    asyncio.run(main())
