"""Linx Commerce / DCG WebDirector engine.

Suporta e-commerces baseados na plataforma Linx Commerce (ex: Brooksfield e Brooksfield Junior).
Executa requisições HTTP assíncronas com headers de evasão (SecurityCore / Azion) sem
necessidade de browser headless (Playwright).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from config import DEFAULT_USER_AGENTS
from core.models import (
    ConditionalOffer,
    OfferConditionType,
    OfferEligibility,
    OfferSource,
    OfferSourceAuthority,
    OfferSourceKind,
    OfferState,
    BrandSearchResult,
    RawProductBronze,
    SearchProductResult,
    ShippingInfo,
)
from services.engines.base_engine import BaseEngine
from services.shipping.base import ShippingCalculation, apply_shipping_calculation

logger = logging.getLogger("LinxEngine")


class LinxProduct(BaseModel):
    product_id: str
    title: str
    brand: str = ""
    url: str
    image_url: Optional[str] = None
    original_price: Optional[float] = None
    current_price: Optional[float] = None
    cash_price: Optional[float] = None
    installment_count: Optional[int] = None
    installment_value: Optional[float] = None
    stock: int = 0
    available: bool = True
    available_colors: List[str] = Field(default_factory=list)
    available_sizes: List[str] = Field(default_factory=list)
    raw_colors: List[str] = Field(default_factory=list)
    raw_sizes: List[str] = Field(default_factory=list)
    category: Optional[str] = None
    composition: Optional[str] = None

    def to_search_result(self) -> SearchProductResult:
        offers: List[ConditionalOffer] = []
        if self.cash_price is not None and self.current_price is not None and self.cash_price < self.current_price:
            offers.append(
                ConditionalOffer(
                    condition_type=OfferConditionType.PAYMENT_METHOD,
                    condition_label="Desconto PIX",
                    eligibility=OfferEligibility.PUBLIC,
                    price=self.cash_price,
                    payment_method="PIX",
                    state=OfferState.ADVERTISED,
                    source=OfferSource(
                        kind=OfferSourceKind.DOM,
                        ref=self.url,
                        authority=OfferSourceAuthority.STRUCTURED,
                    ),
                    raw_text=f"R$ {self.cash_price:.2f} no PIX",
                )
            )

        return SearchProductResult(
            brand=self.brand,
            product_name=self.title,
            url=self.url,
            price_full=self.original_price or self.current_price,
            price_discount=self.current_price,
            image_url=self.image_url,
            category=self.category,
            available=self.available,
            product_code=self.product_id,
            available_colors=self.available_colors,
            available_sizes=self.available_sizes,
            conditional_offers=offers,
        )

    def to_raw_bronze(self) -> RawProductBronze:
        return RawProductBronze(
            url=self.url,
            brand=self.brand,
            raw_title=self.title,
            price_full=self.original_price or self.current_price,
            price_discount=self.current_price,
            stock_availability=self.available,
            category=self.category,
            product_code=self.product_id,
            available_colors=self.available_colors,
            available_sizes=self.available_sizes,
            raw_colors=self.raw_colors,
            raw_sizes=self.raw_sizes,
            size_grid=self.available_sizes,
            raw_size_grid=self.raw_sizes,
            composition=self.composition,
            image_url=self.image_url,
        )


def _parse_currency(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    match = re.search(r"R\$\s*([\d\.,]+)", cleaned)
    if match:
        val_str = match.group(1).replace(".", "").replace(",", ".")
        try:
            return float(val_str)
        except ValueError:
            return None
    return None


class LinxEngine(BaseEngine):
    """Engine para e-commerces baseados na plataforma Linx Commerce / DCG."""

    def __init__(
        self,
        brand_key: str = "brooksfield",
        base_domain: Optional[str] = None,
        junior_domain: Optional[str] = None,
    ):
        self.brand_key = brand_key

        if not base_domain:
            try:
                from services.brand_service import brand_service
                brand_data = brand_service.get_brand(brand_key)
                if brand_data:
                    base_domain = getattr(brand_data, "domain", None) or (
                        brand_data.get("domain") if isinstance(brand_data, dict) else None
                    )
                    cfg = (
                        getattr(brand_data, "configuration", None)
                        or (brand_data.get("configuration") if isinstance(brand_data, dict) else None)
                        or {}
                    )
                    if not junior_domain:
                        domain_aliases = cfg.get("domain_aliases") or []
                        junior_domain = cfg.get("junior_domain") or next(
                            (a for a in domain_aliases if "junior" in a.lower()), None
                        )
            except Exception:
                pass

        self.base_domain = base_domain or "www.brooksfield.com.br"
        self.junior_domain = junior_domain or (
            "www.brooksfieldjunior.com.br" if "brooksfield" in self.brand_key else None
        )

    def get_engine_name(self) -> str:
        return "LinxCommerce"

    def _get_headers(self) -> Dict[str, str]:
        user_agent = DEFAULT_USER_AGENTS[0] if DEFAULT_USER_AGENTS else "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        return {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

    def _resolve_domain_for_path(self, path_or_query: str) -> str:
        lower = path_or_query.lower()
        if self.junior_domain and (
            self.junior_domain.lower() in lower
            or any(k in lower for k in ["/junior", "infantil-", "infantil", "junior", "menino", "kids"])
        ):
            return self.junior_domain
        return self.base_domain

    def parse_product_card(self, card_soup: Any, base_domain: Optional[str] = None) -> Optional[LinxProduct]:
        """Converte um card HTML li.purchasable em um objeto LinxProduct."""
        if not card_soup:
            return None

        domain = base_domain or self.base_domain
        line_div = card_soup.find("div", attrs={"data-product-id": True})
        if not line_div:
            line_div = card_soup.find(class_=re.compile(r"wd-product-line", re.I))

        pid = line_div.get("data-product-id") if line_div else card_soup.get("data-product-id")
        if not pid:
            class_str = " ".join(card_soup.get("class", []))
            pid_match = re.search(r"product-(\d+)", class_str)
            if pid_match:
                pid = pid_match.group(1)
        if not pid:
            return None

        name = line_div.get("data-name") if line_div else card_soup.get("data-name")
        stock_str = (line_div.get("data-stock") if line_div else card_soup.get("data-stock")) or "1"
        try:
            stock = int(stock_str)
        except (ValueError, TypeError):
            stock = 1

        avail_flag = (line_div.get("data-availability") if line_div else card_soup.get("data-availability")) or "I"
        available = stock > 0 and avail_flag != "N"

        # Título
        if not name:
            title_el = card_soup.find(class_=re.compile(r"title|name", re.I))
            name = title_el.get_text(strip=True) if title_el else f"Produto {pid}"

        # Link e Imagem
        link_el = card_soup.find("a", href=True)
        url = ""
        if link_el:
            href = link_el["href"]
            if href.startswith("http"):
                url = href
            else:
                url = f"https://{domain}{href if href.startswith('/') else '/' + href}"

        img_el = card_soup.find("img")
        image_url = None
        if img_el:
            image_url = img_el.get("src") or img_el.get("data-src")

        # Preços
        original_price = None
        current_price = None
        cash_price = None
        installment_count = None
        installment_value = None

        list_price_el = card_soup.find(lambda tag: tag.get("class") and any(c in tag.get("class") for c in ["list-price", "de-por", "preco-de"]))
        if list_price_el:
            original_price = _parse_currency(list_price_el.get_text(strip=True))

        sale_price_el = card_soup.find(lambda tag: tag.get("class") and any(c in tag.get("class") for c in ["sale-price", "preco-por", "preco-promocional"]))
        if sale_price_el:
            current_price = _parse_currency(sale_price_el.get_text(strip=True))
        else:
            price_el = card_soup.find(lambda tag: tag.get("class") and "price" in tag.get("class") and "has-sale-price" not in tag.get("class"))
            if price_el:
                current_price = _parse_currency(price_el.get_text(strip=True))

        if current_price is None and original_price is not None:
            current_price = original_price

        # PIX e Parcelamento
        all_text = card_soup.get_text(" ", strip=True)
        pix_match = re.search(r"R\$\s*([\d\.,]+)\s*no\s*PIX", all_text, re.IGNORECASE)
        if pix_match:
            cash_price = _parse_currency(f"R$ {pix_match.group(1)}")

        inst_match = re.search(r"(\d+)\s*x\s*R\$\s*([\d\.,]+)", all_text, re.IGNORECASE)
        if inst_match:
            try:
                installment_count = int(inst_match.group(1))
                installment_value = float(inst_match.group(2).replace(".", "").replace(",", "."))
            except (ValueError, IndexError):
                pass

        # Variações (Cores e Tamanhos via JSON embutido)
        available_colors: List[str] = []
        available_sizes: List[str] = []
        json_script = card_soup.find("script", class_="json-data-options")
        if json_script and json_script.string:
            try:
                options_data = json.loads(json_script.string.strip())
                for opt in options_data:
                    prop_name = opt.get("PropertyName", "")
                    values = opt.get("Values", [])
                    if prop_name.lower() == "cor":
                        for val in values:
                            text = val.get("Text")
                            if text and text not in available_colors:
                                available_colors.append(text)
                    elif prop_name.lower() == "tamanho":
                        for val in values:
                            text = val.get("Text")
                            if text and text not in available_sizes:
                                available_sizes.append(text)
            except Exception as e:
                logger.debug("Falha ao parsear json-data-options: %s", e)

        return LinxProduct(
            product_id=str(pid),
            title=name,
            brand=self.brand_key,
            url=url,
            image_url=image_url,
            original_price=original_price,
            current_price=current_price,
            cash_price=cash_price,
            installment_count=installment_count,
            installment_value=installment_value,
            stock=stock,
            available=available,
            available_colors=available_colors,
            available_sizes=available_sizes,
            raw_colors=list(available_colors),
            raw_sizes=list(available_sizes),
        )

    async def _fetch_html(self, url: str) -> Optional[str]:
        """Executa requisição HTTP assíncrona com headers de evasão e descompressão gzip/deflate."""
        import gzip
        import ssl
        import urllib.request
        import zlib

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=self._get_headers())
        try:
            loop = asyncio.get_event_loop()
            def _blocking_fetch():
                with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
                    data = resp.read()
                    encoding = resp.headers.get("Content-Encoding", "").lower()
                    if "gzip" in encoding or data.startswith(b"\x1f\x8b"):
                        try:
                            data = gzip.decompress(data)
                        except Exception:
                            pass
                    elif "deflate" in encoding:
                        try:
                            data = zlib.decompress(data)
                        except zlib.error:
                            try:
                                data = zlib.decompress(data, -zlib.MAX_WBITS)
                            except Exception:
                                pass
                    return data.decode("utf-8", errors="ignore")

            return await loop.run_in_executor(None, _blocking_fetch)
        except Exception as e:
            logger.warning("Falha ao buscar URL %s: %s", url, e)
            return None

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ) -> BrandSearchResult:
        """Executa busca por termo na loja Brooksfield / Brooksfield Junior."""
        domain = self._resolve_domain_for_path(query)
        encoded_query = quote_plus(query)
        url = f"https://{domain}/pesquisa?t={encoded_query}&pg=1"
        brand_name = self.brand_key.replace("_", " ").title()

        html = await self._fetch_html(url)
        if not html:
            return BrandSearchResult(
                brand_key=self.brand_key,
                brand_name=brand_name,
            )

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("li", class_=re.compile(r"purchasable|product-"))
        results: List[SearchProductResult] = []

        for card in cards:
            parsed = self.parse_product_card(card, base_domain=domain)
            if parsed:
                results.append(parsed.to_search_result())
                if max_results is not None and len(results) >= max_results:
                    break

        if include_shipping and zipcode and results:
            await self._populate_shipping(results, zipcode)

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=brand_name,
            products=results,
            total_found=len(results),
        )

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ):
        """Executa varredura exaustiva com paginação ?pg=N em uma categoria Linx."""
        domain = self._resolve_domain_for_path(category_url)
        parsed_url = urlparse(category_url)
        category_path = parsed_url.path if parsed_url.path else category_url
        if not category_path.startswith("/"):
            category_path = "/" + category_path

        self.emit_log(log_callback, f"Iniciando varredura Linx em https://{domain}{category_path}...")

        page = 1
        seen_pids = set()
        max_safety_pages = 50  # Prevenção contra loops infinitos

        while page <= max_safety_pages:
            if cancel_event and cancel_event.is_set():
                self.emit_log(log_callback, "Varredura cancelada pelo usuário.")
                break

            url = f"https://{domain}{category_path}?pg={page}"
            self.emit_log(log_callback, f"Carregando página {page}: {url}")

            html = await self._fetch_html(url)
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.find_all("li", class_=re.compile(r"purchasable|product-"))
            if not cards:
                self.emit_log(log_callback, f"Nenhum produto adicional encontrado na página {page}. Varredura concluída.")
                break

            page_products = []
            for card in cards:
                parsed = self.parse_product_card(card, base_domain=domain)
                if not parsed or parsed.product_id in seen_pids:
                    continue

                seen_pids.add(parsed.product_id)
                page_products.append(parsed)

            if not page_products:
                self.emit_log(log_callback, "Página sem novos produtos. Encerrando paginação.")
                break

            # Enriquecimento concorrente da composição/tecido via PDP
            sem = asyncio.Semaphore(10)
            async def _enrich_comp(prod: LinxProduct):
                async with sem:
                    try:
                        pdp_html = await self._fetch_html(prod.url)
                        if pdp_html:
                            pdp_soup = BeautifulSoup(pdp_html, "html.parser")
                            desc_el = pdp_soup.find(class_="wd-descriptions-text")
                            if desc_el:
                                text = desc_el.get_text("\n", strip=True)
                                for line in text.split("\n"):
                                    l_clean = line.strip()
                                    if any(k in l_clean.lower() for k in ["algod", "linho", "poliester", "viscose", "%", "fio", "seda", "la", "elastano"]):
                                        prod.composition = l_clean
                                        return
                                if not prod.composition:
                                    prod.composition = text.split("\n")[0]
                    except Exception:
                        pass
                if not prod.composition:
                    t_lower = prod.title.lower()
                    if "linho" in t_lower:
                        prod.composition = "Linho"
                    elif any(k in t_lower for k in ["algod", "oxford", "suedine", "pima"]):
                        prod.composition = "100% Algodão"

            await asyncio.gather(*(_enrich_comp(p) for p in page_products))

            for prod in page_products:
                bronze_item = prod.to_raw_bronze()
                validated = self.validate_single(bronze_item, log_callback=log_callback)
                if validated:
                    yield validated

            page += 1
            await asyncio.sleep(0.5)

        self.emit_log(log_callback, f"Varredura Linx finalizada. Total de produtos extraídos: {len(seen_pids)}")

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        """Extrai detalhes de um único produto visitando sua PDP sob demanda."""
        html = await self._fetch_html(product_url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.find("h1")
        title = title_el.get_text(strip=True) if title_el else ""

        desc_el = soup.find(class_=re.compile(r"description|descricao|detalhes", re.I))
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""

        price_el = soup.find(class_=re.compile(r"sale-price|price", re.I))
        price = _parse_currency(price_el.get_text(strip=True)) if price_el else None

        ref_el = soup.find(class_=re.compile(r"reference|referencia|sku", re.I))
        sku = ref_el.get_text(strip=True) if ref_el else None

        return {
            "title": title,
            "description": desc,
            "price": price,
            "sku": sku,
            "url": product_url,
        }

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[ShippingCalculation]:
        """Cálculo de frete sob demanda para Linx Commerce."""
        from services.brand_service import brand_service
        from services.shipping.resolver import resolve_shipping_provider

        brand = brand_service.get_brand(self.brand_key)
        if not brand:
            return None
        provider = resolve_shipping_provider(brand)
        return await provider.calculate(product, zipcode, brand)

    async def _populate_shipping(self, products: List[SearchProductResult], zipcode: str) -> None:
        semaphore = asyncio.Semaphore(3)

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
        """Retorna categorias mapeadas para a marca."""
        from services.brand_service import brand_service
        brand_data = brand_service.get_brand(self.brand_key)
        mappings = getattr(brand_data, "mappings", []) if brand_data else []
        items = []
        for m in mappings:
            path = getattr(m, "vtex_fq_path", None) or (m.get("vtex_fq_path") if isinstance(m, dict) else "")
            label = getattr(m, "label", None) or (m.get("label") if isinstance(m, dict) else "")
            if path:
                items.append({"name": label or path, "path": path})
        return items

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []
