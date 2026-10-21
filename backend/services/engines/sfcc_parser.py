"""
SFCC (Salesforce Commerce Cloud / Demandware) parser utilities.

Pure-Python extraction logic for public browser-rendered SFCC pages:
  - JSON-LD Product/ProductGroup (primary source)
  - OpenGraph meta tags (supplementary)
  - BR-anchored text fallback for price (`R$ X.XXX,XX`)

This module is intentionally thin and side-effect-free so that price parsing
and PDP extraction can be unit-tested without any browser mocking.

Security mitigations (T-31-01, T-31-02):
  - HTML is only parsed via BeautifulSoup `.get_text()` — no eval/exec.
  - JSON-LD is parsed via `json.loads` as a data structure, never executed.
  - `_BR_MONEY_RE` requires the `R$` currency prefix; bare accessibility-text
    numbers (e.g. "5 out of 5 stars", "208 reviews") never match.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from services.attribute_parsing import extract_color_fallback, extract_size_fallback
from services.sanitization import sanitize_colors, sanitize_sizes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BR money regex — D-02, T-31-02
# Requires the `R$` prefix so that bare integers and accessibility numbers
# (star ratings, review counts, etc.) are never interpreted as prices.
# ---------------------------------------------------------------------------
_BR_MONEY_RE = re.compile(
    r"R\$\s*([\d]{1,3}(?:\.[\d]{3})*,[\d]{2}|[\d]+,[\d]{2})"
)


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def parse_price_br(text: Any) -> Optional[float]:
    """
    Parse a Brazilian Real price string to float.

    Handles:
      - "R$ 1.234,56"  -> 1234.56
      - "R$ 119,00"    -> 119.0
      - 1234.56        -> 1234.56  (numeric passthrough for JSON-LD plain floats)
      - 119            -> 119.0    (int passthrough)

    Returns None for:
      - US-format prices: "$119.00"
      - Bare accessibility numbers: "5 out of 5 stars", "208 reviews"
      - Negative or zero values
    """
    if isinstance(text, (int, float)):
        value = float(text)
        return value if value > 0 else None

    if not isinstance(text, str):
        return None

    match = _BR_MONEY_RE.search(text)
    if not match:
        return None

    raw = match.group(1)                       # e.g. "1.234,56"
    normalized = raw.replace(".", "").replace(",", ".")  # "1234.56"
    try:
        value = float(normalized)
    except ValueError:
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# JSON-LD extraction — Pattern 4
# ---------------------------------------------------------------------------

def extract_jsonld_products(html: str) -> List[Dict[str, Any]]:
    """
    Extract Product and ProductGroup JSON-LD blocks from rendered HTML.

    Returns a list of dicts with `@type` in ("Product", "ProductGroup").
    Malformed JSON blocks are silently skipped (T-31-01: swallow
    JSONDecodeError/TypeError so a single bad block does not fail the page).
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") in ("Product", "ProductGroup"):
                    results.append(item)
        elif isinstance(data, dict) and data.get("@type") in ("Product", "ProductGroup"):
            results.append(data)
    return results


# ---------------------------------------------------------------------------
# OpenGraph extraction
# ---------------------------------------------------------------------------

def extract_og_meta(html: str) -> Dict[str, str]:
    """
    Extract all `og:*` meta properties from rendered HTML.

    Returns a dict like {"og:title": "...", "og:image": "...", ...}.
    """
    soup = BeautifulSoup(html, "html.parser")
    og: Dict[str, str] = {}
    for tag in soup.find_all("meta", property=True):
        prop = tag.get("property", "")
        if prop.startswith("og:"):
            content = tag.get("content", "")
            if content:
                og[prop] = content
    return og


# ---------------------------------------------------------------------------
# Offer helpers
# ---------------------------------------------------------------------------

def offer_from(product: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the first offer dict from a JSON-LD Product/ProductGroup.

    `offers` may be a single dict or a list of dicts.  Returns an empty
    dict if no offer is present.
    """
    offers = product.get("offers")
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def extract_price(
    offer: Dict[str, Any],
    og_meta: Dict[str, str],
    visible_text: str = "",
) -> Optional[float]:
    """
    Extract price using the 3-layer strategy from CRQ-1:

    1. JSON-LD `offers.price` / `offers.lowPrice` (plain float preferred)
    2. OpenGraph `og:product:price:amount`
    3. `parse_price_br(visible_text)` (BR text regex fallback)

    Each layer is tried in order; the first successful non-zero result wins.
    """
    # Layer 1: JSON-LD offer
    for key in ("price", "lowPrice"):
        raw = offer.get(key)
        if raw is not None:
            try:
                value = float(raw)
                if value > 0:
                    return value
            except (ValueError, TypeError):
                # Possibly a localized string "1.234,56" — fall through to BR regex
                br_value = parse_price_br(str(raw))
                if br_value:
                    return br_value

    # Layer 2: OpenGraph product price
    og_amount = og_meta.get("og:product:price:amount")
    if og_amount:
        try:
            value = float(og_amount)
            if value > 0:
                return value
        except (ValueError, TypeError):
            br_value = parse_price_br(og_amount)
            if br_value:
                return br_value

    # Layer 3: BR text regex on visible rendered text
    if visible_text:
        return parse_price_br(visible_text)

    return None


# ---------------------------------------------------------------------------
# Availability parsing
# ---------------------------------------------------------------------------

def parse_availability(value: Any) -> Optional[bool]:
    """
    Map JSON-LD / OpenGraph availability strings to bool.

    InStock → True, OutOfStock / SoldOut → False, unknown → None.
    """
    if value is None:
        return None
    text = str(value).lower()
    if not text:
        return None
    if "instock" in text or "in stock" in text:
        return True
    if "outofstock" in text or "out of stock" in text or "sold out" in text:
        return False
    return None


def parse_aggregate_rating(product_ld: Dict[str, Any]) -> tuple[Optional[float], Optional[int]]:
    """Extract rating/count from JSON-LD aggregateRating."""
    aggregate = product_ld.get("aggregateRating")
    if not isinstance(aggregate, dict):
        return None, None
    rating = None
    count = None
    try:
        if aggregate.get("ratingValue") is not None:
            rating = round(float(str(aggregate.get("ratingValue")).replace(",", ".")), 1)
    except (TypeError, ValueError):
        rating = None
    for key in ("reviewCount", "ratingCount"):
        try:
            if aggregate.get(key) is not None:
                count = int(str(aggregate.get(key)).replace(".", ""))
                break
        except (TypeError, ValueError):
            continue
    return rating, count


# ---------------------------------------------------------------------------
# Product identity — D-03
# ---------------------------------------------------------------------------

def extract_pdp_product_code(product_ld: Dict[str, Any]) -> Optional[str]:
    """
    Extract an unambiguous product-level identifier from a JSON-LD Product/
    ProductGroup block.

    Only accepts `productID` (schema.org's canonical product-level field) or
    `@id`. Deliberately NEVER falls back to a lone `sku` — on a rendered SFCC
    PDP, `sku` commonly identifies just the currently-selected variant (one
    color/size combination), not the stable product (D-03 / threat: variant
    SKU silently promoted to product identity). Absence of an unambiguous
    identifier returns None, never an invented/guessed value.
    """
    if not product_ld:
        return None
    for key in ("productID", "@id"):
        value = product_ld.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


# ---------------------------------------------------------------------------
# Brand extraction helper
# ---------------------------------------------------------------------------

def _extract_brand(product_ld: Dict[str, Any], og_meta: Dict[str, str]) -> str:
    """Extract brand name from JSON-LD brand object or OG tags."""
    brand = product_ld.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name", "")
        if name:
            return str(name).strip()
    if isinstance(brand, str) and brand.strip():
        return brand.strip()
    return og_meta.get("og:brand", "") or og_meta.get("og:site_name", "")


def _explicit_values(value: Any) -> List[str]:
    """Copia somente strings fornecidas explicitamente pelo payload/DOM."""
    if isinstance(value, str):
        label = value.strip()
        return [label] if label else []
    if not isinstance(value, (list, tuple)):
        return []

    values: List[str] = []
    for item in value:
        if item is None:
            continue
        label = str(item).strip()
        if label:
            values.append(label)
    return values


def _jsonld_explicit_attributes(
    product_ld: Dict[str, Any],
) -> tuple[List[str], List[str], List[str]]:
    raw_colors = _explicit_values(product_ld.get("color"))
    raw_sizes = _explicit_values(product_ld.get("size"))
    raw_size_grid = list(raw_sizes)

    variants = product_ld.get("hasVariant")
    if not isinstance(variants, list):
        return raw_colors, raw_sizes, raw_size_grid

    for variant in variants:
        if not isinstance(variant, dict):
            continue
        raw_colors.extend(_explicit_values(variant.get("color")))
        variant_sizes = _explicit_values(variant.get("size"))
        raw_size_grid.extend(variant_sizes)

        availability = parse_availability(offer_from(variant).get("availability"))
        if availability is not False:
            raw_sizes.extend(variant_sizes)

    return raw_colors, raw_sizes, raw_size_grid


def _normalize_explicit_attributes(
    colors: List[str],
    sizes: List[str],
    size_grid: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """Preserva raw e transforma apenas copias canonicas de fontes explicitas."""
    raw_colors = list(colors)
    raw_sizes = list(sizes)
    raw_size_grid = list(size_grid if size_grid is not None else sizes)

    canonical_colors = [extract_color_fallback(value) for value in raw_colors]
    canonical_sizes = [extract_size_fallback(value) for value in raw_sizes]
    canonical_size_grid = [extract_size_fallback(value) for value in raw_size_grid]

    return {
        "raw_colors": raw_colors,
        "available_colors": sanitize_colors(canonical_colors),
        "raw_sizes": raw_sizes,
        "available_sizes": sanitize_sizes(canonical_sizes),
        "raw_size_grid": raw_size_grid,
        "size_grid": sanitize_sizes(canonical_size_grid),
    }



# ---------------------------------------------------------------------------
# PDP parsing
# ---------------------------------------------------------------------------

def parse_pdp(html: str, source_url: str) -> Optional[Dict[str, Any]]:
    """
    Parse a rendered SFCC PDP page into a RawProductBronze-compatible dict.

    Extraction order:
      - JSON-LD Product/ProductGroup (primary)
      - OpenGraph meta (supplementary / fallback)

    Returns None when:
      - Both JSON-LD products and OG title are absent (nothing to extract)
      - raw_title cannot be determined

    The returned dict keys map directly to RawProductBronze fields:
      url, brand, raw_title, raw_description, price_full, image_url,
      stock_availability, available_colors, available_sizes, specifications
    """
    soup = BeautifulSoup(html, "html.parser")
    og_meta = extract_og_meta(html)
    products_ld = extract_jsonld_products(html)

    # Use the first Product/ProductGroup block found
    product_ld: Dict[str, Any] = products_ld[0] if products_ld else {}

    # --- title ---
    raw_title = ""
    if product_ld:
        raw_title = str(product_ld.get("name") or "").strip()
    if not raw_title:
        raw_title = og_meta.get("og:title", "").strip()
    if not raw_title:
        return None  # Nothing to extract

    # --- offer ---
    offer = offer_from(product_ld) if product_ld else {}

    # --- price ---
    visible_text = soup.get_text(separator=" ")
    price_full = extract_price(offer, og_meta, visible_text)

    # --- image ---
    image_url: Optional[str] = None
    raw_image = product_ld.get("image") if product_ld else None
    if isinstance(raw_image, list) and raw_image:
        image_url = str(raw_image[0]).strip() or None
    elif isinstance(raw_image, str) and raw_image.strip():
        image_url = raw_image.strip()
    if not image_url:
        image_url = og_meta.get("og:image", "").strip() or None

    # --- availability ---
    availability_raw = offer.get("availability") if offer else None
    if availability_raw is None:
        availability_raw = og_meta.get("og:availability")
    stock_availability = parse_availability(availability_raw)

    # --- brand ---
    brand = _extract_brand(product_ld, og_meta)
    rating, review_count = parse_aggregate_rating(product_ld)
    raw_colors, raw_sizes, raw_size_grid = _jsonld_explicit_attributes(product_ld)
    attributes = _normalize_explicit_attributes(
        raw_colors,
        raw_sizes,
        raw_size_grid,
    )

    # --- description ---
    raw_description = ""
    if product_ld:
        raw_description = str(product_ld.get("description") or "").strip()
    if not raw_description:
        raw_description = og_meta.get("og:description", "").strip()
    if not raw_description:
        raw_description = raw_title  # fallback to title

    # Limpeza de truncamento comum no CMS SFCC (ex: "... 1933. Confec")
    if raw_description:
        import re
        raw_description = re.sub(r'\s+Confec\.?$', '.', raw_description).strip()

    # Se faltarem atributos explícitos no JSON-LD (comum no storefront SFCC da Lacoste),
    # tenta extrair de window.currentProduct se presente no HTML
    if (not raw_colors or not raw_sizes) and "window.currentProduct" in html:
        import re
        try:
            m = re.search(r'window\.currentProduct\s*=\s*(\{.*?\});\s*(?:window|\n)', html, re.DOTALL)
            if m:
                cp = json.loads(m.group(1))
                if not raw_colors:
                    color_lbl = cp.get("color", {}).get("label")
                    if color_lbl:
                        raw_colors = [str(color_lbl).strip()]
                desc_obj = cp.get("description", {})
                for d in desc_obj.get("descriptions", []):
                    items = d.get("list", [])
                    if items:
                        bullet_block = "\n".join(f"• {it}" for it in items)
                        if bullet_block not in raw_description:
                            raw_description = f"{raw_description}\n\n{bullet_block}".strip()
                        break
                attributes = _normalize_explicit_attributes(
                    raw_colors,
                    raw_sizes,
                    raw_size_grid,
                )
        except Exception:
            pass

    return {
        "url": source_url,
        "brand": brand,
        "raw_title": raw_title,
        "raw_description": raw_description,
        "price_full": price_full,
        "image_url": image_url,
        "stock_availability": stock_availability,
        **attributes,
        "rating": rating,
        "review_count": review_count,
        "specifications": {},
        # D-03: productID/@id inequivoco apenas — None quando a PDP so oferece
        # um SKU de variante (ver extract_pdp_product_code).
        "product_code": extract_pdp_product_code(product_ld),
    }


# ---------------------------------------------------------------------------
# Nav category extraction — D-05, T-31-07
# ---------------------------------------------------------------------------

# Noise terms for nav link labels and paths — mirrors ShopifyEngine.discover_categories
_NAV_NOISE_TERMS: set = {
    "account", "login", "register", "cart", "checkout", "wishlist",
    "help", "ajuda", "faq", "sitemap",
    # PT-BR equivalents
    "conta", "entrar", "cadastro", "sacola", "cesta", "favoritos",
    "politica", "política", "termos", "contato", "sobre", "blog",
    "sustentabilidade", "responsabilidade", "imprensa", "press",
    "stores", "lojas", "store-locator", "encontrar-loja",
    "sale", "outlet", "gift-card", "giftcard",
}


def extract_nav_categories(html: str, base_domain: str) -> List[Dict[str, Any]]:  # noqa: ARG001 — base_domain reserved for absolute-URL resolution in future callers
    """
    Extract navigation category links from rendered SFCC home/menu HTML.

    Locates the first ``<nav>`` element or element with ``role="navigation"``.
    For each ``<a href>`` under that element:
      - Keeps only same-domain relative hrefs (``href.startswith("/")``)
      - Filters labels shorter than 3 characters
      - Filters noise terms (account/login/cart/help/politica/conta …) from both
        label and path (T-31-07: label extracted via ``.get_text(strip=True)`` only)
      - Deduplicates by path (first-seen wins, order preserved)

    Returns a list of ``{"name": label, "path": href, "id": href}`` dicts.
    Returns ``[]`` when no nav element is found.

    This function is pure (no network, no BrowserManager) so it can be tested
    without any browser mocking overhead.

    Security: T-31-07 — ``.get_text(strip=True)`` extracts label text only
    (no markup executed); only ``startswith("/")`` hrefs are kept (external and
    ``javascript:`` hrefs are silently dropped).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Locate nav container — try <nav> first, then role=navigation fallback
    nav = soup.find("nav") or soup.find(attrs={"role": "navigation"})
    if not nav:
        return []

    seen: set = set()
    result: List[Dict[str, Any]] = []

    for a_tag in nav.find_all("a", href=True):
        href: str = a_tag["href"]

        # T-31-07: only same-domain relative paths (drops external + javascript: + mailto:)
        if not href.startswith("/"):
            continue

        # T-31-07: label via get_text only — no markup execution
        label: str = a_tag.get_text(strip=True)

        # Filter: too-short labels (single icons, hidden spans, etc.)
        if len(label) <= 2:
            continue

        # Noise filter: check label words and path segments against noise set
        label_lower = label.lower()
        path_segments = set(href.lower().strip("/").split("/"))
        if any(term in label_lower for term in _NAV_NOISE_TERMS):
            continue
        if path_segments & _NAV_NOISE_TERMS:
            continue

        # Dedup by path (preserve first-seen order)
        if href in seen:
            continue
        seen.add(href)

        result.append({"name": label, "path": href, "id": href})

    return result


# ---------------------------------------------------------------------------
# Search results URL extraction
# ---------------------------------------------------------------------------

def _absolutize(url: str, base_domain: str) -> str:
    """Normaliza um href/src para URL https absoluta.

    Cobre as três formas que páginas SFCC emitem:
      - protocol-relative ``//host/path`` → ``https://host/path``  (corrige o bug do ``//``
        que gerava ``https://www.lacoste.com.br//www.lacoste.com/br/...``)
      - root-relative      ``/path``       → ``https://www.<domain>/path``
      - já absoluta         ``https://...`` → inalterada
    """
    if not url:
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("/"):
        host = base_domain if base_domain.startswith("www.") else f"www.{base_domain}"
        return f"https://{host}{url}"
    return url


def _looks_like_pdp_url(href: str, base_domain: str) -> bool:
    """
    Heuristic: is this href likely a product detail page (PDP)?

    Criteria:
    - Must be on the same domain (or a relative path on that domain)
    - Must have a non-trivial path (not just "/" or a nav anchor)
    - Must not look like a category listing, checkout, or account page
    - Must contain at least one path segment that looks like a product slug

    This heuristic is intentionally permissive in Wave 0 — false positives
    (category pages that slip through) are filtered at the PDP enrichment stage
    by the Quality Gate (missing title/price/image).
    """
    if not href:
        return False

    # Resolve relative URLs against base domain
    if href.startswith("/"):
        parsed = urlparse(f"https://{base_domain}{href}")
    elif href.startswith("http"):
        parsed = urlparse(href)
        # Must be same domain (or www. prefix variant)
        link_host = parsed.netloc.lstrip("www.")
        canon_host = base_domain.lstrip("www.")
        if link_host != canon_host:
            return False
    else:
        return False  # Relative fragment, javascript:, mailto:, etc.

    path = parsed.path.rstrip("/")
    if not path or path in ("/", ""):
        return False

    # Exclude common non-PDP paths
    non_pdp_segments = {
        "search", "busca", "checkout", "cart", "account",
        "login", "register", "wishlist", "sitemap",
        "on", "static", "cdn", "assets",
    }
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    # If first segment is a known non-PDP section, skip
    if segments[0].lower() in non_pdp_segments:
        return False

    # Require at least 1 path segment with a product-like slug (contains alphanumeric + hyphen)
    product_like = re.compile(r"^[a-zA-Z0-9][\w\-]+$")
    has_product_slug = any(product_like.match(s) and len(s) > 3 for s in segments)
    if not has_product_slug:
        return False

    return True


def parse_search_results(html: str, base_domain: str) -> List[str]:
    """
    Extract candidate PDP URLs from a rendered SFCC search/category page.

    Returns a deduped list of absolute URLs that pass the `_looks_like_pdp_url`
    heuristic. This is the discovery phase — false positives are acceptable since
    the PDP enrichment Quality Gate will filter products missing required fields.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set = set()
    results: List[str] = []

    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag["href"]
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        # Normaliza ANTES de testar (cobre protocol-relative //host/path) — corrige o bug
        # que gerava URLs com // e mascarava links de nav como PDP. A heurística passa a
        # operar sobre a URL absoluta.
        absolute = _absolutize(href, base_domain)

        if not _looks_like_pdp_url(absolute, base_domain):
            continue

        # Strip query strings for dedup purposes (keep the canonical PDP URL)
        canonical = absolute.split("?")[0].rstrip("/")
        if canonical not in seen:
            seen.add(canonical)
            results.append(canonical)

    return results


# ---------------------------------------------------------------------------
# Search results — direct tile extraction (preferred over PDP enrichment)
# ---------------------------------------------------------------------------

def parse_search_tiles(html: str, base_domain: str, brand: str = "") -> List[Dict[str, Any]]:
    """
    Extrai produtos DIRETO dos tiles da página de busca/categoria SFCC.

    Cada tile (``div[data-producttile="true"]`` / ``div.product-tile[data-pid]``) já
    carrega título, URL absoluta da PDP, preço de venda, imagem e estoque — então o
    contrato catálogo+preço é satisfeito SEM uma navegação por PDP. Menos requisições
    = menos exposição a anti-bot (D-13) e menor latência que o enriquecimento por PDP.

    Retorna dicts no mesmo formato de :func:`parse_pdp` (compatível com RawProductBronze):
    ``url, brand, raw_title, raw_description, price_full, image_url, stock_availability,
    available_colors, available_sizes, specifications``.

    Função pura (sem rede/sem BrowserManager) — testável contra um fixture capturado.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict[str, Any]] = []
    seen: set = set()

    tiles = soup.select('div[data-producttile="true"]') or soup.select("div.product-tile[data-pid]")
    for tile in tiles:
        # --- título + url (h2 do título; fallback p/ link overlay do tile) ---
        link = tile.select_one("h2 a[href]") or tile.select_one("a.js-product-tile-link[href]")
        if not link:
            continue
        url = _absolutize((link.get("href") or "").strip(), base_domain)
        if not url:
            continue
        raw_title = link.get_text(strip=True)
        if not raw_title:
            img_alt = tile.select_one("img[alt]")
            raw_title = (img_alt.get("alt") or "").strip() if img_alt else ""
        if not raw_title:
            continue

        # --- preço: sales-price preferida; fallback p/ qualquer R$ no tile ---
        price_full: Optional[float] = None
        sales = tile.select_one(".sales-price")
        if sales:
            price_full = parse_price_br(sales.get_text(" ", strip=True))
        if price_full is None:
            price_full = parse_price_br(tile.get_text(" ", strip=True))

        # --- imagem (protocol-relative //imagesa1... → https:) ---
        image_url: Optional[str] = None
        img = tile.select_one("img.js-img[src]") or tile.select_one("img[src]")
        if img and img.get("src"):
            image_url = _absolutize(img["src"].strip(), base_domain)

        # --- disponibilidade via data-out-of-stock ---
        if tile.has_attr("data-out-of-stock"):
            stock_availability: Optional[bool] = tile["data-out-of-stock"].strip().lower() != "true"
        else:
            stock_availability = None

        # dedup por URL canônica (ignora ?color=)
        canonical = url.split("?")[0].rstrip("/")
        if canonical in seen:
            continue
        seen.add(canonical)

        # --- product_code: data-pid do tile (D-03) ---
        # O data-pid do tile é o identificador de produto nativo do SFCC —
        # nunca inferido/derivado de título ou URL.
        raw_pid = tile.get("data-pid")
        product_code = str(raw_pid).strip() or None if raw_pid else None

        raw_colors = _explicit_values(
            tile.get("data-color") or tile.get("data-colors")
        )
        raw_sizes = _explicit_values(
            tile.get("data-size") or tile.get("data-sizes")
        )
        attributes = _normalize_explicit_attributes(raw_colors, raw_sizes)


        results.append({
            "url": url,
            "brand": brand,
            "raw_title": raw_title,
            "raw_description": raw_title,
            "price_full": price_full,
            "image_url": image_url,
            "stock_availability": stock_availability,
            **attributes,
            "specifications": {},
            "product_code": product_code,
        })

    return results
