"""Wake token override helpers.

Committed brand data must not carry storefront tokens. Runtime deployments can
provide them through environment variables keyed by brand.
"""

from __future__ import annotations

import os
import re
from typing import Any


def resolve_wake_access_token_override(brand: Any) -> str | None:
    """Resolve a Wake token from environment variables for a brand."""
    for env_name in _candidate_env_names(brand):
        value = os.getenv(env_name)
        if value and value.strip():
            return value.strip()
    return None


def _candidate_env_names(brand: Any) -> list[str]:
    candidates: list[str] = []
    for suffix in _brand_suffixes(brand):
        candidates.extend(
            [
                f"WAKE_ACCESS_TOKEN_{suffix}",
                f"WAKE_{suffix}_ACCESS_TOKEN",
                f"{suffix}_WAKE_ACCESS_TOKEN",
            ]
        )
    candidates.append("WAKE_ACCESS_TOKEN")
    return list(dict.fromkeys(candidates))


def _brand_suffixes(brand: Any) -> list[str]:
    values = [
        _get_field(brand, "brand_key"),
        _get_field(brand, "brand_name"),
        _get_field(brand, "domain"),
    ]
    suffixes: list[str] = []
    for value in values:
        if not value:
            continue
        normalized = str(value).replace("https://", "").replace("http://", "")
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").upper()
        if suffix:
            suffixes.append(suffix)
    return list(dict.fromkeys(suffixes))


def _get_field(brand: Any, key: str) -> Any:
    if isinstance(brand, dict):
        return brand.get(key)
    return getattr(brand, key, None)
