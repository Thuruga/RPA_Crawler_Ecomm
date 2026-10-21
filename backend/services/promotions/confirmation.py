"""Anonymous, ephemeral and strictly allowlisted promotion confirmation."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp

from core.models import ConditionalOffer, ConditionalOfferResult
from services.promotions.guards import build_probe_endpoint, validate_promotion_target


VTEX_SIMULATION_PATH = "/api/checkout/pub/orderForms/simulation"
SHOPIFY_CART_PATH = "/cart/add.js"
_ALLOWED_TARGETS = {
    "vtex": ("POST", VTEX_SIMULATION_PATH),
    "shopify": ("POST", SHOPIFY_CART_PATH),
}
_FORBIDDEN_INPUT_PARTS = {
    "address",
    "authorization",
    "cookie",
    "credential",
    "customer",
    "document",
    "email",
    "password",
    "phone",
    "profile",
    "session",
    "token",
    "transaction",
}


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _scope_matches(advertised: ConditionalOffer, candidate: ConditionalOffer) -> bool:
    for name in ("sku_id", "seller_id"):
        expected = getattr(advertised.scope, name)
        actual = getattr(candidate.scope, name)
        if expected and actual and str(expected) != str(actual):
            return False
    return True


def _condition_matches(advertised: ConditionalOffer, candidate: ConditionalOffer) -> bool:
    """Require authoritative evidence to correspond to the announced condition."""
    if not _scope_matches(advertised, candidate):
        return False
    announced = _normalized(advertised.condition_label or advertised.raw_text)
    applied = _normalized(candidate.condition_label or candidate.raw_text)
    if announced and applied and (announced in applied or applied in announced):
        return True
    if advertised.coupon_code:
        return _normalized(advertised.coupon_code) in applied
    if advertised.payment_method:
        return advertised.payment_method.casefold() == (candidate.payment_method or "").casefold()
    return (
        advertised.condition_type == candidate.condition_type
        and advertised.eligibility == candidate.eligibility
        and bool(announced and applied)
    )


def match_authoritative_offer(
    advertised: ConditionalOffer,
    candidates: list[ConditionalOffer],
) -> list[ConditionalOffer]:
    """Return only matching allocations, retaining the advertised stable key."""
    matched = []
    for candidate in candidates:
        if candidate.state != "confirmed" or candidate.source.authority != "authoritative":
            continue
        if not _condition_matches(advertised, candidate):
            continue
        matched.append(candidate.model_copy(update={"offer_key": advertised.offer_key}))
    return matched


async def _read_payload(response: Any) -> Any:
    if isinstance(response, Mapping):
        return response.get("payload", response.get("json", response))
    if hasattr(response, "json"):
        value = response.json()
        return await value if inspect.isawaitable(value) else value
    return None


def _response_status(response: Any) -> int:
    if isinstance(response, Mapping):
        return int(response.get("status", 200))
    return int(getattr(response, "status", 0))


class AnonymousConfirmationTransport:
    """Creates and destroys a cookie-isolated session for every confirmation."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., Any] | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._session_factory = session_factory or aiohttp.ClientSession
        self.timeout_seconds = timeout_seconds

    def _new_session(self) -> Any:
        kwargs = {
            "cookie_jar": aiohttp.CookieJar(unsafe=False),
            "timeout": aiohttp.ClientTimeout(total=self.timeout_seconds),
            "headers": {"Accept": "application/json"},
        }
        try:
            return self._session_factory(**kwargs)
        except TypeError:
            # Small fake factories used by hermetic tests may accept no kwargs.
            return self._session_factory()

    @staticmethod
    def _validate_public_inputs(values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in _FORBIDDEN_INPUT_PARTS):
                raise ValueError("user identity, credentials and address data are forbidden")
            if isinstance(value, Mapping):
                AnonymousConfirmationTransport._validate_public_inputs(value)

    async def _post(
        self,
        *,
        platform: str,
        brand_domain: str,
        payload: dict[str, Any],
    ) -> tuple[int, Any]:
        normalized_platform = platform.casefold()
        try:
            method, path = _ALLOWED_TARGETS[normalized_platform]
        except KeyError as exc:
            raise ValueError("unsupported anonymous confirmation platform") from exc
        self._validate_public_inputs(payload)
        target = build_probe_endpoint(brand_domain=brand_domain, path=path)
        allowed_host = urlparse(target).hostname or ""
        validate_promotion_target(target, allowed_host=allowed_host)

        session = self._new_session()
        try:
            response = await session.request(
                method,
                target,
                json=payload,
                allow_redirects=False,
                timeout=self.timeout_seconds,
            )
            status = _response_status(response)
            if 300 <= status < 400:
                return status, None
            return status, await _read_payload(response)
        finally:
            close = getattr(session, "close", None)
            if close is not None:
                value = close()
                if inspect.isawaitable(value):
                    await value

    async def confirm_vtex(
        self,
        *,
        brand_domain: str,
        advertised: ConditionalOffer,
        sku_id: str,
        seller_id: str = "1",
        quantity: int = 1,
    ) -> ConditionalOfferResult:
        if quantity < 1:
            raise ValueError("confirmation quantity must be positive")
        payload = {"items": [{"id": str(sku_id), "quantity": quantity, "seller": str(seller_id)}]}
        return await self._confirm(
            platform="vtex", brand_domain=brand_domain, advertised=advertised, payload=payload
        )

    async def confirm_shopify(
        self,
        *,
        brand_domain: str,
        advertised: ConditionalOffer,
        variant_id: str,
        quantity: int = 1,
    ) -> ConditionalOfferResult:
        if quantity < 1:
            raise ValueError("confirmation quantity must be positive")
        payload = {"items": [{"id": str(variant_id), "quantity": quantity}]}
        return await self._confirm(
            platform="shopify", brand_domain=brand_domain, advertised=advertised, payload=payload
        )

    async def _confirm(
        self,
        *,
        platform: str,
        brand_domain: str,
        advertised: ConditionalOffer,
        payload: dict[str, Any],
    ) -> ConditionalOfferResult:
        if advertised.state not in {"advertised", "requires_auth"}:
            return ConditionalOfferResult(
                status="none_detected",
                probe_level="confirm",
                diagnostics=["confirmation skipped: no advertised evidence"],
            )
        try:
            status, response_payload = await self._post(
                platform=platform, brand_domain=brand_domain, payload=payload
            )
        except (asyncio.TimeoutError, TimeoutError):
            return ConditionalOfferResult(
                status="error", probe_level="confirm", diagnostics=["confirmation timed out"]
            )
        except ValueError:
            raise
        except Exception as exc:
            return ConditionalOfferResult(
                status="error",
                probe_level="confirm",
                diagnostics=[f"confirmation failed: {type(exc).__name__}"],
            )

        source = _ALLOWED_TARGETS[platform][1]
        if status == 401:
            return ConditionalOfferResult(
                status="requires_auth",
                offers=[advertised.model_copy(update={"state": "requires_auth"})],
                probe_level="confirm",
                sources_attempted=[source],
                diagnostics=["http_status=401"],
            )
        if status == 403:
            return ConditionalOfferResult(
                status="blocked", probe_level="confirm", sources_attempted=[source], diagnostics=["http_status=403"]
            )
        if status == 429 or status >= 500 or status < 200 or status >= 300:
            return ConditionalOfferResult(
                status="error", probe_level="confirm", sources_attempted=[source], diagnostics=[f"http_status={status}"]
            )
        if not isinstance(response_payload, dict):
            return ConditionalOfferResult(
                status="error", probe_level="confirm", sources_attempted=[source], diagnostics=["unexpected confirmation schema"]
            )

        if platform == "vtex":
            from services.promotions.adapters.vtex import parse_checkout

            parsed = parse_checkout(response_payload)
        else:
            from services.promotions.adapters.shopify import parse_cart

            parsed = parse_cart(response_payload)
        matched = match_authoritative_offer(advertised, parsed)
        return ConditionalOfferResult(
            status="found" if matched else "none_detected",
            offers=matched,
            probe_level="confirm",
            sources_attempted=[source],
            diagnostics=[] if matched else ["authoritative response had no matching allocation"],
        )

