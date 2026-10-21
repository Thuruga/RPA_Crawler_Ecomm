import json
import logging
import math
import re
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable
import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from config import relevance_settings
from services.engines.base_engine import BaseEngine
from services.attribute_parsing import extract_color_fallback, extract_size_fallback
from core.models import BrandSearchResult, SearchProductResult, ShippingInfo
from services.engines.seller_extraction import parse_ml_seller_from_html, MARKETPLACE_DEFAULT_SELLER

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """
    Converte um texto livre para o formato de slug usado pelo Mercado Livre.

    O ML usa URLs no formato:
      lista.mercadolivre.com.br/{categoria}/tenis-slip-on-aramis

    Passos:
      1. Normaliza NFD para separar letras e diacríticos (ex: ç → c + cedilla)
      2. Remove todos os caracteres não-ASCII (diacríticos)
      3. Converte para lowercase
      4. Substitui espaços e caracteres especiais por hífens
      5. Remove hífens duplicados / leading / trailing
    """
    text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("utf-8")
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)   # mantém apenas letras, dígitos, espaços e hífens
    text = re.sub(r"[\s]+", "-", text.strip())   # espaços → hífens
    text = re.sub(r"-+", "-", text)              # hífens duplos → único
    return text


class MercadoLivreEngine(BaseEngine):
    SHIPPING_TIER2_BLOCKS_ON_NONE = False

    def __init__(self, brand_key: str = "mercado_livre"):
        self.brand_key = brand_key
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }

    def get_engine_name(self) -> str:
        return "Mercado Livre"

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None
    ):
        raise NotImplementedError("Mercado Livre engine is for search only.")

    async def discover_categories(self) -> List[Dict[str, Any]]:
        return []

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []

    def _run_playwright_pdp(self, product_url: str) -> Optional[Dict[str, Any]]:
        try:
            from playwright.sync_api import sync_playwright
            import time

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(
                    user_agent=(
                        self.headers.get("User-Agent")
                        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="pt-BR",
                )
                page = ctx.new_page()
                
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                """)
                
                page.goto(product_url, wait_until="domcontentloaded", timeout=15000)
                time.sleep(3)
                html = page.content()
                seller = parse_ml_seller_from_html(html) or MARKETPLACE_DEFAULT_SELLER["Mercado Livre"]
                browser.close()
                return {"seller": seller}
        except Exception as e:
            logger.error(f"Erro ao buscar detalhes ML via Playwright {product_url}: {e}")
            return None

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        try:
            async with AsyncSession(impersonate="chrome120", timeout=15) as session:
                response = await session.get(product_url, headers=self.headers)
                if response.status_code == 200 and "ui-pdp-seller" in response.text:
                    seller = parse_ml_seller_from_html(response.text) or MARKETPLACE_DEFAULT_SELLER["Mercado Livre"]
                    return {"seller": seller}
        except Exception as e:
            logger.debug(f"Erro no ML PDP via curl_cffi: {e}")
            
        # Tenta com Playwright como fallback
        return await asyncio.to_thread(self._run_playwright_pdp, product_url)

    async def get_pdp_product(self, product_url: str) -> Optional[Dict[str, Any]]:
        """
        Extrai o produto COMPLETO da PDP do Mercado Livre (para o monitor de preço).

        O ML protege as PDPs com o desafio Anubis (PoW): responde HTTP 200 com uma
        página de challenge que NÃO contém o JSON-LD do produto. Por isso um 200 sem
        Product também precisa cair no Playwright robusto (BrowserManager +
        networkidle — o mesmo caminho comprovado da busca, que atravessa o redirect
        do Anubis), e não só os casos de status != 200 / exceção.

        Fonte de extração: JSON-LD `application/ld+json` (Product) → fallback DOM.
        Retorna um dict compatível com RawProductBronze.
        """
        # --- Tentativa 1: curl_cffi (rápido) ---
        try:
            async with AsyncSession(impersonate="chrome120", timeout=15) as session:
                response = await session.get(product_url, headers=self.headers)
                if response.status_code == 200 and not self._is_anubis_challenge(response.text):
                    product = self._build_pdp_product(response.text, product_url)
                    if product:
                        return product
                    logger.warning(
                        "ML PDP %s: 200 OK mas Product não extraído (curl_cffi) — tentando Playwright.",
                        product_url,
                    )
                elif response.status_code == 200:
                    logger.warning(
                        "ML PDP %s: curl_cffi caiu no Anubis (200 challenge) — tentando Playwright.",
                        product_url,
                    )
                else:
                    logger.warning(
                        "ML PDP %s: curl_cffi status %s — tentando Playwright.",
                        product_url, response.status_code,
                    )
        except Exception as e:
            logger.warning("ML PDP %s: erro curl_cffi (%s) — tentando Playwright.", product_url, e)

        # --- Tentativa 2: Playwright robusto (BrowserManager resolve o Anubis PoW) ---
        try:
            from core.browser_manager import BrowserManager
            html = await BrowserManager.fetch_html(
                product_url, wait_until="networkidle", extra_sleep=8.0, timeout=60000
            )
        except Exception as exc:
            logger.error("ML PDP %s: Playwright não renderizou: %s", product_url, exc)
            return None

        if not html or self._is_anubis_challenge(html):
            logger.warning("ML PDP %s: Anubis/challenge não resolvido no Playwright.", product_url)
            return None

        product = self._build_pdp_product(html, product_url)
        if not product:
            logger.warning(
                "ML PDP %s: HTML renderizado (len=%d) mas Product/preço não extraído "
                "(ld+json blocks=%d).",
                product_url, len(html), len(self._extract_seo_json(html)),
            )
        return product

    def _build_pdp_product(
        self, html: Optional[str], product_url: str
    ) -> Optional[Dict[str, Any]]:
        if not html:
            return None

        # 1. Fonte primária: JSON-LD Product (SEO estável do ML).
        for block in self._extract_seo_json(html):
            if isinstance(block, dict) and block.get("@type") == "Product":
                result = self._pdp_from_jsonld(block, product_url)
                if result:
                    return result
                break

        # 2. Fallback: seletores da PDP renderizada (quando não há JSON-LD).
        return self._pdp_from_dom(html, product_url)

    @staticmethod
    def _explicit_attribute_values(value: Any) -> List[str]:
        candidates = value if isinstance(value, list) else [value]
        values: List[str] = []
        for candidate in candidates:
            label = str(candidate or "").strip()
            if label and label not in values:
                values.append(label)
        return values

    @classmethod
    def _normalize_explicit_attributes(cls, source: Dict[str, Any]) -> Dict[str, List[str]]:
        raw_colors = cls._explicit_attribute_values(source.get("color"))
        raw_sizes = cls._explicit_attribute_values(source.get("size"))

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

    def _pdp_from_jsonld(
        self, product_block: Dict[str, Any], product_url: str
    ) -> Optional[Dict[str, Any]]:
        title = product_block.get("name") or ""

        offers = product_block.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price_full: Optional[float] = None
        try:
            price_full = float(offers.get("price")) if offers.get("price") else None
        except (TypeError, ValueError):
            price_full = None

        availability = str(offers.get("availability", "")).lower()
        available = "instock" in availability if availability else bool(price_full)

        image = product_block.get("image") or ""
        if isinstance(image, list):
            image = image[0] if image else ""

        description = product_block.get("description") or title
        rating, review_count = self._aggregate_rating(product_block.get("aggregateRating"))
        attributes = self._normalize_explicit_attributes(product_block)

        if not title or price_full is None or price_full <= 0:
            return None

        # D-03: product_code vem do token nativo bruto exposto pelo JSON-LD
        # (campo "sku" do Product schema do ML) — preserva pontuacao/casing
        # exatos da fonte. NUNCA reconstruido a partir da URL (isso e privado
        # ao fluxo de shipping, ver MercadoLivreEngine._extract_item_id).
        product_code = product_block.get("sku") or None

        return {
            "url": product_url,
            "brand": self.brand_key,
            "raw_title": title,
            "raw_description": description,
            "price_full": price_full,
            "image_url": image or None,
            "stock_availability": bool(available),
            "product_code": product_code,
            "rating": rating,
            "review_count": review_count,
            **attributes,
        }

    def _pdp_from_dom(self, html: str, product_url: str) -> Optional[Dict[str, Any]]:
        """Fallback: extrai título/preço direto da PDP renderizada do ML.

        Usado quando o JSON-LD não está presente (ex.: variações de layout). Busca
        o preço na LINHA PRINCIPAL de preço (``.ui-pdp-price__second-line``) para não
        pegar o preço original riscado.
        """
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one(".ui-pdp-title") or soup.find("h1")
        title = title_el.get_text(strip=True) if title_el else ""

        price_full: Optional[float] = None
        price_scope = soup.select_one(".ui-pdp-price__second-line") or soup
        fraction = price_scope.select_one(".andes-money-amount__fraction")
        if fraction:
            int_part = re.sub(r"\D", "", fraction.get_text())
            cents_el = price_scope.select_one(".andes-money-amount__cents")
            cents = re.sub(r"\D", "", cents_el.get_text()) if cents_el else ""
            if int_part:
                try:
                    price_full = float(f"{int_part}.{cents or '00'}")
                except ValueError:
                    price_full = None

        img_el = soup.select_one("figure.ui-pdp-gallery__figure img, img.ui-pdp-image")
        image = (img_el.get("src") or img_el.get("data-zoom")) if img_el else None

        if not title or price_full is None or price_full <= 0:
            return None
        rating, review_count = self._dom_rating_summary(soup)
        attributes = self._normalize_explicit_attributes({})

        return {
            "url": product_url,
            "brand": self.brand_key,
            "raw_title": title,
            "raw_description": title,
            "price_full": price_full,
            "image_url": image or None,
            "stock_availability": True,
            "rating": rating,
            "review_count": review_count,
            **attributes,
        }

    @staticmethod
    def _aggregate_rating(raw: Any) -> tuple[Optional[float], Optional[int]]:
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

    @staticmethod
    def _dom_rating_summary(soup: BeautifulSoup) -> tuple[Optional[float], Optional[int]]:
        text = soup.get_text(" ", strip=True)
        rating = None
        count = None
        rating_match = re.search(r"(\d+(?:[,.]\d+)?)\s*(?:de|/)\s*5", text, re.IGNORECASE)
        if rating_match:
            try:
                rating = round(float(rating_match.group(1).replace(",", ".")), 1)
            except ValueError:
                rating = None
        count_match = re.search(
            r"([\d\.]+)\s+(?:opini(?:o|õ)es|avalia(?:c|ç)(?:o|õ)es)",
            text,
            re.IGNORECASE,
        )
        if count_match:
            try:
                count = int(count_match.group(1).replace(".", ""))
            except ValueError:
                count = None
        return rating, count

    def _extract_seo_json(self, html: str) -> List[Dict]:
        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script", type="application/ld+json")
        json_blocks = []
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, list):
                    json_blocks.extend(data)
                else:
                    json_blocks.append(data)
            except Exception:
                continue
        return json_blocks

    def _extract_product_array(self, html: str, limit: int) -> List[Dict[str, Any]]:
        """
        Extrai o array de Products do estado React/Redux do ML.
        O ML injeta os dados como: '[{"@type":"Product","name":...}]' no HTML
        dentro do estado da aplicação React.
        """
        produtos = []
        urls_vistas = set()

        match = re.search(r'\[\s*\{"@type"\s*:\s*"Product"', html)
        if not match:
            return []

        start = match.start()
        depth = 0
        in_string = False
        escape_next = False
        end = start

        for i, char in enumerate(html[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if char == "\\" and in_string:
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if not in_string:
                if char in "[{":
                    depth += 1
                elif char in "]}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

        try:
            products = json.loads(html[start:end])
        except Exception as e:
            logger.warning(f"ML: falha ao parsear array de produtos: {e}")
            return []

        for item in products[:limit]:
            try:
                name = item.get("name", "")
                offers = item.get("offers", {})
                price = float(offers.get("price", 0.0))
                url_raw = offers.get("url", "")

                # Desescapa unicode (\\u002F -> /)
                url = url_raw.replace("\\u002F", "/").replace("\\/", "/")

                image_url = item.get("image", "")
                if isinstance(image_url, list):
                    image_url = image_url[0] if image_url else ""
                    
                seller_obj = offers.get("seller", {})
                seller = (seller_obj.get("name") if isinstance(seller_obj, dict) else None) or "Mercado Livre"

                # D-03: product_code vem do token nativo bruto "sku" exposto pelo
                # array de estado do ML — preserva pontuacao/casing exatos, nunca
                # reconstruido/normalizado (isso e privado ao fluxo de shipping).
                product_code = item.get("sku") or None

                if name and price > 0 and url and url not in urls_vistas:
                    produtos.append({
                        "plataforma": "Mercado Livre",
                        "titulo": name,
                        "preco": price,
                        "url": url.split("?")[0],
                        "imagem": image_url,
                        "seller": seller,
                        "product_code": product_code,
                    })
                    urls_vistas.add(url)
            except Exception:
                continue

        return produtos

    def _parse_html(self, html: str, limit: int) -> List[Dict[str, Any]]:
        """Extrai produtos do HTML da página de resultados do ML."""
        urls_vistas = set()

        # Tática 1: Array de Products do estado React (mais confiável com Playwright)
        produtos = self._extract_product_array(html, limit)
        if produtos:
            return produtos

        # Tática 2: JSON-LD scripts (quando disponível)
        json_blocks = self._extract_seo_json(html)
        for data in json_blocks:
            if data.get("@type") == "ItemList":
                for item in data.get("itemListElement", [])[:limit]:
                    prod = item.get("item", {})
                    price_raw = prod.get("offers", {}).get("price", 0.0)
                    try:
                        price = float(price_raw)
                    except (TypeError, ValueError):
                        price = 0.0
                    url = prod.get("url", "").split("?")[0]
                    
                    image_url = prod.get("image", "")
                    if isinstance(image_url, list):
                        image_url = image_url[0] if image_url else ""
                        
                    seller_raw = prod.get("offers", {}).get("seller", {})
                    seller = (seller_raw.get("name") if isinstance(seller_raw, dict) else None) or "Mercado Livre"

                    if price > 0 and url and url not in urls_vistas:
                        produtos.append({
                            "plataforma": "Mercado Livre",
                            "titulo": prod.get("name", "Sem Título"),
                            "preco": price,
                            "url": url,
                            "imagem": image_url,
                            "seller": seller,
                        })
                        urls_vistas.add(url)
                if produtos:
                    return produtos[:limit]

        # Tática 3: Fallback HTML com seletores CSS
        soup = BeautifulSoup(html, "html.parser")
        selectors = [
            ("li", {"class": "ui-search-layout__item"}),
            ("div", {"class": "ui-search-result__wrapper"}),
            ("article", {"class": "ui-search-layout__item"}),
        ]

        items = []
        for tag, attrs in selectors:
            items = soup.find_all(tag, attrs)
            if items:
                break

        for item in items:
            if len(produtos) >= limit:
                break
            try:
                title_el = item.find("h2") or item.find("a", class_=re.compile(r"poly-component__title")) or item.find("span", class_=re.compile(r"ui-search-item__title"))
                if not title_el:
                    continue
                title = title_el.text.strip()

                a_tag = item.find("a")
                if not a_tag or not a_tag.get("href"):
                    continue
                url = a_tag["href"].split("?")[0]

                price_el = item.find("span", class_="andes-money-amount__fraction")
                if not price_el:
                    continue
                price_txt = price_el.text.replace(".", "").replace(",", "").strip()
                try:
                    price = float(price_txt)
                except ValueError:
                    continue

                img_tag = item.find("img")
                image_url = ""
                if img_tag:
                    srcset = img_tag.get("srcset", "")
                    if srcset:
                        image_url = srcset.split(",")[0].strip().split(" ")[0]
                    else:
                        image_url = img_tag.get("src") or img_tag.get("data-src") or ""

                seller_el = item.find("p", class_="ui-search-official-store-label")
                if seller_el:
                    seller_text = seller_el.text
                    clean_seller = re.sub(r'(?i)(Vendido por|por|Loja oficial)\s*', '', seller_text).strip()
                    seller = clean_seller or "Mercado Livre"
                else:
                    seller = "Mercado Livre"

                if price > 0 and url not in urls_vistas:
                    produtos.append({
                        "plataforma": "Mercado Livre",
                        "titulo": title,
                        "preco": price,
                        "url": url,
                        "imagem": image_url,
                        "seller": seller,
                    })
                    urls_vistas.add(url)
            except Exception:
                continue

        return produtos[:limit]

    def _is_anubis_challenge(self, html: str) -> bool:
        """Detecta se o ML retornou a página de bot-challenge (Anubis/PoW) ou bloqueio de tráfego."""
        signals = [
            "anubis" in html.lower(),
            "proof of work" in html.lower(),
            "pow" in html.lower() and "challenge" in html.lower(),
            "/_anubis/" in html,
            "window.__ANUBIS" in html,
            "account-verification" in html,
            "suspicious-traffic" in html,
            "loginType=negative_traffic" in html,
            "Olá! Para continuar, acesse" in html,
        ]
        return any(signals)

    async def _search_with_playwright(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """
        Usa BrowserManager (Playwright) para contornar o bot challenge (Anubis/Account Verification) do Mercado Livre.
        """
        from core.browser_manager import BrowserManager
        
        query_slug = _slugify(query)
        url = f"https://lista.mercadolivre.com.br/{query_slug}"

        try:
            # Anubis é um desafio proof-of-work: a página inicial roda um JS que calcula o PoW,
            # seta cookie e REDIRECIONA para a página real. Com 'domcontentloaded' + 5s o HTML era
            # lido ANTES do redirect (challenge ainda presente). 'networkidle' aguarda a rede
            # silenciar — ou seja, atravessa o redirect do Anubis — e o extra_sleep dá folga para
            # o array de produtos React hidratar. Verificado: resolve o Anubis e retorna ~10 itens.
            html = await BrowserManager.fetch_html(
                url,
                wait_until="networkidle",
                extra_sleep=10.0,
                timeout=60000
            )

            if self._is_anubis_challenge(html):
                logger.warning(f"ML Playwright: Anubis challenge ou bloqueio não resolvido para '{query}'")
                return []
                
            return self._parse_html(html, max_results)
        except Exception as e:
            logger.error(f"Erro Playwright ML: {e}")
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
        Busca produtos no Mercado Livre sem hardcode de categorias.
        Tenta primeiro via curl_cffi. Se falhar, usa Playwright.
        A busca usa a URL universal do Mercado Livre via slug da query.
        """
        query_slug = _slugify(query)
        url = f"https://lista.mercadolivre.com.br/{query_slug}"
        timeout_curl = relevance_settings.ML_TIMEOUT_CURL_SECONDS
        max_fetch = max_results * 2 if max_results is not None else 1000
        fallback_error = None
        raw_items: List[Dict[str, Any]] = []

        logger.info(f"ML: tentando URL de busca: {url}")
        try:
            async with AsyncSession(
                impersonate="chrome120", timeout=timeout_curl
            ) as session:
                response = await session.get(url, headers=self.headers)
                if response.status_code == 200:
                    if self._is_anubis_challenge(response.text):
                        logger.warning(f"ML curl_cffi: Anubis challenge na busca para '{query}'")
                    else:
                        raw_items = self._parse_html(response.text, max_fetch)
                        if raw_items:
                            logger.info(f"✅ ML curl_cffi: {len(raw_items)} produtos para '{query}'")
                        else:
                            logger.warning(f"ML curl_cffi: 0 produtos após parse/filtro para '{query}'")
                else:
                    logger.warning(f"ML curl_cffi: HTTP {response.status_code} para '{query}'")
                    fallback_error = f"HTTP {response.status_code}"
        except Exception as exc:
            logger.error(f"ML curl_cffi erro: {exc}")
            fallback_error = str(exc)

        if not raw_items:
            logger.info(f"ML: fallback para Playwright para '{query}'...")
            raw_items = await self._search_with_playwright(query, max_fetch)
            if not raw_items:
                fallback_error = fallback_error or "CAPTCHA/Anubis — Playwright bloqueado"

        filtered = self.filter_mens_fashion(raw_items)
        if max_results is not None:
            filtered = filtered[:max_results]
        products = []
        for item in filtered:
            # Tier 1: Check if "Frete grátis" might be visible in HTML (placeholder, we will do API in Tier 2)
            is_free = False
            
            shipping = ShippingInfo(status="Calculado no checkout", price=0.0 if is_free else None) if include_shipping else None
            products.append(SearchProductResult(
                brand="Mercado Livre",
                product_name=item["titulo"],
                url=item["url"],
                price_full=item["preco"],
                price_discount=item.get("preco_desconto"),

                image_url=item["imagem"],
                available=True,
                seller=item.get("seller", "Mercado Livre"),
                product_code=item.get("product_code"),
                shipping=shipping,
                is_free_shipping=is_free,
                shipping_price=0.0 if is_free else None
            ))

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name="Mercado Livre",
            products=products,
            total_found=len(products),
            error=fallback_error if not products else None,
        )

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[Dict[str, Any]]:
        """
        Tier 2: Chamada extra para a API do Mercado Livre para calcular frete nominal.
        Extrai o Item ID da URL (ex: MLB123456) e chama /shipping_options.
        """
        url = ""
        if isinstance(product, str):
            url = product
        elif isinstance(product, dict):
            url = product.get("url", "")
        else:
            url = getattr(product, "url", "")
            
        if not url:
            return None

        item_id = self._extract_item_id(url)
        if not item_id:
            item_id = await self._resolve_item_id_from_page(url)
        if not item_id:
            logger.warning(f"ML: não foi possível extrair item_id da URL {url}")
            return None

        return await self._fetch_shipping_options(item_id, zipcode)

    def _extract_item_id(self, text: str) -> Optional[str]:
        match = re.search(r'\bMLB-?(\d{6,})\b', text.upper())
        if match:
            return f"MLB{match.group(1)}"
        return None

    async def _resolve_item_id_from_page(self, url: str) -> Optional[str]:
        try:
            async with AsyncSession(impersonate="chrome120", timeout=10) as session:
                response = await session.get(url, headers=self.headers)
                if response.status_code != 200:
                    logger.debug(f"ML: HTTP {response.status_code} ao resolver item_id via PDP")
                    return None

                html = response.text
                candidates = [
                    r'"itemId"\s*:\s*"(MLB\d+)"',
                    r'"item_id"\s*:\s*"(MLB\d+)"',
                    r'"id"\s*:\s*"(MLB\d+)"',
                    r'data-item-id=["\'](MLB\d+)["\']',
                    r'/items/(MLB\d+)',
                ]
                for pattern in candidates:
                    match = re.search(pattern, html, re.IGNORECASE)
                    if match:
                        return match.group(1).upper()
        except Exception as e:
            logger.debug(f"ML: erro ao resolver item_id via PDP: {e}")
        return None

    def _parse_delivery_time(self, options: list) -> Dict[str, Any]:
        """
        Extrai prazo de entrega das opcoes de frete (D-02). Confianca MEDIA
        (RESEARCH.md A1): o campo estimated_delivery_time e opcional e seu
        shape pode variar (unit/time_frame/date); toda leitura usa .get()
        para degradar graciosamente a cost-only quando ausente.

        Verificacao ao vivo (Plan 42-03, live smoke): a API real de
        /shipping_options nao retorna time_frame.to nem unit+time — ela
        retorna um `date` absoluto em ISO-8601 (ex: "2026-07-02T00:00:00-03:00").
        Quando so o `date` esta presente, calculamos estimated_delivery_days
        a partir da diferenca (em dias, arredondada para cima) entre essa data
        e agora. delivery_raw_text SO recebe o `date` bruto quando o parse da
        data falha (fallback defensivo) — o frontend prefere raw_text sobre
        estimated_delivery_days na cadeia de fallback (App.tsx), entao nunca
        devemos deixar a string ISO crua vazar como raw_text quando ja
        conseguimos calcular os dias com sucesso.
        """
        delivery: Dict[str, Any] = {}
        for opt in options:
            estimate = opt.get("estimated_delivery_time") or {}
            if not isinstance(estimate, dict):
                continue
            try:
                logger.debug("ML: estimated_delivery_time shape=%s", list(estimate.keys()))
            except Exception:
                pass

            date_str = estimate.get("date")
            time_frame = estimate.get("time_frame") or {}
            unit = estimate.get("unit")
            days_resolved = False

            if isinstance(time_frame, dict) and time_frame.get("to") is not None:
                try:
                    delivery["estimated_delivery_days"] = int(time_frame["to"])
                    days_resolved = True
                except (TypeError, ValueError):
                    pass
            elif unit and str(unit).lower() in ("day", "dias", "d") and estimate.get("time") is not None:
                try:
                    delivery["estimated_delivery_days"] = int(estimate["time"])
                    days_resolved = True
                except (TypeError, ValueError):
                    pass
            elif date_str:
                days = self._days_from_iso_date(str(date_str))
                if days is not None:
                    delivery["estimated_delivery_days"] = days
                    days_resolved = True

            # delivery_raw_text is the defensive fallback: only surface the raw
            # value when we could NOT turn it into a usable day-count, so the
            # frontend's raw_text-before-estimated_delivery_days fallback chain
            # never displays a machine-formatted ISO timestamp to the user.
            if date_str and not days_resolved:
                delivery["delivery_raw_text"] = str(date_str)
            if delivery:
                break
        return delivery

    @staticmethod
    def _days_from_iso_date(date_str: str) -> Optional[int]:
        """
        Converte uma data absoluta ISO-8601 (ex: "2026-07-02T00:00:00-03:00",
        retornada ao vivo por /shipping_options) na quantidade de dias entre
        agora e essa data, arredondada para cima (math.ceil) para que "mais
        tarde hoje" ainda conte como >= 1 dia. Nunca levanta excecao — retorna
        None em qualquer falha de parse (degradacao graciosa, cost-only).
        """
        try:
            parsed = datetime.fromisoformat(date_str)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta_seconds = (parsed - now).total_seconds()
            days = math.ceil(delta_seconds / 86400)
            return max(days, 0)
        except (ValueError, TypeError, OverflowError):
            return None

    async def _fetch_shipping_options(self, item_id: str, zipcode: str) -> Optional[Dict[str, Any]]:
        api_url = f"https://api.mercadolibre.com/items/{item_id}/shipping_options?zip_code={zipcode}"

        try:
            async with AsyncSession(impersonate="chrome120", timeout=10) as session:
                response = await session.get(api_url)
                if response.status_code == 200:
                    data = response.json()
                    options = data.get("options", [])
                    if not options:
                        return {"is_free_shipping": False, "shipping_price": None}

                    # Pega a opção mais barata, ou a padrão
                    prices = [float(opt.get("cost", 0.0)) for opt in options]
                    highest_price = max(prices) if prices else 0.0 # user context: assume highest for ranges/options to be safe

                    is_free = any(opt.get("cost") == 0 for opt in options)
                    # If free is available, maybe shipping_price is 0? The user said "pior cenário".
                    # But if free shipping is an option, it's free. Let's use the max price of the options if it's not strictly free

                    # Actually, ML usually returns 1-2 options (normal, expresso).
                    # We will take the max cost if it's not free shipping.
                    shipping_price = 0.0 if is_free else highest_price

                    result: Dict[str, Any] = {
                        "is_free_shipping": is_free,
                        "shipping_price": shipping_price
                    }
                    result.update(self._parse_delivery_time(options))
                    return result
                logger.debug(f"ML: shipping_options HTTP {response.status_code} para {item_id}")
        except Exception as e:
            logger.debug(f"Erro ao calcular frete no ML para {item_id}: {e}")

        return None

    def _run_playwright_shipping(self, url: str, zipcode: str) -> Optional[Dict[str, Any]]:
        try:
            logger.info(f"Calculando frete ML via Playwright para {url} com CEP {zipcode}")
            from playwright.sync_api import sync_playwright
            import time
            import re
            
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True, 
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
                )
                ctx = browser.new_context(
                    user_agent=(
                        self.headers.get("User-Agent")
                        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="pt-BR",
                )
                page = ctx.new_page()
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                """)
                
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(5)
                
                html = page.content()
                rendered_text = ""
                try:
                    rendered_text = f"{page.title()}\n{page.locator('body').inner_text(timeout=5000)}"
                except Exception as e:
                    logger.debug(f"Erro ao ler texto renderizado no ML: {e}")
                if re.search(r"frete\s+gr.tis", f"{html}\n{rendered_text}", re.IGNORECASE):
                    browser.close()
                    return {"is_free_shipping": True, "shipping_price": 0.0}
                
                # Checa frete grátis visível direto no HTML
                if "frete grátis" in html.lower():
                    browser.close()
                    return {"is_free_shipping": True, "shipping_price": 0.0}

                if "frete grátis" in rendered_text.lower():
                    browser.close()
                    return {"is_free_shipping": True, "shipping_price": 0.0}
                    
                match = re.search(r'Chegar[a-záéíóú]*.*?por\s*R\$\s*(\d+,\d{2})', html, re.IGNORECASE)
                if match:
                    price = float(match.group(1).replace(',', '.'))
                    browser.close()
                    return {"is_free_shipping": False, "shipping_price": price}
                    
                cep_btn = page.query_selector('.ui-pdp-media__action, .andes-tooltip-button-close')
                if cep_btn:
                    try:
                        cep_btn.click()
                        time.sleep(1)
                        cep_input = page.query_selector('input[name="zipcode"]')
                        if cep_input:
                            cep_input.fill(zipcode)
                            cep_input.press("Enter")
                            time.sleep(3)
                            html_after = page.content()
                            if "frete grátis" in html_after.lower():
                                browser.close()
                                return {"is_free_shipping": True, "shipping_price": 0.0}
                            match_after = re.search(r'por\s*R\$\s*(\d+,\d{2})', html_after, re.IGNORECASE)
                            if match_after:
                                price = float(match_after.group(1).replace(',', '.'))
                                browser.close()
                                return {"is_free_shipping": False, "shipping_price": price}
                    except Exception as e:
                        logger.debug(f"Erro ao interagir com form de CEP no ML: {e}")
                
                browser.close()
                
        except Exception as e:
            logger.error(f"Erro ao calcular frete avançado no ML: {e}")
            
        return None

    async def calculate_shipping_advanced(self, url: str, zipcode: str) -> Optional[Dict[str, Any]]:
        """
        Calcula o frete real usando a API pública do ML com curl_cffi (impersonation).
        O Playwright está sendo bloqueado por Login Wall, mas a API funciona.
        """
        shipping_info = await self.calculate_shipping(url, zipcode)
        if shipping_info:
            return shipping_info
        return await asyncio.to_thread(self._run_playwright_shipping, url, zipcode)

