"""Bounded in-memory caches for global rules and per-product offer results."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


CACHE_VERSION = "v1"
RULE_TTL_SECONDS = 15 * 60
PRODUCT_POSITIVE_TTL_SECONDS = 10 * 60
PRODUCT_NEGATIVE_TTL_SECONDS = 3 * 60
EXPENSIVE_TTL_SECONDS = 20 * 60


@dataclass
class _Entry:
    value: Any
    expires_at: float


def normalize_product_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    path = "/" + "/".join(part for part in parsed.path.split("/") if part)
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
        )
    )
    return urlunsplit(("https", host + port, path or "/", query, ""))


def make_rule_key(*, brand: str, locale: str = "pt-BR", channel: str = "default", rule: str = "default") -> str:
    return f"promotion-rule:{CACHE_VERSION}:{brand.casefold()}:{locale}:{channel}:{rule}"


def make_product_key(
    *,
    brand: str,
    url: str,
    level: str,
    sku_id: str | None = None,
    seller_id: str | None = None,
    condition: str | None = None,
    rule: str | None = None,
    locale: str = "pt-BR",
    channel: str = "default",
) -> str:
    values = (
        brand.casefold(), locale, channel, normalize_product_url(url), sku_id or "-",
        seller_id or "-", condition or "-", rule or "-", level,
    )
    return "promotion-product:" + CACHE_VERSION + ":" + "|".join(values)


class PromotionCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._rules: dict[str, _Entry] = {}
        self._products: dict[str, _Entry] = {}

    def _get(self, entries: dict[str, _Entry], key: str, *, allow_stale: bool) -> tuple[Any | None, bool]:
        entry = entries.get(key)
        if entry is None:
            return None, False
        stale = entry.expires_at <= self.clock()
        if stale and not allow_stale:
            return None, True
        return entry.value, stale

    def get_rule(self, key: str, *, allow_stale: bool = False) -> tuple[Any | None, bool]:
        return self._get(self._rules, key, allow_stale=allow_stale)

    def set_rule(self, key: str, value: Any, *, ttl: float = RULE_TTL_SECONDS) -> None:
        self._rules[key] = _Entry(value=value, expires_at=self.clock() + ttl)

    def get_product(self, key: str, *, allow_stale: bool = False) -> tuple[Any | None, bool]:
        return self._get(self._products, key, allow_stale=allow_stale)

    def set_product(self, key: str, value: Any, *, ttl: float) -> None:
        self._products[key] = _Entry(value=value, expires_at=self.clock() + ttl)

    def invalidate_product(self, key: str) -> None:
        self._products.pop(key, None)

    def invalidate_brand(self, brand: str) -> None:
        needle = brand.casefold()
        self._rules = {key: value for key, value in self._rules.items() if needle not in key}
        self._products = {key: value for key, value in self._products.items() if needle not in key}

