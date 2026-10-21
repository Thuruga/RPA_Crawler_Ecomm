"""Conservative pure parser for publicly advertised HTML offers."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from core.models import ConditionalOffer, ConditionalOfferResult, OfferScope, OfferSource
from core.browser_manager import BrowserManager
from core.session_manager import SessionManager
from services.promotions.base import normalize_quantity_offer
from services.promotions.guards import validate_promotion_target

_TAG = re.compile(r"<[^>]+>")
_MONEY = r"R\$\s*(\d+(?:\.\d{3})*(?:,\d{2})?)"


def _text(markup: str) -> str:
    return " ".join(unescape(_TAG.sub(" ", markup)).split())


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(".", "").replace(",", "."))


def parse(markup: str, *, product_id: str | None = None) -> ConditionalOfferResult:
    text = _text(markup)
    lowered = text.casefold()
    if any(marker in lowered for marker in ("entre na sua conta", "faça login", "faca login")):
        return ConditionalOfferResult(status="requires_auth", probe_level="fast", diagnostics=[text])

    quantity = re.search(rf"leve\s+(\d+)\s+por\s+{_MONEY}", text, re.IGNORECASE)
    if quantity:
        count = int(quantity.group(1))
        total = _decimal(quantity.group(2))
        unit = normalize_quantity_offer(quantity=count, total=total)
        offer = ConditionalOffer(
            condition_type="quantity",
            condition_label=quantity.group(0),
            eligibility="requires_quantity",
            price=unit,
            effective_unit_price=unit,
            quantity_required=count,
            scope=OfferScope(product_id=product_id),
            state="advertised",
            source=OfferSource(kind="html", ref="public_html", authority="advertised"),
            raw_text=text,
        )
        return ConditionalOfferResult(status="found", offers=[offer], probe_level="fast")

    pix = re.search(rf"{_MONEY}\s+no\s+pix", text, re.IGNORECASE)
    if pix:
        offer = ConditionalOffer(
            condition_type="payment_method",
            condition_label=pix.group(0),
            eligibility="requires_payment_method",
            price=_decimal(pix.group(1)),
            payment_method="pix",
            scope=OfferScope(product_id=product_id),
            state="advertised",
            source=OfferSource(kind="html", ref="public_html", authority="advertised"),
            raw_text=text,
        )
        return ConditionalOfferResult(status="found", offers=[offer], probe_level="fast")

    if "oferta anunciada no carrinho" in lowered:
        offer = ConditionalOffer(
            condition_type="checkout",
            condition_label="Oferta anunciada no carrinho",
            eligibility="requires_checkout",
            scope=OfferScope(product_id=product_id),
            state="advertised",
            source=OfferSource(kind="html", ref="public_html", authority="advertised"),
            raw_text=text,
        )
        return ConditionalOfferResult(status="found", offers=[offer], probe_level="fast")
    return ConditionalOfferResult(status="none_detected", probe_level="fast")


def parse_http(status: int, payload: Any) -> ConditionalOfferResult:
    if status == 401:
        return ConditionalOfferResult(status="requires_auth", diagnostics=["http_status=401"])
    if status == 403:
        return ConditionalOfferResult(status="blocked", diagnostics=["http_status=403"])
    if status == 429 or status >= 500:
        return ConditionalOfferResult(status="error", diagnostics=[f"http_status={status}"])
    if isinstance(payload, str):
        return parse(payload)
    return parse_json(payload)


def parse_json(payload: Any) -> ConditionalOfferResult:
    if not isinstance(payload, dict) or not any(key in payload for key in ("offers", "promotions")):
        return ConditionalOfferResult(status="error", diagnostics=["unexpected promotion schema"])
    return ConditionalOfferResult(status="none_detected")


parse_html = parse


async def _response_payload(response: Any) -> Any:
    try:
        # Determine content-type: aiohttp exposes .content_type, curl_cffi uses headers dict
        content_type = str(getattr(response, "content_type", "") or "").casefold()
        if not content_type:
            headers = getattr(response, "headers", {}) or {}
            content_type = str(headers.get("content-type", "") or "").casefold()

        if "json" in content_type:
            if hasattr(response, "json"):
                result = response.json()
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    return await result
                return result

        if hasattr(response, "text"):
            result = response.text
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await result
            # If text looks like JSON, try to parse it
            if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
                try:
                    return json.loads(result)
                except (json.JSONDecodeError, ValueError):
                    pass
            return result
    except Exception:
        return None
    return response if isinstance(response, (dict, str)) else None



async def fetch_public_payload(
    target: str,
    *,
    allowed_host: str,
    session: Any | None = None,
    max_redirects: int = 4,
    timeout_seconds: float = 15.0,
) -> tuple[int, Any, list[str]]:
    """Fetch a public URL while validating every redirect before following it."""
    current = validate_promotion_target(target, allowed_host=allowed_host)
    active_session = session or await SessionManager.get_session()
    redirects: list[str] = []
    for _ in range(max_redirects + 1):
        response = await active_session.request(
            "GET", current, allow_redirects=False, timeout=timeout_seconds
        )
        status = int(response.get("status", 200)) if isinstance(response, dict) else int(getattr(response, "status", 0))
        headers = response.get("headers", {}) if isinstance(response, dict) else getattr(response, "headers", {})
        if 300 <= status < 400 and headers.get("Location"):
            next_url = urljoin(current, headers["Location"])
            current = validate_promotion_target(next_url, allowed_host=allowed_host)
            redirects.append(current)
            if hasattr(response, "release"):
                response.release()
            continue
        payload = await _response_payload(response)
        if hasattr(response, "release"):
            response.release()
        return status, payload, [f"redirects={len(redirects)}"] if redirects else []
    raise ValueError("promotion probe exceeded redirect limit")


async def probe(
    target: str,
    *,
    allowed_host: str,
    level: str = "full",
    session: Any | None = None,
    product_id: str | None = None,
) -> ConditionalOfferResult:
    """Run public HTML transport; browser rendering is allowed only at full level."""
    validate_promotion_target(target, allowed_host=allowed_host)
    if level == "fast":
        return ConditionalOfferResult(
            status="unsupported",
            probe_level="fast",
            diagnostics=["HTML transport is not part of the fast path"],
        )
    status, payload, diagnostics = await fetch_public_payload(
        target, allowed_host=allowed_host, session=session
    )
    result = parse_http(status, payload)
    result.probe_level = "full"
    result.sources_attempted = [urlparse(target).path or "/"]
    result.diagnostics.extend(diagnostics)
    if result.status in {"blocked", "error"} or not isinstance(payload, str):
        return result
    if result.status == "none_detected":
        markup = await BrowserManager.fetch_html(target)
        result = parse(markup, product_id=product_id)
        result.probe_level = "full"
        result.sources_attempted = [urlparse(target).path or "/", "rendered_dom"]
    return result
