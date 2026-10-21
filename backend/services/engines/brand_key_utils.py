"""
Canonical brand-key normalization shared across engine resolution.

A single source of truth for converting a brand_key (as stored in brands.json,
e.g. "mercado_livre", or as typed by a user, e.g. "Mercado Livre") into the
canonical engine key used by the factory and the cross-marketplace _ENGINE_MAP
(e.g. "mercadolivre").

Previously this transform lived inline in EngineFactory.get_engine while
CrossMarketplaceService._active_engines compared raw brand_keys without
normalizing — so the production brand_key "mercado_livre" never matched the
_ENGINE_MAP key "mercadolivre" and Mercado Livre was silently excluded from
every per-SKU search. Centralizing kills that divergence.
"""


def normalize_brand_key(key: str) -> str:
    """Return the canonical engine key for a brand_key.

    Lowercases and strips spaces and underscores so the marketplace variants
    ("Mercado Livre", "mercado_livre", "mercadolivre") all collapse to the
    single canonical key "mercadolivre".
    """
    return key.lower().replace(" ", "").replace("_", "")
