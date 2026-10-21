"""Conservative URL normalization utilities.

Used for deduplication in the price monitor (D-08) and by the brand-identify
endpoint so that URLs differing only by tracking params map to the same key,
while URLs differing by SKU query params remain distinct.

No external dependencies — stdlib only.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

logger = logging.getLogger(__name__)

_TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "msclkid",
    "dclid",
})


def normalize_url(url: str) -> str:
    """Return a canonical form of *url* for deduplication purposes.

    Rules (D-08):
    - Strips leading/trailing whitespace.
    - Forces scheme to ``https``.
    - Lowercases the host and removes a leading ``www.`` **literal prefix**
      (slices off the exact 4-char prefix ``"www."`` — does NOT use
      ``str.lstrip`` which strips a char-set and corrupts hosts like
      ``wwww.example.com``).
    - Returns *url* unchanged if the normalised host is empty (malformed input).
    - Strips a trailing ``/`` from the path (path is ``"/"`` if empty).
    - Drops query params whose key is in :data:`_TRACKING_PARAMS` **or**
      whose lower-cased key starts with ``utm_`` (composite + prefix check).
    - Preserves all other path and query components so that distinct SKUs
      (e.g. ``?skuId=1`` vs ``?skuId=2``) are NOT merged.
    """
    parsed = urlparse(url.strip())
    scheme = "https"

    # Lowercase the netloc and strip the literal "www." prefix safely.
    netloc_lower = parsed.netloc.lower()
    if netloc_lower.startswith("www."):
        host = netloc_lower[len("www."):]
    else:
        host = netloc_lower

    if not host:
        # Malformed or relative URL — return unchanged to avoid silent data loss.
        return url

    path = parsed.path.rstrip("/") or "/"

    filtered_qs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query = urlencode(filtered_qs)

    return urlunparse((scheme, host, path, "", query, ""))
