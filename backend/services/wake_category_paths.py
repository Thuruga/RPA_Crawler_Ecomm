"""Canonical category paths for Wake storefronts with legacy mappings.

Richards has both men's and women's collections at short, unscoped paths such
as ``/calcas``. Older seed and database records used those paths even though
the official men's navigation uses ``/masculino/calcas``. Keep the correction
at the source-path boundary so persisted legacy records and direct scans use
the same collection.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


_RICHARDS_MENS_PATHS = {
    "/acessorios": "/masculino/acessorios",
    "/bermudas": "/masculino/bermudas",
    "/blazers": "/masculino/blazers",
    "/calcados": "/masculino/calcados",
    "/calcas": "/masculino/calcas",
    "/camisas": "/masculino/camisas",
    "/intimo": "/masculino/intimo",
    "/jaquetas": "/masculino/jaquetas",
    "/moletom": "/masculino/moletons",
    "/polos": "/masculino/polos",
    "/praia": "/masculino/praia",
    "/shorts": "/masculino/shorts",
    "/camisetas": "/masculino/t-shirts",
    # Some durable mappings stored the canonical slug instead of the source
    # path. They describe the same official collections and must collapse
    # with the navigation-derived entries before reaching the frontend.
    "/outlet": "/sale/masculino",
    "/t-shirts": "/masculino/t-shirts",
    "/tricos-e-blusas": "/masculino/tricots-e-blusas",
    "/tricot": "/masculino/tricots-e-blusas",
}

_ARAMIS_MENS_PATHS = {
    "/roupas/camisas": "/camisa-masculina",
    "/camisas": "/camisa-masculina",
    "/roupas/camisetas": "/camiseta-masculina",
    "/camisetas": "/camiseta-masculina",
    "/roupas/polos": "/polo-masculina",
    "/polos": "/polo-masculina",
    "/roupas/calcas": "/calca-masculina",
    "/calcas": "/calca-masculina",
    "/roupas/bermudas-e-shorts": "/bermuda-masculina",
    "/bermudas": "/bermuda-masculina",
    "/roupas/jaquetas": "/jaqueta-masculina",
    "/jaquetas": "/jaqueta-masculina",
    "/roupas/moletom": "/moletom-masculino",
    "/moletom": "/moletom-masculino",
    "/roupas/tricots": "/tricot-masculino",
    "/tricots": "/tricot-masculino",
    "/roupas/alfaiataria": "/alfaiataria-masculina",
    "/alfaiataria": "/alfaiataria-masculina",
    "/roupas/beachwear": "/beachwear-masculino",
    "/beachwear": "/beachwear-masculino",
}


def canonicalize_wake_category_source(brand_key: str, source: str | None) -> str:
    """Return the official Wake category path while preserving complete URLs.

    Richards and Aramis have legacy paths mapped to their official Wake collections.
    Other Wake brands are intentionally untouched: an unscoped path can be
    legitimate for a single-gender storefront.
    """
    value = str(source or "").strip()
    key = brand_key.casefold().strip()
    if not value or key not in ("richards", "aramis"):
        return value

    mapping = _RICHARDS_MENS_PATHS if key == "richards" else _ARAMIS_MENS_PATHS
    parsed = urlsplit(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    normalized_path = "/" + path.strip("/") if path.strip("/") else ""
    canonical_path = mapping.get(normalized_path)
    if not canonical_path:
        return value

    if parsed.scheme or parsed.netloc:
        return urlunsplit((parsed.scheme, parsed.netloc, canonical_path, parsed.query, parsed.fragment))
    return canonical_path
