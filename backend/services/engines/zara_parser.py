"""Pure parsers for Zara/Inditex rendered pages.

Zara BR pages expose useful product data in two shapes:
  - category pages: JSON-LD ItemList with product name, image, URL and price.
  - search pages: rendered product tiles with title, URL, prices and image.

This module has no network or browser dependency so it can be tested with small
fixtures. The engine is responsible for fetching rendered HTML.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup


_BR_MONEY_RE = re.compile(
    r"R\$\s*([\d]{1,3}(?:\.[\d]{3})*,[\d]{2}|[\d]+,[\d]{2})"
)


def parse_price_br(value: Any) -> Optional[float]:
    """Parse Brazilian Real prices from Zara HTML/JSON-LD."""
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else None

    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    try:
        parsed = float(stripped)
        return parsed if parsed > 0 else None
    except ValueError:
        pass

    match = _BR_MONEY_RE.search(stripped)
    if not match:
        return None

    normalized = match.group(1).replace(".", "").replace(",", ".")
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _absolute_url(url: str, base_url: str = "https://www.zara.com") -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin(base_url, url)


def _first_offer(product: Dict[str, Any]) -> Dict[str, Any]:
    offers = product.get("offers")
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def _aggregate_rating(product: Dict[str, Any]) -> tuple[Optional[float], Optional[int]]:
    raw = product.get("aggregateRating")
    if not isinstance(raw, dict):
        return None, None
    rating = None
    count = None
    try:
        if raw.get("ratingValue") is not None:
            rating = round(float(str(raw.get("ratingValue")).replace(",", ".")), 1)
    except (TypeError, ValueError):
        rating = None
    for key in ("reviewCount", "ratingCount"):
        try:
            if raw.get(key) is not None:
                count = int(str(raw.get(key)).replace(".", ""))
                break
        except (TypeError, ValueError):
            continue
    return rating, count


def _product_dict(
    *,
    name: str,
    url: str,
    image_url: Optional[str],
    price_full: Optional[float],
    brand: str,
    price_discount: Optional[float] = None,
    available: Optional[bool] = None,
    product_code: Optional[str] = None,
    available_colors: Optional[List[str]] = None,
    available_sizes: Optional[List[str]] = None,
    size_grid: Optional[List[str]] = None,
    raw_description: Optional[str] = None,
    composition: Optional[str] = None,
    specifications: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    name = (name or "").strip()
    url = (url or "").strip()
    if not name or not url or not price_full or not image_url:
        return None

    colors = list(available_colors) if available_colors else []
    sizes = list(available_sizes) if available_sizes else []
    # Sem grade explícita (ex.: listagem sem hasVariant), a grade colapsa no
    # que já temos em available_sizes — nunca inventa tamanhos além disso.
    grid = list(size_grid) if size_grid else list(sizes)

    final_description = (raw_description or "").strip() or name

    return {
        "url": url,
        "brand": brand,
        "raw_title": name,
        "raw_description": final_description,
        "price_full": price_full,
        "price_discount": price_discount,
        "promotions": [],
        "stock_availability": available,
        "image_url": image_url,
        "available_colors": colors,
        "available_sizes": sizes,
        # D-01: mesma extração explícita usada em available_colors/available_sizes
        # (hasVariant.color/size), nunca inferida de nome/slug/título.
        "raw_colors": list(colors),
        "raw_sizes": list(sizes),
        "size_grid": grid,
        "raw_size_grid": list(grid),
        "composition": composition.strip() if composition else None,
        "specifications": specifications or {},
        "product_code": product_code,
    }


def _extract_variant_attributes(
    block: Dict[str, Any],
) -> tuple[List[str], List[str], List[str]]:
    """Extract raw colors/size-grid/available-sizes from ProductGroup.hasVariant (D-01).

    Only reads the schema.org `color`/`size`/`offers.availability` properties on
    each variant when explicitly present — never inferred from name/slug/title.
    Absence of `hasVariant` returns ([], [], []).

    Returns (colors, size_grid, available_sizes):
      - size_grid: every size across all variants, regardless of stock.
      - available_sizes: subset of size_grid whose variant offer availability
        resolves to schema.org InStock. A variant without a parseable
        `offers.availability` is treated as available (fail-open — matches the
        prior behavior where every variant size was reported as available).
    """
    colors: List[str] = []
    size_grid: List[str] = []
    available_sizes: List[str] = []
    variants = block.get("hasVariant")
    if not isinstance(variants, list):
        return colors, size_grid, available_sizes
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        color = variant.get("color")
        if color is not None:
            text = str(color).strip()
            if text and text not in colors:
                colors.append(text)
        size = variant.get("size")
        if size is None:
            continue
        text = str(size).strip()
        if not text:
            continue
        if text not in size_grid:
            size_grid.append(text)
        offer = _first_offer(variant)
        availability = str(offer.get("availability") or "").strip().lower()
        is_available = ("outofstock" not in availability) if availability else True
        if is_available and text not in available_sizes:
            available_sizes.append(text)
    return colors, size_grid, available_sizes


def _jsonld_blocks(soup: BeautifulSoup) -> Iterable[Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            yield json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue


def parse_itemlist_products(
    html: str,
    *,
    brand: str = "Zara",
    base_url: str = "https://www.zara.com",
) -> List[Dict[str, Any]]:
    """Extract products from Zara category ItemList JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")
    products: List[Dict[str, Any]] = []

    for block in _jsonld_blocks(soup):
        if not isinstance(block, dict) or block.get("@type") != "ItemList":
            continue

        for entry in block.get("itemListElement", []):
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            if not isinstance(item, dict):
                continue

            offer = _first_offer(item)
            url = (
                offer.get("url")
                or item.get("url")
                or ""
            )
            image = item.get("image")
            if isinstance(image, list):
                image_url = str(image[0]).strip() if image else None
            else:
                image_url = str(image).strip() if image else None

            product = _product_dict(
                name=str(item.get("name") or ""),
                url=_absolute_url(str(url), base_url),
                image_url=_absolute_url(image_url or "", base_url),
                price_full=parse_price_br(offer.get("price")),
                brand=brand,
                available=None,
                product_code=str(item.get("sku")) if item.get("sku") else None,
            )
            if product:
                rating, review_count = _aggregate_rating(item)
                product["rating"] = rating
                product["review_count"] = review_count
                products.append(product)

    return products


def _tile_prices(tile: Any) -> tuple[Optional[float], Optional[float]]:
    """Return (price_full, price_discount) from a rendered Zara tile."""
    current = None
    current_node = tile.select_one(".price-current__amount")
    if current_node:
        current = parse_price_br(current_node.get_text(" ", strip=True))

    prices: List[float] = []
    for node in tile.select(".money-amount__main"):
        parsed = parse_price_br(node.get_text(" ", strip=True))
        if parsed is not None:
            prices.append(parsed)

    if current is None and prices:
        current = prices[-1]

    unique_prices = []
    for price in prices:
        if price not in unique_prices:
            unique_prices.append(price)

    if len(unique_prices) >= 2 and current is not None:
        original = max(unique_prices)
        if original > current:
            return original, current

    return current, None


def parse_tile_products(
    html: str,
    *,
    brand: str = "Zara",
    base_url: str = "https://www.zara.com",
) -> List[Dict[str, Any]]:
    """Extract products from rendered Zara search/category tiles."""
    soup = BeautifulSoup(html, "html.parser")
    products: List[Dict[str, Any]] = []

    for tile in soup.select(".product-grid-product"):
        name_link = tile.select_one(".product-grid-product-info__name[href]")
        figure_link = tile.select_one(".product-grid-product__link[href]")
        link = name_link or figure_link
        if not link:
            continue

        title = name_link.get_text(" ", strip=True) if name_link else ""
        if not title:
            heading = tile.select_one("h3")
            title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            continue

        image_url = None
        img = tile.select_one("img[src]")
        if img and img.get("src"):
            image_url = _absolute_url(img["src"].strip(), base_url)

        price_full, price_discount = _tile_prices(tile)
        text = tile.get_text(" ", strip=True).lower()
        available = False if "esgotado" in text else True

        product = _product_dict(
            name=title,
            url=_absolute_url(link.get("href", ""), base_url),
            image_url=image_url,
            price_full=price_full,
            price_discount=price_discount,
            brand=brand,
            available=available,
            product_code=tile.get("data-productid"),
        )
        if product:
            product_id = tile.get("data-productid")
            if product_id:
                product["shipping_product_id"] = product_id
            products.append(product)

    return products


def _extract_view_payload(soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
    """Extract embedded window.zara.viewPayload if present."""
    for script in soup.find_all("script"):
        txt = script.string or ""
        if "window.zara.viewPayload" in txt:
            match = re.search(
                r"window\.zara\.viewPayload\s*=\s*(\{.*?\});\s*(?:window|\n|</script>)",
                txt,
                re.DOTALL,
            )
            if match:
                try:
                    return json.loads(match.group(1))
                except (json.JSONDecodeError, TypeError):
                    continue
    return None


def _extract_composition(
    block: Optional[Dict[str, Any]],
    soup: BeautifulSoup,
    view_payload: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Extract product composition from JSON-LD, viewPayload, or rendered DOM."""
    # 1. JSON-LD additionalProperty (propertyID='Composition')
    if block:
        add_props = block.get("additionalProperty")
        if isinstance(add_props, list):
            lines = []
            for prop in add_props:
                if isinstance(prop, dict):
                    prop_id = str(prop.get("propertyID") or "").lower()
                    name = str(prop.get("name") or "").strip()
                    val = str(prop.get("value") or "").strip()
                    if "composition" in prop_id or "composi" in prop_id:
                        if name and val:
                            lines.append(f"{name}: {val}")
                        elif val:
                            lines.append(val)
            if lines:
                return "\n".join(lines)

    # 2. window.zara.viewPayload detailedComposition
    if view_payload:
        detailed_comp = (
            view_payload.get("product", {})
            .get("detail", {})
            .get("detailedComposition")
        )
        if isinstance(detailed_comp, dict):
            parts = detailed_comp.get("parts", [])
            lines = []
            for part in parts:
                if isinstance(part, dict):
                    desc = (part.get("description") or "").strip()
                    components = part.get("components", [])
                    comp_strs = []
                    for c in components:
                        if isinstance(c, dict):
                            mat = (c.get("material") or "").strip()
                            pct = (c.get("percentage") or "").strip()
                            if pct and mat:
                                comp_strs.append(f"{pct} {mat}")
                            elif mat:
                                comp_strs.append(mat)
                    if comp_strs:
                        if desc:
                            lines.append(f"{desc}: {', '.join(comp_strs)}")
                        else:
                            lines.append(", ".join(comp_strs))
            if lines:
                return "\n".join(lines)

    # 3. Rendered DOM .product-detail-view__detailed-composition
    dom_comp = soup.select_one(
        ".product-detail-composition, .product-detail-view__detailed-composition"
    )
    if dom_comp:
        parts_el = dom_comp.select(".product-detail-composition__part")
        if parts_el:
            lines = []
            for p in parts_el:
                part_name_el = p.select_one(".product-detail-composition__part-name")
                part_name = (
                    part_name_el.get_text(" ", strip=True) if part_name_el else ""
                )
                mat_items = [
                    li.get_text(" ", strip=True)
                    for li in p.select(".product-detail-composition__materials li")
                ]
                if mat_items:
                    if part_name:
                        lines.append(f"{part_name}: {', '.join(mat_items)}")
                    else:
                        lines.append(", ".join(mat_items))
            if lines:
                return "\n".join(lines)
        txt = dom_comp.get_text("\n", strip=True)
        if txt:
            return txt

    # 4. JSON-LD material fallback
    if block and block.get("material"):
        mat = str(block.get("material")).strip()
        if mat:
            return mat

    return None


def _extract_description(
    block: Optional[Dict[str, Any]],
    soup: BeautifulSoup,
    view_payload: Optional[Dict[str, Any]] = None,
    default_name: str = "",
) -> str:
    """Extract full product description or fall back to default name."""
    # 1. JSON-LD description
    if block and block.get("description"):
        desc = str(block.get("description")).strip()
        if desc and desc.lower() != default_name.lower():
            return desc

    # 2. viewPayload product.seo.description
    if view_payload:
        seo_desc = (
            view_payload.get("product", {}).get("seo", {}).get("description")
        )
        if seo_desc and str(seo_desc).strip() and str(seo_desc).strip().lower() != default_name.lower():
            return str(seo_desc).strip()

    # 3. meta description
    meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", property="og:description"
    )
    if meta_desc and meta_desc.get("content"):
        content = meta_desc.get("content").strip()
        if content and content.lower() != default_name.lower():
            return content

    # 4. DOM description
    dom_desc = soup.select_one(".product-detail-description, .expandable-text")
    if dom_desc:
        txt = dom_desc.get_text(" ", strip=True)
        if txt.lower().startswith("descrição"):
            txt = txt[9:].strip()
        if txt and txt.lower() != default_name.lower():
            return txt

    return default_name


def _extract_specifications(
    block: Optional[Dict[str, Any]],
    soup: BeautifulSoup,
    view_payload: Optional[Dict[str, Any]] = None,
    composition: Optional[str] = None,
) -> Dict[str, str]:
    """Extract structured product specifications."""
    specs: Dict[str, str] = {}

    # Reference
    display_ref = None
    if view_payload:
        display_ref = (
            view_payload.get("product", {})
            .get("detail", {})
            .get("displayReference")
        )
    if not display_ref:
        ref_btn = soup.select_one(".product-color-extended-name__reference")
        if ref_btn:
            display_ref = ref_btn.get_text(" ", strip=True)
    if display_ref:
        specs["Referência"] = display_ref.strip()
    elif block and block.get("sku"):
        specs["Referência"] = str(block.get("sku")).strip()

    # Composition
    if composition:
        specs["Composição"] = composition

    # Classification attributes from viewPayload
    if view_payload:
        prod = view_payload.get("product", {})
        if prod.get("sectionName"):
            specs["Seção"] = str(prod.get("sectionName")).strip()
        if prod.get("familyName"):
            specs["Família"] = str(prod.get("familyName")).strip()
        if prod.get("subfamilyName"):
            specs["Subfamília"] = str(prod.get("subfamilyName")).strip()

    return specs


def parse_product_detail(
    html: str,
    source_url: str,
    *,
    brand: str = "Zara",
    base_url: str = "https://www.zara.com",
) -> Optional[Dict[str, Any]]:
    """Extract a single product from a Zara PDP."""
    soup = BeautifulSoup(html, "html.parser")
    view_payload = _extract_view_payload(soup)

    for block in _jsonld_blocks(soup):
        if not isinstance(block, dict) or block.get("@type") not in ("Product", "ProductGroup"):
            continue

        offer = _first_offer(block)
        image = block.get("image")
        if isinstance(image, list):
            image_url = str(image[0]).strip() if image else None
        else:
            image_url = str(image).strip() if image else None

        # D-01: hasVariant pode expor cor/tamanho brutos explícitos (schema.org
        # color/size) — nunca inferidos de nome/slug/título.
        variant_colors, variant_size_grid, variant_available_sizes = _extract_variant_attributes(
            block
        )

        name = str(block.get("name") or "")
        desc = _extract_description(block, soup, view_payload, default_name=name)
        comp = _extract_composition(block, soup, view_payload)
        specs = _extract_specifications(block, soup, view_payload, comp)

        product = _product_dict(
            name=name,
            url=source_url,
            image_url=_absolute_url(image_url or "", base_url),
            price_full=parse_price_br(offer.get("price")),
            brand=brand,
            available=None,
            product_code=str(block.get("sku")) if block.get("sku") else None,
            available_colors=variant_colors,
            available_sizes=variant_available_sizes,
            size_grid=variant_size_grid,
            raw_description=desc,
            composition=comp,
            specifications=specs,
        )
        if product:
            rating, review_count = _aggregate_rating(block)
            product["rating"] = rating
            product["review_count"] = review_count
            return product

        # Se for ProductGroup e não encontrou offer na raiz, tenta nas variantes
        if block.get("@type") == "ProductGroup":
            variants = block.get("hasVariant", [])
            for variant in variants:
                if isinstance(variant, dict):
                    v_offer = _first_offer(variant)
                    v_price = parse_price_br(v_offer.get("price"))
                    if v_price:
                        v_name = str(block.get("name") or variant.get("name") or "")
                        v_desc = _extract_description(
                            block, soup, view_payload, default_name=v_name
                        )
                        product = _product_dict(
                            name=v_name,
                            url=source_url,
                            image_url=_absolute_url(image_url or "", base_url),
                            price_full=v_price,
                            brand=brand,
                            available=None,
                            # D-03: SOMENTE o sku do ProductGroup (estável) — nunca
                            # variant.get("sku") isolado, que identifica apenas a
                            # variante selecionada (threat: SKU de variante promovido
                            # a identidade de produto). Ausência de sku no grupo
                            # produz None, nunca um valor de variante.
                            product_code=str(block.get("sku")) if block.get("sku") else None,
                            available_colors=variant_colors,
                            available_sizes=variant_available_sizes,
                            size_grid=variant_size_grid,
                            raw_description=v_desc,
                            composition=comp,
                            specifications=specs,
                        )
                        if product:
                            return product

    og_title = soup.find("meta", property="og:title")
    og_image = soup.find("meta", property="og:image")
    fallback_name = og_title.get("content", "") if og_title else ""
    fallback_desc = _extract_description(
        None, soup, view_payload, default_name=fallback_name
    )
    fallback_comp = _extract_composition(None, soup, view_payload)
    fallback_specs = _extract_specifications(None, soup, view_payload, fallback_comp)

    return _product_dict(
        name=fallback_name,
        url=source_url,
        image_url=_absolute_url(og_image.get("content", "") if og_image else "", base_url),
        price_full=parse_price_br(soup.get_text(" ", strip=True)),
        brand=brand,
        available=None,
        product_code=None,
        raw_description=fallback_desc,
        composition=fallback_comp,
        specifications=fallback_specs,
    )


def parse_products(
    html: str,
    *,
    brand: str = "Zara",
    base_url: str = "https://www.zara.com",
) -> List[Dict[str, Any]]:
    """Extract products from every known Zara listing shape, deduped by URL."""
    products = [
        *parse_tile_products(html, brand=brand, base_url=base_url),
        *parse_itemlist_products(html, brand=brand, base_url=base_url),
    ]

    seen: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for product in products:
        canonical = product["url"].split("?")[0].rstrip("/")
        if canonical in seen:
            continue
        seen.add(canonical)
        deduped.append(product)
    return deduped


def parse_nav_categories(html: str) -> List[Dict[str, Any]]:
    """Extract Zara category links from a rendered navigation/menu page."""
    soup = BeautifulSoup(html, "html.parser")
    noise = {
        "ver tudo",
        "the new",
        "nova colecao",
        "colecao",
        "saldo",
        "01 collection",
        "02 colecao",
        "02 sapatos | acessorios",
        "homem",
    }

    seen: set[str] = set()
    categories: List[Dict[str, Any]] = []
    for anchor in soup.select('a[href*="/br/pt/man-"]'):
        label = anchor.get_text(" ", strip=True)
        href = anchor.get("href", "")
        if not label or len(label) <= 2:
            continue
        label_key = label.lower().strip()
        if label_key in noise:
            continue

        path = _absolute_url(href).replace("https://www.zara.com", "")
        if not path.startswith("/br/pt/") or path in seen:
            continue
        seen.add(path)
        categories.append({"name": label, "path": path, "id": path})

    return categories
