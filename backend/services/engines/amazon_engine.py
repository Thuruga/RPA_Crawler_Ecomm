"""
Amazon Search Engine.

Estratégia de busca:
  1. curl_cffi (rápido, impersonation de Chrome)
  2. Playwright (fallback quando Amazon retorna 503/CAPTCHA)

A Amazon bloqueia bots com frequência via CAPTCHA (503). O Playwright com
stealth mitiga parcialmente esse problema em ambiente local/dev.

Em produção com alto volume, considere usar um proxy residencial
(BRIGHTDATA_PROXY_URL ou SCRAPERAPI_KEY em config.py).
"""

import logging
import urllib.parse
import re
from typing import List, Dict, Any, Optional, Callable
import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from core.models import BrandSearchResult, SearchProductResult, ShippingInfo
from config import relevance_settings
from services.engines.base_engine import BaseEngine
from services.attribute_parsing import extract_color_fallback, extract_size_fallback
from services.engines.seller_extraction import parse_amazon_seller_from_html, MARKETPLACE_DEFAULT_SELLER

logger = logging.getLogger(__name__)


class AmazonEngine(BaseEngine):
    # Tier 2 calculate_shipping() below is a permanent stub that always returns
    # None (no anti-bot attempt is made) — a None result here means "unimplemented",
    # not "blocked". See BaseEngine.SHIPPING_TIER2_BLOCKS_ON_NONE.
    SHIPPING_TIER2_BLOCKS_ON_NONE = False

    def __init__(self, brand_key: str = "amazon"):
        self.brand_key = brand_key
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }

    def get_engine_name(self) -> str:
        return "Amazon"

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        raise NotImplementedError("Amazon engine is for search only.")

    async def discover_categories(self) -> List[Dict[str, Any]]:
        return []

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        try:
            async with AsyncSession(impersonate="chrome120", timeout=15) as session:
                response = await session.get(product_url, headers=self.headers)
                if response.status_code == 200:
                    seller = parse_amazon_seller_from_html(response.text) or MARKETPLACE_DEFAULT_SELLER["Amazon"]
                    return {"seller": seller}
        except Exception as e:
            logger.error(f"Erro ao buscar detalhes Amazon {product_url}: {e}")
        return None

    async def get_pdp_product(self, product_url: str) -> Optional[Dict[str, Any]]:
        """
        Extrai o produto COMPLETO da PDP da Amazon (para o monitor de preço).

        1ª tentativa: curl_cffi. Em caso de CAPTCHA/503, faz fallback para
        Playwright (BrowserManager). Retorna dict compatível com RawProductBronze.
        """
        try:
            async with AsyncSession(impersonate="chrome120", timeout=15) as session:
                response = await session.get(product_url, headers=self.headers)
                if response.status_code == 200 and not self._check_captcha(response.text):
                    product = self._parse_pdp_html(response.text, product_url)
                    if product:
                        return product
                    # 200 OK sem CAPTCHA mas sem produto: o parser não achou
                    # título/preço. Loga para diagnosticar (antes era silencioso)
                    # e segue para o Playwright (layout/preço pode hidratar via JS).
                    logger.warning(
                        "Amazon PDP %s: 200 OK sem CAPTCHA, mas título/preço não extraídos "
                        "(seletor de preço ausente?) — ativando Playwright.",
                        product_url,
                    )
                elif response.status_code == 503 or self._check_captcha(response.text):
                    logger.warning(
                        "Amazon PDP %s disparou CAPTCHA/503 — ativando Playwright.", product_url
                    )
                else:
                    logger.warning(
                        "Amazon PDP %s: HTTP %s (curl_cffi) — ativando Playwright.",
                        product_url, response.status_code,
                    )
        except Exception as e:
            logger.error(f"Erro Amazon PDP via curl_cffi {product_url}: {e}")

        if relevance_settings.PLAYWRIGHT_AMAZON_FALLBACK:
            try:
                from core.browser_manager import BrowserManager

                html = await BrowserManager.fetch_html(
                    product_url, wait_until="domcontentloaded", extra_sleep=2.0, timeout=35000
                )
                if self._check_captcha(html):
                    logger.warning("Amazon PDP Playwright: CAPTCHA para %s", product_url)
                    return None
                product = self._parse_pdp_html(html, product_url)
                if not product:
                    # NAMEIA o engine e o desfecho: o Playwright renderizou mas o
                    # parser não extraiu preço — pista p/ a próxima iteração ao vivo.
                    logger.warning(
                        "Amazon PDP %s: Playwright renderizou mas título/preço não foram "
                        "extraídos (revisar seletores de preço da PDP).",
                        product_url,
                    )
                return product
            except Exception as exc:
                logger.error(f"Amazon PDP Playwright erro {product_url}: {exc}")
        else:
            logger.warning(
                "Amazon PDP %s: curl_cffi não resolveu e PLAYWRIGHT_AMAZON_FALLBACK=False — "
                "sem preço.", product_url,
            )
        return None

    @staticmethod
    def _explicit_variation_options(soup: BeautifulSoup, variation_id: str) -> List[str]:
        values: List[str] = []
        for option in soup.select(f"#{variation_id} option"):
            label = option.get_text(" ", strip=True)
            value = str(option.get("value") or "").strip()
            if not label or value in {"", "-1"} or label.lower().startswith("selecione"):
                continue
            if label not in values:
                values.append(label)
        return values

    @staticmethod
    def _normalize_explicit_attributes(
        raw_colors: List[str], raw_sizes: List[str]
    ) -> Dict[str, List[str]]:
        def normalized(values: List[str], fallback: Callable[[str], str]) -> List[str]:
            result: List[str] = []
            for value in values:
                canonical = fallback(value)
                if canonical and canonical not in result:
                    result.append(canonical)
            return result

        return {
            "raw_colors": list(raw_colors),
            "raw_sizes": list(raw_sizes),
            "available_colors": normalized(raw_colors, extract_color_fallback),
            "available_sizes": normalized(raw_sizes, extract_size_fallback),
        }

    def _parse_pdp_html(self, html: str, product_url: str) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")

        title_tag = soup.find(id="productTitle")
        title = title_tag.get_text(strip=True) if title_tag else ""

        price_full = self._extract_pdp_price(soup)

        # Imagem principal
        image_url = None
        img_tag = soup.find(id="landingImage") or soup.find(id="imgBlkFront")
        if img_tag:
            image_url = img_tag.get("src") or img_tag.get("data-old-hires")

        # Disponibilidade
        available = True
        avail_tag = soup.find(id="availability")
        if avail_tag:
            avail_text = avail_tag.get_text(strip=True).lower()
            if any(s in avail_text for s in ["indisponível", "esgotado", "unavailable", "out of stock"]):
                available = False
        rating, review_count = self._extract_review_summary(soup)

        if not title or price_full is None or price_full <= 0:
            # Loga QUAL campo faltou — a próxima iteração ao vivo fica diagnosticável
            # (antes: retornava None silenciosamente e o monitor ficava "Pendente").
            logger.warning(
                "Amazon _parse_pdp_html: extração incompleta para %s "
                "(title=%r, price_full=%r) — sem produto.",
                product_url, bool(title), price_full,
            )
            return None

        match = re.search(r"/(?:dp|gp/product|exec/obidos/ASIN)/([A-Z0-9]{10})", product_url)
        product_code = match.group(1) if match else None
        attributes = self._normalize_explicit_attributes(
            self._explicit_variation_options(soup, "variation_color_name"),
            self._explicit_variation_options(soup, "variation_size_name"),
        )

        return {
            "url": product_url,
            "brand": self.brand_key,
            "raw_title": title,
            "raw_description": title,
            "price_full": price_full,
            "image_url": image_url or None,
            "stock_availability": available,
            "product_code": product_code,
            "rating": rating,
            "review_count": review_count,
            **attributes,
        }

    def _extract_review_summary(self, soup: BeautifulSoup) -> tuple[Optional[float], Optional[int]]:
        rating = None
        for node in (
            soup.select_one("#acrPopover"),
            soup.select_one("span[data-hook='rating-out-of-text']"),
            soup.select_one("i.a-icon-star span.a-icon-alt"),
        ):
            text = (node.get("title") or node.get_text(" ", strip=True)) if node else ""
            rating = self._parse_rating(text)
            if rating is not None:
                break

        review_count = None
        count_node = soup.select_one("#acrCustomerReviewText")
        if count_node:
            review_count = self._parse_review_count(count_node.get_text(" ", strip=True))
        return rating, review_count

    @staticmethod
    def _parse_rating(text: Optional[str]) -> Optional[float]:
        if not text:
            return None
        match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:de|out of)\s*5", text, re.IGNORECASE)
        if not match:
            return None
        try:
            return round(float(match.group(1).replace(",", ".")), 1)
        except ValueError:
            return None

    @staticmethod
    def _parse_review_count(text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        match = re.search(r"([\d\.]+)", text)
        if not match:
            return None
        try:
            return int(match.group(1).replace(".", ""))
        except ValueError:
            return None

    def _extract_pdp_price(self, soup: BeautifulSoup) -> Optional[float]:
        """Extrai o preço da PDP tentando os contêineres da PDP, do mais específico
        para o mais genérico.

        A Amazon serve vários layouts de preço. O layout atual usa o bloco
        ``#corePriceDisplay_desktop_feature_div`` / ``#corePrice_feature_div``.
        Se o preço for um range (ex: R$ 178,99 - R$ 579,99), extrai todos os preços
        não riscados e pega o MAX, para evitar o 'price bait' de tamanhos esgotados.
        """
        scoped_selectors = [
            "#apex_price",
            "#desktop_unifiedPrice",
            "#corePriceDisplay_desktop_feature_div",
            "#corePrice_feature_div",
            "#corePrice_desktop",
            "div[data-cy='price-recipe']",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#price_inside_buybox",
        ]
        
        for css in scoped_selectors:
            container = soup.select_one(css)
            if not container:
                continue
                
            prices = []
            for p_span in container.select("span.a-price:not(.a-text-price) span.a-offscreen, span.aok-offscreen.apex-pricetopay-accessibility-label"):
                price_val = self._price_from_tag_text(p_span.get_text(strip=True))
                if price_val is not None:
                    prices.append(price_val)
                    
            if prices:
                return min(prices)
                
            # Fallback text-based
            price = self._price_from_tag_text(container.get_text(strip=True))
            if price is not None:
                return price

        return None

    @staticmethod
    def _price_from_tag_text(text: Optional[str]) -> Optional[float]:
        """Converte um texto de preço da Amazon BR (ex.: 'R$ 1.299,90') em float."""
        if not text:
            return None
        raw = (
            text.replace("R$", "")
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(".", "")
            .replace(",", ".")
            .strip()
        )
        matches = re.findall(r"\d+\.\d{2}", raw)
        if matches:
            try:
                return float(matches[0])
            except ValueError:
                return None
        return None

    def _parse_html(self, html: str, limit: int) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        produtos = []
        urls_vistas = set()
        for item in soup.find_all(
            "div", attrs={"data-component-type": "s-search-result"}
        ):
            if len(produtos) >= limit:
                break
            try:
                a_tag = (
                    item.find_all("a", class_=re.compile(r"a-text-normal")) or [None]
                )[0]
                if not a_tag:
                    for h2 in item.find_all("h2"):
                        a_cand = h2.find_parent("a") or h2.find("a")
                        if a_cand:
                            a_tag = a_cand
                            break
                if not a_tag:
                    continue
                title = a_tag.text.replace("Anúncio patrocinado –", "").strip()
                url = a_tag["href"]
                if "/sspa/click" in url and "url=" in url:
                    url = urllib.parse.unquote(url.split("url=")[1].split("&")[0])
                if url.startswith("/"):
                    url = f"https://www.amazon.com.br{url}"
                url = url.split("?")[0].split("/ref=")[0]
                prices = []
                for p_span in item.select("span.a-price:not(.a-text-price) span.a-offscreen"):
                    price_val = self._price_from_tag_text(p_span.get_text(strip=True))
                    if price_val is not None:
                        prices.append(price_val)
                
                if not prices:
                    price_span = item.find("span", class_="a-offscreen")
                    if not price_span:
                        continue
                    price = self._price_from_tag_text(price_span.get_text(strip=True))
                    if price is None:
                        continue
                else:
                    price = min(prices)

                img_tag = item.find("img", class_="s-image")
                image_url = img_tag["src"] if img_tag else None

                seller = "Amazon"
                
                match = re.search(r"/(?:dp|gp/product|exec/obidos/ASIN)/([A-Z0-9]{10})", url)
                product_code = match.group(1) if match else None

                if url not in urls_vistas and price > 0:
                    produtos.append(
                        {
                            "plataforma": "Amazon",
                            "titulo": title,
                            "preco": price,
                            "url": url,
                            "imagem": image_url,
                            "seller": seller,
                            "product_code": product_code,
                        }
                    )
                    urls_vistas.add(url)
            except Exception:
                continue
        return produtos

    def _check_captcha(self, html: str) -> bool:
        """
        Detecta se a Amazon retornou uma página de CAPTCHA/bot challenge.

        Sinais usados APENAS os inequívocos da página de CAPTCHA real da Amazon.
        Evita 'robot' que é muito genérico (aparece em <meta name='robots'> em páginas normais).
        """
        captcha_signals = [
            "captcha" in html.lower(),
            "api-services-support@amazon.com" in html,
            '<form method="get" action="/errors/validateCaptcha"' in html,
            "Digitar os caracteres que você vê na imagem" in html,
            "Enter the characters you see below" in html,
        ]
        return any(captcha_signals)

    async def _search_with_playwright(self, query: str, max_results: int, sort: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Usa BrowserManager (Playwright) para contornar o CAPTCHA da Amazon.
        """
        from core.browser_manager import BrowserManager

        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.amazon.com.br/s?k={encoded_query}"
        if sort == "recent":
            url += "&s=date-desc-rank"
        elif sort == "price_asc":
            url += "&s=price-asc-rank"
        elif sort == "price_desc":
            url += "&s=price-desc-rank"

        try:
            html = await BrowserManager.fetch_html(
                url,
                wait_until="domcontentloaded",
                extra_sleep=2.0,
                timeout=35000
            )

            if self._check_captcha(html):
                logger.warning(f"Amazon Playwright: CAPTCHA detectado para '{query}'")
                return []

            produtos = self._parse_html(html, max_results)
            logger.info(f"Amazon Playwright: {len(produtos)} produtos para '{query}'")
            return produtos
        except Exception as exc:
            import traceback
            logger.error(f"Amazon Playwright erro: {repr(exc)}\n{traceback.format_exc()}")
            return []

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> BrandSearchResult:
        """
        Busca produtos na Amazon.

        1ª tentativa: curl_cffi (rápido, ~2s)
        2ª tentativa: Playwright (fallback quando Amazon retorna 503/CAPTCHA)

        Em caso de CAPTCHA persistente, configure BRIGHTDATA_PROXY_URL ou
        SCRAPERAPI_KEY no .env para rotação de IP.
        """
        encoded_query = urllib.parse.quote_plus(query)
        url = f"https://www.amazon.com.br/s?k={encoded_query}"
        if sort == "recent":
            url += "&s=date-desc-rank"
        elif sort == "price_asc":
            url += "&s=price-asc-rank"
        elif sort == "price_desc":
            url += "&s=price-desc-rank"
        max_fetch = max_results * 2 if max_results is not None else 1000
        fallback_error = None
        raw_items: List[Dict[str, Any]] = []

        # --- Tentativa 1: curl_cffi ---
        try:
            async with AsyncSession(
                impersonate="chrome120",
                timeout=relevance_settings.ENGINE_DEFAULT_TIMEOUT_SECONDS
            ) as session:
                response = await session.get(url, headers=self.headers)
                if response.status_code == 200 and not self._check_captcha(response.text):
                    raw_items = self._parse_html(response.text, max_fetch)
                    if raw_items:
                        logger.info(f"✅ Amazon curl_cffi: {len(raw_items)} produtos para '{query}'")
                    else:
                        logger.warning(f"Amazon curl_cffi: 0 produtos para '{query}' (página vazia ou anti-bot).")
                elif response.status_code == 503 or self._check_captcha(response.text):
                    logger.warning(f"Amazon disparou CAPTCHA (HTTP {response.status_code}) — ativando Playwright.")
                else:
                    logger.error(f"Amazon curl_cffi: HTTP {response.status_code}")
                    fallback_error = f"HTTP {response.status_code}"
        except Exception as exc:
            logger.error(f"Amazon curl_cffi erro: {exc}")
            fallback_error = str(exc)

        # --- Tentativa 2: Playwright (se curl_cffi falhou ou retornou vazio) ---
        if not raw_items and relevance_settings.PLAYWRIGHT_AMAZON_FALLBACK:
            logger.info(f"Amazon: ativando Playwright para '{query}'...")
            raw_items = await self._search_with_playwright(query, max_fetch, sort)
            if not raw_items:
                fallback_error = fallback_error or "CAPTCHA — Playwright também não retornou resultados"
                logger.warning(f"Amazon Playwright: sem resultados para '{query}'")

        # --- Montar resultado ---
        filtered = self.filter_mens_fashion(raw_items)
        if max_results is not None:
            filtered = filtered[:max_results]
        products = []
        for item in filtered:
            shipping = ShippingInfo(status="Calculado no checkout", price=0.0 if False else None) if include_shipping else None
            products.append(SearchProductResult(
                brand="Amazon",
                product_name=item["titulo"],
                url=item["url"],
                price_full=item["preco"],
                price_discount=None,
                image_url=item["imagem"],
                available=True,
                seller=item["seller"],
                product_code=item.get("product_code"),
                shipping=shipping,
                is_free_shipping=False,
                shipping_price=None
            ))

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name="Amazon",
            products=products,
            total_found=len(products),
            error=fallback_error if not products else None,
        )

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[Dict[str, Any]]:
        """
        Tier 2: Cálculo de frete para Amazon.
        Sendo extremamente restrita, a Amazon exige sessão com endereço logado 
        ou endpoints de delivery baseados em CSRF tokens para simular frete.
        Neste fallback mockado, retornamos None (frete a calcular).
        """
        return None

    async def _read_delivery_text(self, page) -> str:
        selectors = [
            "#mir-layout-DELIVERY_BLOCK",
            "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE",
            "#mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE",
            "#deliveryBlockMessage",
            "#contextualIngressPtLabel_deliveryShortLine",
            "[data-csa-c-delivery-price]",
        ]
        parts = []
        for selector in selectors:
            try:
                elements = await page.query_selector_all(selector)
                for element in elements:
                    text = (await element.inner_text()).strip()
                    if text:
                        parts.append(text)
            except Exception:
                continue
        return "\n".join(dict.fromkeys(parts))

    def _parse_delivery_time(self, text: str) -> Dict[str, Any]:
        """Extrai prazo de entrega do MESMO texto ja lido (D-02) — sem novas leituras de pagina."""
        delivery: Dict[str, Any] = {}

        day_patterns = [
            r'(?:receba|chegar[aá])[^\n\r]{0,40}?em\s+at[eé]\s+(\d{1,2})\s+dias?',
            r'chegar[aá]\s+entre\s+\d{1,2}\s+e\s+(\d{1,2})\s+dias?',
            r'entrega\s+em\s+at[eé]\s+(\d{1,2})\s+dias?',
        ]
        for pattern in day_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                try:
                    delivery["estimated_delivery_days"] = int(match.group(1))
                except (TypeError, ValueError):
                    pass
                break

        if re.search(r'\bamanh[aã]\b', text, re.IGNORECASE):
            delivery.setdefault("estimated_delivery_days", 1)

        raw_match = re.search(
            r'((?:receba|chegar[aá])[^\n\r.]{0,80})',
            text,
            re.IGNORECASE,
        )
        if raw_match:
            delivery["delivery_raw_text"] = raw_match.group(1).strip()

        return delivery

    def _parse_shipping_text(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None

        lower = text.lower()
        if re.search(r'(frete|entrega)\s+gr[aá]tis|gr[aá]tis\s+(?:de\s+)?(?:frete|entrega)', lower):
            result: Dict[str, Any] = {"is_free_shipping": True, "shipping_price": 0.0}
            result.update(self._parse_delivery_time(text))
            return result

        freight_patterns = [
            r'(?:frete|entrega)[^\n\r]{0,80}?R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})',
            r'R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})[^\n\r]{0,80}?(?:frete|entrega)',
        ]
        for pattern in freight_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1).replace(".", "").replace(",", ".")
                result = {"is_free_shipping": False, "shipping_price": float(value)}
                result.update(self._parse_delivery_time(text))
                return result

        return None

    async def calculate_shipping_advanced(self, url: str, zipcode: str) -> Optional[Dict[str, Any]]:
        """
        Calcula o frete real via Playwright interagindo com a página da Amazon.
        Tenta extrair da string de entrega padrão, ou injetar no modal de CEP.
        """
        try:
            logger.info(f"Calculando frete Amazon via Playwright para {url} com CEP {zipcode}")
            from playwright.async_api import async_playwright
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True, 
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"]
                )
                try:
                    ctx = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        locale="pt-BR"
                    )
                    page = await ctx.new_page()
                    try:
                        from playwright_stealth import Stealth
                        await Stealth().apply_stealth_async(page)
                    except Exception as e:
                        logger.debug(f"Amazon: stealth não aplicado: {e}")
                    
                    # Amazon bloqueia fácil, então load com sleep extra
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(3)
                    
                    # Tenta primeiro ver se já existe texto de "Frete GRÁTIS" na página
                    html = await page.content()
                    if self._check_captcha(html):
                        logger.warning("Amazon: CAPTCHA no cálculo de frete.")
                        return {
                            "_marketplace_read_state": "blocked",
                            "error": "A Amazon bloqueou o cálculo de frete com CAPTCHA/anti-bot nesta sessão."
                        }
                    
                    parsed_shipping = self._parse_shipping_text(await self._read_delivery_text(page))
                    if parsed_shipping:
                        return parsed_shipping
                    delivery_message = await page.query_selector('#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE')
                    if delivery_message:
                        text = await delivery_message.inner_text()
                        if "grátis" in text.lower():
                            return {"is_free_shipping": True, "shipping_price": 0.0}
                    
                    # Clica no modal de CEP (Enviar para...)
                    location_btn = await page.query_selector('#nav-global-location-popover-link')
                    if location_btn:
                        await location_btn.click()
                        await asyncio.sleep(2)
                        
                        # Amazon BR divide o CEP em 2 campos as vezes (5 dígitos e 3 dígitos)
                        normalized_zipcode = re.sub(r"\D", "", zipcode or "")
                        cep_input_1 = await page.query_selector('#GLUXZipUpdateInput_0')
                        cep_input_2 = await page.query_selector('#GLUXZipUpdateInput_1')
                        
                        if cep_input_1 and cep_input_2 and len(normalized_zipcode) == 8:
                            p1, p2 = normalized_zipcode[:5], normalized_zipcode[5:]
                            await cep_input_1.fill(p1)
                            await cep_input_2.fill(p2)
                        else:
                            single_input = await page.query_selector('#GLUXZipUpdateInput')
                            if single_input:
                                await single_input.fill(normalized_zipcode or zipcode)
                        
                        apply_btn = await page.query_selector('#GLUXZipUpdate, input[aria-labelledby="GLUXZipUpdate-announce"], #GLUXZipUpdate-announce')
                        if apply_btn:
                            await apply_btn.click()
                            await asyncio.sleep(3)
                            
                            # Recarrega a página ou a div de frete
                            confirm_btn = await page.query_selector('#GLUXConfirmClose, input[aria-labelledby="GLUXConfirmClose-announce"]')
                            if confirm_btn:
                                try:
                                    await confirm_btn.click()
                                    await asyncio.sleep(1)
                                except Exception as e:
                                    logger.debug("Amazon: falha ao confirmar/fechar o modal de CEP: %s", e)
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=25000)
                                await asyncio.sleep(2)
                            except Exception:
                                await asyncio.sleep(2)
                            parsed_shipping = self._parse_shipping_text(await self._read_delivery_text(page))
                            if parsed_shipping:
                                return parsed_shipping
                            delivery_message = await page.query_selector('#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE')
                            if delivery_message:
                                text = await delivery_message.inner_text()
                                if "grátis" in text.lower():
                                    return {"is_free_shipping": True, "shipping_price": 0.0}
                                else:
                                    import re
                                    match = re.search(r'R\$\s*(\d+,\d{2})', text)
                                    if match:
                                        return {
                                            "is_free_shipping": False, 
                                            "shipping_price": float(match.group(1).replace(',', '.'))
                                        }
                    return {
                        "error": "Não foi possível localizar o bloco de frete da Amazon após informar o CEP."
                    }
                finally:
                    await browser.close()
                                    
        except Exception as e:
            logger.error(f"Erro ao calcular frete avançado na Amazon: {e}")
            
        return None
