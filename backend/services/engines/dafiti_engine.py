from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from core.browser_manager import BrowserManager
from core.models import BrandSearchResult, RawProductBronze, SearchProductResult, coerce_money_decimal, money_decimal_to_json_number
from services.engines.base_engine import BaseEngine
from services.shipping.base import ShippingCalculation, apply_shipping_calculation

logger = logging.getLogger(__name__)


def _parse_price_text(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = text.strip().replace("R$", "").replace("\xa0", " ").strip()
    # Padrão brasileiro com vírgula: 599,90 ou 1.599,90
    match_br = re.search(r"([\d\.]+,\d{2})", cleaned)
    if match_br:
        val_str = match_br.group(1).replace(".", "").replace(",", ".")
        try:
            dec = coerce_money_decimal(val_str)
            return money_decimal_to_json_number(dec)
        except Exception:
            return None
    # Padrão com ponto: 599.90 ou 1599.90 (ex: dataLayer ou APIs)
    match_dot = re.search(r"(\d+\.\d{2})", cleaned)
    if match_dot:
        try:
            dec = coerce_money_decimal(match_dot.group(1))
            return money_decimal_to_json_number(dec)
        except Exception:
            return None
    match_int = re.search(r"(\d+)", cleaned)
    if match_int:
        try:
            dec = coerce_money_decimal(match_int.group(1))
            return money_decimal_to_json_number(dec)
        except Exception:
            return None
    return None



def _extract_dafiti_pdp_prices(
    soup: BeautifulSoup, html: str
) -> tuple[Optional[float], Optional[float]]:
    """Extrai (original_price, effective_price) da Dafiti com prioridade estrita:

    1. dataLayer / scripts analíticos embutidos na página (fonte da verdade estruturada).
    2. Seletores DOM dedicados (.catalog-detail-price-line, .catalog-detail-price-value).
    3. Inspeção do container de preço (.catalog-detail-price, .catalog-detail-full-price)
       para extrair linhas 'from' e 'to' / 'special' sem assumir que ausência do seletor
       de desconto significa preço cheio.
    """
    orig_price: Optional[float] = None
    eff_price: Optional[float] = None

    # 1. Tentativa via dataLayer embutido no HTML
    # Ex: 'original_price': "599.90", 'price': "339.90"
    m_orig = re.search(r"['\"]original_price['\"]\s*:\s*['\"]?([\d\.,]+)['\"]?", html)
    m_eff = re.search(r"['\"]price['\"]\s*:\s*['\"]?([\d\.,]+)['\"]?", html)

    if m_orig:
        orig_price = _parse_price_text(m_orig.group(1))
    if m_eff:
        eff_price = _parse_price_text(m_eff.group(1))

    if eff_price is not None and eff_price > 0:
        if orig_price is None or orig_price <= 0:
            orig_price = eff_price
        return orig_price, eff_price

    # 2. Tentativa via seletores DOM
    original_price_el = soup.select_one(
        ".catalog-detail-price-special, .catalog-detail-full-price del, .catalog-detail-price-from, .price-original, del, s"
    )
    effective_price_el = soup.select_one(
        ".catalog-detail-price-value, .catalog-detail-price-to, .special-price, .product-box-price-to"
    )

    dom_orig = _parse_price_text(original_price_el.get_text(" ", strip=True)) if original_price_el else None
    dom_eff = _parse_price_text(effective_price_el.get_text(" ", strip=True)) if effective_price_el else None

    # Se achou dom_eff diretamente
    if dom_eff is not None and dom_eff > 0:
        eff_price = dom_eff
        orig_price = dom_orig or orig_price or dom_eff
        return orig_price, eff_price

    # 3. Inspeção de container caso o seletor específico tenha falhado por variação de markup
    price_container = soup.select_one(".catalog-detail-price, .catalog-detail-full-price")
    if price_container:
        lines = price_container.select(".catalog-detail-price-line")
        if len(lines) >= 2:
            p0 = _parse_price_text(lines[0].get_text(" ", strip=True))
            p1 = _parse_price_text(lines[1].get_text(" ", strip=True))
            if p0 and p1:
                return max(p0, p1), min(p0, p1)

    # 4. Fallback final consistente: se apenas um preço foi encontrado
    fallback = dom_orig or orig_price or dom_eff
    return fallback, fallback



def parse_dafiti_products(html: str, base_url: str = "https://www.dafiti.com.br") -> List[Dict[str, Any]]:
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    products: List[Dict[str, Any]] = []

    cards = soup.select(
        ".product-box, [data-product-box], .product-item, li.product-card, div.product-card"
    )

    for card in cards:
        sku = (
            card.get("data-sku")
            or card.get("data-product-sku")
            or card.get("id", "").replace("product-box-", "")
            or ""
        ).strip()

        link_el = card.select_one("a.product-box-link, a[href]")
        if not link_el:
            continue
        href = link_el.get("href", "")
        if not href or href.startswith("javascript:") or "#" in href:
            continue
        product_url = urljoin(base_url, href)

        title_el = card.select_one(
            ".product-box-title, .product-name, [itemprop='name'], .title, h2, h3"
        )
        brand_el = card.select_one(".product-box-brand, .brand")

        brand_text = brand_el.get_text(strip=True) if brand_el else "Dafiti"
        title_text = title_el.get_text(strip=True) if title_el else link_el.get("title", "")
        if not title_text:
            title_text = link_el.get_text(strip=True)

        full_title = f"{brand_text} {title_text}".strip() if brand_text and brand_text.lower() not in title_text.lower() else title_text

        img_el = card.select_one("img.product-image, img[data-src], img[src]")
        image_url = ""
        if img_el:
            image_url = img_el.get("data-src") or img_el.get("src") or ""
            if image_url:
                image_url = urljoin(base_url, image_url)

        price_from_el = card.select_one(
            ".product-box-price-from, .price-from, .regular-price, del, s"
        )
        price_to_el = card.select_one(
            ".product-box-price-to, .special-price, .price-to, .price"
        )

        orig_price = _parse_price_text(price_from_el.get_text(strip=True)) if price_from_el else None
        eff_price = _parse_price_text(price_to_el.get_text(strip=True)) if price_to_el else None

        if eff_price is None and orig_price is not None:
            eff_price = orig_price
        if orig_price is None and eff_price is not None:
            orig_price = eff_price

        if eff_price is None or eff_price <= 0:
            continue

        products.append(
            {
                "product_name": full_title,
                "title": full_title,
                "raw_title": full_title,
                "brand": brand_text or "Dafiti",
                "price": eff_price,
                "price_discount": eff_price if orig_price and orig_price > eff_price else None,
                "price_full": orig_price or eff_price,
                "url": product_url,
                "image_url": image_url,
                "sku": sku,
                "product_code": sku,
                "available": True,
            }
        )

    return products


class DafitiEngine(BaseEngine):
    """Engine Playwright dedicada para o marketplace Dafiti."""

    def __init__(self, brand_key: str = "dafiti"):
        self.brand_key = brand_key
        self.base_url = "https://www.dafiti.com.br"

    def _brand_config(self) -> Dict[str, Any]:
        from services.brand_service import brand_service

        brand = brand_service.get_brand(self.brand_key)
        default_search = "https://www.dafiti.com.br/catalog/?q={query}&gender=masculino"
        if not brand:
            return {
                "brand_name": "Dafiti",
                "domain": "www.dafiti.com.br",
                "search_url_template": default_search,
                "proxy_url": None,
            }
        return {
            "brand_name": getattr(brand, "brand_name", "Dafiti"),
            "domain": getattr(brand, "domain", "www.dafiti.com.br") or "www.dafiti.com.br",
            "search_url_template": getattr(brand, "search_url_template", None) or default_search,
            "proxy_url": getattr(brand, "proxy_url", None),
        }

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ) -> BrandSearchResult:
        import re

        is_kids = any(term in query.lower() for term in ["infantil", "menino", "garoto", "kids"])
        gender_param = "menino" if is_kids else "masculino"

        config = self._brand_config()
        brand_name = config["brand_name"]
        raw_template = config["search_url_template"]

        # Garante que o parâmetro gender adequado é utilizado
        if "gender=" not in raw_template:
            delimiter = "&" if "?" in raw_template else "?"
            template_with_gender = f"{raw_template}{delimiter}gender={gender_param}"
        else:
            template_with_gender = re.sub(r"gender=[^&]+", f"gender={gender_param}", raw_template)

        search_url = template_with_gender.format(query=quote_plus(query.strip()))

        raw_products: List[Dict[str, Any]] = []
        error: Optional[str] = None

        try:
            html = await BrowserManager.fetch_html(
                search_url,
                wait_selector=".product-box, [data-product-box], .catalog-detail",
                timeout=30000,
            )
            raw_products = parse_dafiti_products(html, base_url=self.base_url)
        except Exception as exc:
            logger.warning("[DafitiEngine] Falha ao extrair busca '%s': %s", query, exc)
            error = str(exc)

        # Filtro por gênero de moda
        if is_kids:
            filtered = self.filter_boys_fashion(raw_products)
        else:
            filtered = self.filter_mens_fashion(raw_products)

        search_products: List[SearchProductResult] = []
        for p in (filtered if max_results is None else filtered[:max_results]):
            validated = self.validate_single(p)
            if validated:
                search_products.append(
                    SearchProductResult(
                        brand=validated.get("brand") or brand_name,
                        product_name=validated.get("raw_title") or validated.get("title") or validated.get("product_name") or "",
                        price_full=validated.get("price_full"),
                        price_discount=validated.get("price_discount"),
                        image_url=validated.get("image_url"),
                        url=validated.get("url") or "",
                        available=validated.get("available", True),
                    )
                )

        if sort == "price_asc":
            search_products.sort(
                key=lambda p: p.price_discount or p.price_full or float("inf")
            )
        elif sort == "price_desc":
            search_products.sort(
                key=lambda p: p.price_discount or p.price_full or 0,
                reverse=True,
            )

        from core.pricing import calculate_price_averages
        averages = calculate_price_averages(search_products)

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=brand_name,
            products=search_products,
            total_found=len(search_products),
            average_price=averages["average_price"],
            average_price_original=averages["average_price_original"],
            error=error if not search_products else None,
        )

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ):
        raise NotImplementedError("Dafiti engine is for search only.")

    async def discover_categories(self) -> List[Dict[str, Any]]:
        return []

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []

    async def get_pdp_product(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            html = await BrowserManager.fetch_html(
                url,
                wait_selector="h1, .catalog-detail-price, .catalog-detail-price-value",
                timeout=30000,
                extra_sleep=2.0,
            )
            soup = BeautifulSoup(html, "html.parser")
            title_el = soup.select_one("h1, .product-name, .catalog-detail-title")
            brand_el = soup.select_one(".seller-text a, .catalog-detail-brand, [itemprop='brand']")
            img_el = soup.select_one(
                ".gallery-preview-img, .gallery img, img.product-image, img[data-zoom], img[alt*='Marca']"
            )
            description_el = soup.select_one(
                "#desc-detail, .product-description, [itemprop='description']"
            )
            stock_el = soup.select_one(".stock-available-message, .stock-available")

            title = title_el.get_text(strip=True) if title_el else ""
            brand = brand_el.get_text(strip=True) if brand_el else "Dafiti"

            original_price, effective_price = _extract_dafiti_pdp_prices(soup, html)

            img = (
                img_el.get("src") or img_el.get("data-zoom") or img_el.get("data-src") or ""
                if img_el else ""
            )
            if img:
                img = urljoin(self.base_url, img)
            # Especificações estruturadas
            specifications: Dict[str, str] = {}
            for tr in soup.select(".product-informations tr, .technical-specs tr"):
                tds = tr.select("td")
                if len(tds) >= 2:
                    k = tds[0].get_text(strip=True)
                    v = tds[1].get_text(strip=True)
                    if k and v:
                        specifications[k] = v

            # Descrição limpa
            raw_desc = ""
            if description_el:
                ignore_lines = {"veja mais", "veja menos", "ver mais", "ver menos", "denunciar este anúncio"}
                lines = []
                for l in description_el.get_text("\n", strip=True).splitlines():
                    l_str = l.strip()
                    if l_str and l_str.lower() not in ignore_lines and not l_str.lower().startswith("ver detalhes"):
                        lines.append(l_str)
                raw_desc = "\n".join(lines) if lines else description_el.get_text(" ", strip=True)

            # Composição
            composition = specifications.get("Composição") or specifications.get("Composicao") or specifications.get("Material")
            if not composition and description_el:
                m_comp = re.search(r"Composi[çc][ãa]o:\s*([^\n\r<]+)", description_el.get_text(), re.IGNORECASE)
                if m_comp:
                    composition = m_comp.group(1).strip()
            if not composition:
                comb_text = f"{title} {raw_desc}"
                fabric_patterns = [
                    r"\b(100%\s*algod[ãa]o\s*pima)\b",
                    r"\b(algod[ãa]o\s*pima)\b",
                    r"\b(100%\s*algod[ãa]o)\b",
                    r"\b(jeans)\b",
                    r"\b(100%\s*poli[ée]ster)\b",
                    r"\b(100%\s*linho)\b",
                    r"\b(100%\s*viscose)\b",
                    r"\b(100%\s*seda)\b",
                    r"\b(\d{1,3}%\s*[A-Za-zÀ-ÿ]+(?:\s*(?:e|,|\/)\s*\d{1,3}%\s*[A-Za-zÀ-ÿ]+)*)\b",
                ]
                for pat in fabric_patterns:
                    m = re.search(pat, comb_text, re.IGNORECASE)
                    if m:
                        composition = m.group(1).strip().capitalize()
                        break

            # Cores disponíveis
            available_colors: List[str] = []
            all_colors: List[str] = []
            for c_el in soup.select(".color-variation, .product-color, [data-color], .box-colors li, a[title*='Cor']"):
                c_name = c_el.get("data-color") or c_el.get("title") or c_el.get_text(strip=True)
                if c_name and c_name not in all_colors:
                    all_colors.append(c_name)
                    if "disabled" not in c_el.get("class", []):
                        available_colors.append(c_name)
            cor_spec = specifications.get("Cor")
            if cor_spec:
                c_val = cor_spec.strip()
                if c_val not in all_colors:
                    all_colors.append(c_val)
                if c_val not in available_colors:
                    available_colors.append(c_val)

            # Tamanhos disponíveis
            available_sizes: List[str] = []
            all_sizes: List[str] = []
            for item in soup.select(".radio-button-size-block, li[data-target='sku'], .size-select-container li"):
                name_el = item.select_one("[data-field='name'], .size-option, span")
                if name_el:
                    sz_text = name_el.get_text(strip=True)
                    if sz_text and sz_text not in all_sizes:
                        all_sizes.append(sz_text)
                    inp = item.select_one("input")
                    is_disabled = (
                        (inp and inp.has_attr("disabled"))
                        or item.get("data-value") == ""
                        or item.get("data-stock") == "0"
                        or "disabled" in item.get("class", [])
                    )
                    if not is_disabled and sz_text not in available_sizes:
                        available_sizes.append(sz_text)

            for opt in soup.select("select[name*='sku'] option, select[name*='size'] option, select#select-size option"):
                val = opt.get("value")
                txt = opt.get_text(strip=True)
                if val and val != "0" and "selecione" not in txt.lower() and "escolha" not in txt.lower():
                    if txt not in all_sizes:
                        all_sizes.append(txt)
                    if not opt.has_attr("disabled") and txt not in available_sizes:
                        available_sizes.append(txt)

            stock_text = stock_el.get_text(" ", strip=True).lower() if stock_el else ""
            available = len(available_sizes) > 0 if all_sizes else not any(term in stock_text for term in ("esgotado", "indisponível", "indisponivel"))

            return {
                "brand": brand,
                "raw_title": title,
                "raw_description": raw_desc or (description_el.get_text(" ", strip=True) if description_el else ""),
                "price_full": original_price,
                "price_discount": (
                    effective_price
                    if original_price is not None
                    and effective_price is not None
                    and effective_price < original_price
                    else None
                ),
                "price_discount_is_delta": False,
                "stock_availability": available,
                "image_url": img or None,
                "url": url,
                "composition": composition,
                "specifications": specifications,
                "available_colors": available_colors,
                "available_sizes": available_sizes,
                "raw_colors": all_colors,
                "raw_sizes": available_sizes,
                "size_grid": all_sizes,
                "raw_size_grid": all_sizes,
            }
        except Exception as exc:
            logger.warning("[Dafiti] Falha no PDP %s: %s", url, exc)
            err_str = str(exc).lower()
            if any(term in err_str for term in ("err_name_not_resolved", "dns", "timeout", "connection", "network")):
                return {
                    "_marketplace_read_state": "network_error",
                    "error_message": f"Falha temporária de rede/DNS: {str(exc)[:100]}",
                }
            if any(term in err_str for term in ("403", "forbidden", "captcha", "cloudflare", "blocked")):
                return {
                    "_marketplace_read_state": "blocked",
                    "error_message": "Acesso bloqueado pelo site (anti-bot / 403).",
                }
            return {
                "_marketplace_read_state": "network_error",
                "error_message": f"Erro ao acessar página: {str(exc)[:100]}",
            }

    def get_engine_name(self) -> str:
        return "Dafiti"

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[ShippingCalculation]:
        """Cálculo de frete sob demanda para Dafiti."""
        from services.brand_service import brand_service
        from services.shipping.resolver import resolve_shipping_provider

        brand = brand_service.get_brand("dafiti")
        if not brand:
            return None
        provider = resolve_shipping_provider(brand)
        return await provider.calculate(product, zipcode, brand)

    async def _populate_shipping(self, products: List[SearchProductResult], zipcode: str) -> None:
        semaphore = asyncio.Semaphore(5)

        async def _one(product: SearchProductResult) -> None:
            async with semaphore:
                try:
                    calculation = await self.calculate_shipping(product, zipcode)
                    if calculation is not None:
                        apply_shipping_calculation(product, calculation)
                except Exception:
                    return

        await asyncio.gather(*(_one(product) for product in products))

    async def discover_categories(self) -> List[Dict[str, Any]]:
        catalog = await self.get_catalog()
        flat = []
        for group in catalog:
            for item in group.get("items", []):
                flat.append({"name": item.get("label", ""), "path": item.get("path", "")})
        return flat

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        return await self.get_pdp_product(product_url)
