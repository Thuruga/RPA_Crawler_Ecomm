import json
import logging
import urllib.parse
import re
from typing import List, Dict, Any, Optional, Callable
import asyncio
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

from config import relevance_settings
from services.engines.base_engine import BaseEngine
from services.attribute_parsing import extract_color_fallback, extract_size_fallback

logger = logging.getLogger(__name__)


class NetshoesEngine(BaseEngine):
    # Tier 2 shipping failure must never trip the marketplace circuit breaker
    SHIPPING_TIER2_BLOCKS_ON_NONE = False

    def __init__(self, brand_key: str = "netshoes"):
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
        return "Netshoes"

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        raise NotImplementedError("Netshoes engine is for search only.")

    async def discover_categories(self) -> List[Dict[str, Any]]:
        return []

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        try:
            async with AsyncSession(impersonate="chrome120", timeout=8) as session:
                response = await session.get(product_url, headers=self.headers)
                if response.status_code == 200:
                    state = self._extract_initial_state(response.text)
                    if state:
                        return self._extract_seller_price(state)
        except Exception as e:
            logger.debug(f"curl_cffi falhou para detalhes Netshoes {product_url}: {e}")

        # Fallback Playwright Firefox quando curl_cffi recebe 403 do WAF Akamai
        html = await asyncio.to_thread(self._render_pdp_html, product_url)
        if html:
            state = self._extract_initial_state(html)
            if state:
                return self._extract_seller_price(state)
        return None

    async def get_pdp_product(self, product_url: str) -> Optional[Dict[str, Any]]:
        """
        Extrai o produto COMPLETO da PDP da Netshoes (para o monitor de preço).

        Fonte: window.__INITIAL_STATE__ → Product.currentProduct (SSR).

        1ª tentativa: curl_cffi (rápido). Em caso de 403 (anti-bot) ou HTML sem
        __INITIAL_STATE__ parseável, faz fallback para Playwright (mesmo padrão do
        ML/Amazon) — sem o fallback um único 403 dead-endava em None → "Pendente"
        eterno. Retorna um dict compatível com RawProductBronze.
        """
        # --- Tentativa 1: curl_cffi ---
        try:
            async with AsyncSession(impersonate="chrome120", timeout=15) as session:
                response = await session.get(product_url, headers=self.headers)
                if response.status_code == 200:
                    product = self._extract_pdp(response.text, product_url)
                    if product:
                        return product
                    logger.warning(
                        "Netshoes PDP %s: 200 OK mas produto não extraído (curl_cffi) — tentando Playwright.",
                        product_url,
                    )
                else:
                    logger.warning(
                        "Netshoes PDP %s retornou status %s (curl_cffi) — tentando Playwright.",
                        product_url, response.status_code,
                    )
        except Exception as e:
            logger.warning("Erro Netshoes PDP via curl_cffi %s: %s — tentando Playwright.", product_url, e)

        # --- Tentativa 2: Playwright (renderiza a SSR quando curl_cffi é bloqueado) ---
        html = await asyncio.to_thread(self._render_pdp_html, product_url)
        if not html:
            logger.warning(
                "Netshoes PDP %s: Playwright não retornou HTML (anti-bot persistente).",
                product_url,
            )
            return None
        product = self._extract_pdp(html, product_url)
        if not product:
            logger.warning(
                "Netshoes PDP %s: Playwright renderizou HTML mas produto não extraído "
                "(title=%r, __INITIAL_STATE__=%s, ld+json=%d, len=%d).",
                product_url, self._page_title(html),
                "window.__INITIAL_STATE__" in html, len(self._jsonld_blocks(html)), len(html),
            )
        return product

    def _extract_pdp(self, html: str, product_url: str) -> Optional[Dict[str, Any]]:
        """Extrai o produto da PDP tentando, em ordem: __INITIAL_STATE__ (SSR
        autoritativo) → JSON-LD Product → meta tags (og/product). O anti-bot da
        Netshoes muda a estrutura entregue, então múltiplas fontes dão resiliência."""
        product = self._build_pdp_product(self._extract_initial_state(html), product_url)
        if product:
            return product
        product = self._pdp_from_jsonld(html, product_url)
        if product:
            return product
        return self._pdp_from_meta(html, product_url)

    def _jsonld_blocks(self, html: str) -> List[dict]:
        blocks: List[dict] = []
        for script in BeautifulSoup(html, "html.parser").find_all(
            "script", type="application/ld+json"
        ):
            try:
                data = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            if isinstance(data, list):
                blocks.extend(b for b in data if isinstance(b, dict))
            elif isinstance(data, dict):
                blocks.append(data)
        return blocks

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

    def _pdp_from_jsonld(self, html: str, product_url: str) -> Optional[Dict[str, Any]]:
        for block in self._jsonld_blocks(html):
            if block.get("@type") != "Product":
                continue
            title = block.get("name") or ""
            offers = block.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price_full: Optional[float] = None
            try:
                price_full = float(offers.get("price")) if offers.get("price") else None
            except (TypeError, ValueError):
                price_full = None
            if not title or not price_full or price_full <= 0:
                continue
            image = block.get("image") or ""
            if isinstance(image, list):
                image = image[0] if image else ""
            availability = str(offers.get("availability", "")).lower()
            rating, review_count = self._aggregate_rating(block.get("aggregateRating"))
            attributes = self._normalize_explicit_attributes(block)
            return {
                "url": product_url,
                "brand": self.brand_key,
                "raw_title": title,
                "raw_description": block.get("description") or title,
                "price_full": price_full,
                "image_url": image or None,
                "stock_availability": ("instock" in availability) if availability else True,
                **attributes,
                "rating": rating,
                "review_count": review_count,
            }
        return None

    def _pdp_from_meta(self, html: str, product_url: str) -> Optional[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")

        def _meta(*names: str) -> Optional[str]:
            for name in names:
                tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
                if tag and tag.get("content"):
                    return tag["content"].strip()
            return None

        title = _meta("og:title") or self._page_title(html)
        price_raw = _meta("product:price:amount", "og:price:amount")
        price_full: Optional[float] = None
        if price_raw:
            try:
                price_full = float(price_raw.replace(",", "."))
            except ValueError:
                price_full = None
        if not title or not price_full or price_full <= 0:
            return None
        image = _meta("og:image")
        if image and image.startswith("//"):
            image = f"https:{image}"
        return {
            "url": product_url,
            "brand": self.brand_key,
            "raw_title": title,
            "raw_description": _meta("og:description") or title,
            "price_full": price_full,
            "image_url": image or None,
            "stock_availability": True,
            **self._normalize_explicit_attributes({}),
        }

    def _page_title(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        return soup.title.string.strip() if soup.title and soup.title.string else ""

    def _render_pdp_html(self, product_url: str) -> Optional[str]:
        """Renderiza a PDP da Netshoes via Playwright Firefox (bypass Akamai JS challenge)."""
        try:
            from playwright.sync_api import sync_playwright
            import time

            with sync_playwright() as p:
                browser = p.firefox.launch(headless=True)
                page = browser.new_page()
                page.goto(product_url, timeout=30000)
                # Aguarda o desafio JS da Akamai resolver e carregar o estado
                for _ in range(25):
                    if "window.__INITIAL_STATE__" in page.content():
                        break
                    time.sleep(0.5)
                html = page.content()
                browser.close()
                return html
        except Exception as exc:
            logger.error(f"Erro Netshoes PDP via Playwright Firefox {product_url}: {exc}")
            return None

    def _render_search_html(self, search_url: str) -> Optional[str]:
        """Renderiza a página de busca da Netshoes via Playwright Firefox (bypass Akamai JS challenge)."""
        try:
            from playwright.sync_api import sync_playwright
            import time

            with sync_playwright() as p:
                browser = p.firefox.launch(headless=True)
                page = browser.new_page()
                page.goto(search_url, timeout=30000)
                # Aguarda o desafio JS da Akamai resolver e carregar o catálogo
                for _ in range(25):
                    if "window.__INITIAL_STATE__" in page.content():
                        break
                    time.sleep(0.5)
                html = page.content()
                browser.close()
                return html
        except Exception as exc:
            logger.error(f"Erro Netshoes busca via Playwright Firefox {search_url}: {exc}")
            return None

    def _build_pdp_product(
        self, state: Optional[dict], product_url: str
    ) -> Optional[Dict[str, Any]]:
        product_state = (state or {}).get("Product", {}).get("currentProduct", {})
        if not product_state:
            return None

        title = (
            product_state.get("name")
            or product_state.get("productName")
            or product_state.get("title")
            or ""
        )

        # Preço autoritativo da variante exibida (saleInCents).
        price_obj = product_state.get("price") or (
            product_state.get("prices", [{}])[0] if product_state.get("prices") else {}
        )
        price_full: Optional[float] = None
        if isinstance(price_obj, dict):
            sale_in_cents = price_obj.get("saleInCents")
            if isinstance(sale_in_cents, (int, float)) and sale_in_cents > 0:
                price_full = float(sale_in_cents) / 100

        # Imagem
        image_url = product_state.get("image") or ""
        images = product_state.get("images")
        if not image_url and isinstance(images, list) and images:
            first = images[0]
            image_url = first.get("url", "") if isinstance(first, dict) else str(first)
        if image_url and image_url.startswith("//"):
            image_url = f"https:{image_url}"

        # Disponibilidade
        available = product_state.get("available")
        if available is None:
            available = bool(price_full and price_full > 0)

        # Tamanhos / cores quando disponíveis (D-01: somente estrutura explicita
        # das SKUs — nunca inferido de titulo/slug; ausencia produz []).
        raw_sizes: List[str] = []
        raw_colors: List[str] = []
        for sku in product_state.get("skus", []) or []:
            if not isinstance(sku, dict):
                continue
            size = sku.get("size") or ""
            if size and size not in raw_sizes:
                raw_sizes.append(size)
            color = sku.get("color") or ""
            if color and color not in raw_colors:
                raw_colors.append(color)

        if not title or price_full is None:
            return None
        rating, review_count = self._product_state_rating(product_state)
        attributes = self._normalize_explicit_attributes(
            {"color": raw_colors, "size": raw_sizes}
        )

        # D-03: product_code vem SOMENTE da chave product-level id/productId de
        # __INITIAL_STATE__.Product.currentProduct — nunca do primeiro SKU/slug.
        # Ausencia explicita produz None (fail-closed), nunca um ID inventado.
        product_code = product_state.get("id") or product_state.get("productId")

        return {
            "url": product_url,
            "brand": self.brand_key,
            "raw_title": title,
            "raw_description": product_state.get("description") or title,
            "price_full": price_full,
            "image_url": image_url or None,
            "stock_availability": bool(available),
            "product_code": product_code,
            **attributes,
            "rating": rating,
            "review_count": review_count,
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
    def _product_state_rating(product_state: dict[str, Any]) -> tuple[Optional[float], Optional[int]]:
        rating = None
        count = None
        for key in ("rating", "ratingValue", "averageRating"):
            try:
                if product_state.get(key) is not None:
                    rating = round(float(str(product_state.get(key)).replace(",", ".")), 1)
                    break
            except (TypeError, ValueError):
                continue
        for key in ("reviewCount", "reviewsCount", "ratingCount"):
            try:
                if product_state.get(key) is not None:
                    count = int(str(product_state.get(key)).replace(".", ""))
                    break
            except (TypeError, ValueError):
                continue
        return rating, count

    def _extract_seller_price(self, state: Optional[dict]) -> Dict[str, Any]:
        """
        Extrai vendedor e preço autoritativo da PDP a partir do __INITIAL_STATE__.

        O preço da PDP (``price.saleInCents``) é a fonte da verdade da variante/
        vendedor específicos — diverge do preço da listagem de busca, que aponta
        para uma variante representativa do parentSku. Sempre em centavos.
        """
        seller = "Netshoes"
        price: Optional[float] = None

        product_state = (state or {}).get("Product", {}).get("currentProduct", {})
        price_obj = product_state.get("price") or (
            product_state.get("prices", [{}])[0] if product_state.get("prices") else {}
        )

        if isinstance(price_obj, dict):
            seller_obj = price_obj.get("seller", {})
            if isinstance(seller_obj, dict) and seller_obj.get("name"):
                seller = seller_obj.get("name")

            sale_in_cents = price_obj.get("saleInCents")
            if isinstance(sale_in_cents, (int, float)) and sale_in_cents > 0:
                price = float(sale_in_cents) / 100

        return {"seller": seller, "price": price}

    def _extract_initial_state(self, html: str) -> Optional[dict]:
        """
        Extrai o window.__INITIAL_STATE__ do HTML da Netshoes.
        A Netshoes injeta um JSON grande diretamente no HTML (SSR).
        """
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
        if not match:
            return None

        start = match.end()
        # Percorre o JSON manualmente para encontrar o fim do objeto raiz
        depth = 0
        in_string = False
        escape_next = False
        i = start

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
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        # Fim do JSON raiz
                        json_str = html[start : i + 1]
                        try:
                            return json.loads(json_str)
                        except Exception as e:
                            logger.warning(f"Falha ao parsear __INITIAL_STATE__: {e}")
                            return None

        return None

    def _parse_from_initial_state(self, state: dict, limit: int) -> List[Dict[str, Any]]:
        """Parse dos produtos a partir do __INITIAL_STATE__ da Netshoes."""
        produtos = []
        urls_vistas = set()

        search_page = state.get("SearchPage", {})
        items = search_page.get("parentSkus", [])

        if not items:
            # Tenta outras chaves possíveis
            for key in ["products", "items", "results"]:
                items = state.get(key, [])
                if items:
                    break

        for item in items[:limit]:
            try:
                title = item.get("name", "") or item.get("productName", "")
                url_slug = item.get("productSlug", "") or item.get("slug", "")

                # Desescapa unicode (ex: \u002F -> /)
                if url_slug:
                    url_slug = url_slug.encode().decode("unicode_escape") if "\\u" in url_slug else url_slug

                # Preço — a Netshoes SEMPRE armazena em centavos (ex: 9990 = R$ 99,90)
                sale_price_raw = item.get("salePrice", 0) or item.get("price", 0)
                list_price_raw = item.get("listPrice", 0) or sale_price_raw
                try:
                    sale_price = float(sale_price_raw) / 100
                    list_price = float(list_price_raw) / 100
                except (TypeError, ValueError):
                    sale_price = 0.0
                    list_price = 0.0

                if not url_slug or sale_price <= 0:
                    continue

                url = (
                    url_slug
                    if url_slug.startswith("http")
                    else f"https://www.netshoes.com.br{url_slug}"
                )

                image = item.get("image") or ""
                if image and not image.startswith("http"):
                    image = f"https:{image}" if image.startswith("//") else f"https://www.netshoes.com.br{image}"

                seller = item.get("seller") or item.get("sellerName") or item.get("shopName") or "Netshoes"

                # D-03: product_code vem SOMENTE da chave product-level id/productId
                # do item de parentSkus — nunca do slug/URL nem de um SKU de variante.
                product_code = item.get("id") or item.get("productId")

                if url not in urls_vistas:
                    produtos.append({
                        "plataforma": "Netshoes",
                        "titulo": title,
                        "preco": sale_price,
                        "preco_original": list_price,
                        "url": url,
                        "imagem": image,
                        "seller": seller,
                        "product_code": product_code,
                    })
                    urls_vistas.add(url)

            except Exception as e:
                logger.debug(f"Erro ao parsear item Netshoes: {e}")
                continue

        return produtos
        
    def _search_products_from_jsonld(self, html: str, limit: int) -> List[Dict[str, Any]]:
        produtos = []
        urls_vistas = set()
        
        for block in self._jsonld_blocks(html):
            if block.get("@type") == "ItemList":
                items = block.get("itemListElement", [])
                for item_wrapper in items:
                    if not isinstance(item_wrapper, dict):
                        continue
                    item = item_wrapper.get("item", {})
                    if not isinstance(item, dict) or item.get("@type") != "Product":
                        continue
                        
                    title = item.get("name", "")
                    url = item.get("url", "")
                    image = item.get("image", "")
                    if isinstance(image, list) and image:
                        image = image[0]
                    sku = item.get("sku") or item.get("productID")
                    
                    price = 0.0
                    offers = item.get("offers", {})
                    if isinstance(offers, dict):
                        try:
                            price = float(offers.get("price", 0))
                        except (ValueError, TypeError):
                            price = 0.0
                            
                    if not title or not url or price <= 0:
                        continue
                        
                    if not url.startswith("http"):
                        url = f"https://www.netshoes.com.br{url}"
                        
                    if url not in urls_vistas:
                        produtos.append({
                            "plataforma": "Netshoes",
                            "titulo": title,
                            "preco": price,
                            "preco_original": price,
                            "url": url,
                            "imagem": image,
                            "seller": "Netshoes",
                            "product_code": sku,
                        })
                        urls_vistas.add(url)
                        if len(produtos) >= limit:
                            return produtos
                            
        return produtos

    def _search_products_from_cards(self, html: str, limit: int) -> List[Dict[str, Any]]:
        produtos = []
        urls_vistas = set()
        soup = BeautifulSoup(html, "html.parser")
        
        for card in soup.select(".item-card, [data-testid='product-card']"):
            try:
                a_tag = card.find("a")
                if not a_tag or not a_tag.get("href"):
                    continue
                    
                url = a_tag["href"]
                if not url.startswith("http"):
                    url = f"https://www.netshoes.com.br{url}"
                    
                title = a_tag.get("title") or a_tag.text.strip()
                if not title:
                    img_tag = card.find("img")
                    if img_tag:
                        title = img_tag.get("alt") or img_tag.get("title", "")
                        
                img_tag = card.find("img")
                image = ""
                if img_tag:
                    image = img_tag.get("data-src") or img_tag.get("src") or ""
                    if image and image.startswith("//"):
                        image = f"https:{image}"
                        
                price_str = ""
                price_el = card.select_one(".price, [data-testid='price']")
                if price_el:
                    price_str = price_el.text.strip()
                else:
                    # fallback
                    text = card.text.strip()
                    match = re.search(r'R\$\s*(\d+(?:[.,]\d+)*)', text)
                    if match:
                        price_str = match.group(1)
                        
                price = 0.0
                if price_str:
                    try:
                        clean_price = re.sub(r'[^\d,.-]', '', price_str).replace(".", "").replace(",", ".")
                        price = float(clean_price)
                    except (ValueError, TypeError):
                        pass
                        
                if not title or price <= 0:
                    continue
                    
                if url not in urls_vistas:
                    produtos.append({
                        "plataforma": "Netshoes",
                        "titulo": title,
                        "preco": price,
                        "preco_original": price,
                        "url": url,
                        "imagem": image,
                        "seller": "Netshoes",
                        "product_code": None,
                    })
                    urls_vistas.add(url)
                    if len(produtos) >= limit:
                        return produtos
                        
            except Exception as e:
                logger.debug(f"Erro no parse de card Netshoes: {e}")
                
        return produtos
        
    def _is_challenge_page(self, html: str) -> bool:
        lower_html = html.lower()
        if "access denied" in lower_html or "acesso negado" in lower_html:
            return True
        if "incapsula" in lower_html or "_incapsula" in lower_html:
            return True
        if "cf-browser-verification" in lower_html or "cloudflare" in lower_html:
            return True
        if "prove you are human" in lower_html or "security check" in lower_html:
            return True
        if "recaptcha" in lower_html and "data-sitekey" in lower_html:
            return True
        return False

    def _parse_from_parent_skus_json(self, items: list, limit: int) -> List[Dict[str, Any]]:
        produtos = []
        urls_vistas = set()
        for item in items[:limit]:
            try:
                title = item.get("name", "") or item.get("productName", "")
                url_slug = item.get("productSlug", "") or item.get("slug", "") or item.get("url", "")
                if url_slug:
                    url = url_slug if url_slug.startswith("http") else f"https://www.netshoes.com.br{url_slug}"
                else:
                    code = item.get("code") or item.get("productCode") or ""
                    url = f"https://www.netshoes.com.br/p/{code}"
                
                sale_price_raw = item.get("salePrice", 0) or item.get("price", 0)
                list_price_raw = item.get("listPrice", 0) or sale_price_raw
                try:
                    sale_price = float(sale_price_raw) / 100 if float(sale_price_raw) > 1000 else float(sale_price_raw)
                    list_price = float(list_price_raw) / 100 if float(list_price_raw) > 1000 else float(list_price_raw)
                except (TypeError, ValueError):
                    sale_price = 0.0
                    list_price = 0.0
                
                if not title or sale_price <= 0:
                    continue
                
                image = item.get("image") or item.get("imageUrl") or ""
                if image and not image.startswith("http"):
                    image = f"https:{image}" if image.startswith("//") else f"https://www.netshoes.com.br{image}"
                
                seller = item.get("seller") or item.get("sellerName") or "Netshoes"
                product_code = item.get("productCode") or item.get("code")

                if url not in urls_vistas:
                    produtos.append({
                        "plataforma": "Netshoes",
                        "titulo": title,
                        "preco": sale_price,
                        "preco_original": list_price,
                        "url": url,
                        "imagem": image,
                        "seller": seller,
                        "product_code": product_code,
                    })
                    urls_vistas.add(url)
            except Exception as e:
                logger.debug(f"Erro ao parsear parentSku Netshoes: {e}")
                continue
        return produtos

    def _parse_from_suggestion_skus(self, items: list, limit: int) -> List[Dict[str, Any]]:
        produtos = []
        urls_vistas = set()
        for item in items[:limit]:
            try:
                title = item.get("name", "")
                code = item.get("code", "")
                image = item.get("image", "")
                if not title or not code:
                    continue
                
                match = re.search(r'/produtos/([^/]+)/', image)
                if match:
                    slug = match.group(1)
                    url = f"https://www.netshoes.com.br/{slug}-{code}"
                else:
                    url = f"https://www.netshoes.com.br/p/{code}"
                
                sale_price_raw = item.get("salePrice") or item.get("price") or 0
                list_price_raw = item.get("listPrice") or sale_price_raw
                try:
                    sale_price = float(sale_price_raw) / 100 if float(sale_price_raw) > 1000 else float(sale_price_raw)
                    list_price = float(list_price_raw) / 100 if float(list_price_raw) > 1000 else float(list_price_raw)
                except (TypeError, ValueError):
                    sale_price = 0.0
                    list_price = 0.0
                
                # Se não possui preço real, não inclui preço arbitrário/hardcodado
                if sale_price <= 0:
                    continue
                
                if url not in urls_vistas:
                    produtos.append({
                        "plataforma": "Netshoes",
                        "titulo": title,
                        "preco": sale_price,
                        "preco_original": list_price,
                        "url": url,
                        "imagem": image,
                        "seller": "Netshoes",
                        "product_code": code,
                    })
                    urls_vistas.add(url)
            except Exception as e:
                logger.debug(f"Erro ao parsear suggestion sku Netshoes: {e}")
                continue
        return produtos

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> Any:  # Returns BrandSearchResult
        from core.models import BrandSearchResult, SearchProductResult, ShippingInfo
        from services.nlp_service import nlp_service

        params: dict = {"q": query}
        gender_filter = relevance_settings.NETSHOES_GENDER_FILTER.strip()
        if gender_filter:
            params["gender"] = gender_filter
        
        if sort == "recent":
            params["sort"] = "new-release"
        elif sort == "price_asc":
            params["sort"] = "lowest-first"
        elif sort == "price_desc":
            params["sort"] = "highest-first"

        produtos_dict = []
        challenge_detected = False

        # 1ª tentativa: Endpoint interno JSON de busca (/busca/api) — tentativa rápida via curl_cffi (2.5s)
        try:
            async with AsyncSession(impersonate="chrome120", timeout=2.5) as session:
                target_url = f"https://www.netshoes.com.br/busca/api?{urllib.parse.urlencode(params)}"
                target_headers = {
                    "Referer": f"https://www.netshoes.com.br/busca?{urllib.parse.urlencode(params)}",
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                }
                response = await session.get(target_url, headers=target_headers)
                if response.status_code == 200:
                    data = response.json()
                    raw_skus = data.get("parentSkus", [])
                    if raw_skus:
                        produtos_dict = self._parse_from_parent_skus_json(raw_skus, max_results * 2 if max_results is not None else 1000)
                elif response.status_code in (403, 429) or self._is_challenge_page(response.text):
                    challenge_detected = True
        except Exception as e:
            logger.debug("Tentativa busca/api Netshoes rápida ignorada: %s", e)

        # 2ª tentativa: Playwright Firefox (bypass Akamai JS challenge e SSR completo)
        # O Firefox headless resolve o script de verificação da Akamai e entrega o HTML real
        # com window.__INITIAL_STATE__, contendo catálogo, preços reais e lojistas reais.
        if not produtos_dict:
            try:
                base_url = "https://www.netshoes.com.br/busca"
                url = f"{base_url}?{urllib.parse.urlencode(params)}"
                html = await asyncio.to_thread(self._render_search_html, url)
                if html:
                    if self._is_challenge_page(html):
                        challenge_detected = True
                    state = self._extract_initial_state(html)
                    if state is not None:
                        produtos_dict = self._parse_from_initial_state(state, max_results * 2 if max_results is not None else 1000)
                    if not produtos_dict:
                        produtos_dict = self._search_products_from_jsonld(html, max_results * 2 if max_results is not None else 1000)
                    if not produtos_dict:
                        produtos_dict = self._search_products_from_cards(html, max_results * 2 if max_results is not None else 1000)
            except Exception as e:
                logger.warning("Tentativa Playwright Firefox Netshoes falhou: %s", e)

        # 3ª tentativa: Query simplificada sem cor (usando vocabulário NLP real) se nada foi encontrado
        if not produtos_dict:
            no_color_query = nlp_service.remove_colors(query)
            if no_color_query and no_color_query.lower() != query.lower():
                try:
                    alt_params = dict(params, q=no_color_query)
                    alt_url = f"https://www.netshoes.com.br/busca?{urllib.parse.urlencode(alt_params)}"
                    html = await asyncio.to_thread(self._render_search_html, alt_url)
                    if html:
                        if self._is_challenge_page(html):
                            challenge_detected = True
                        state = self._extract_initial_state(html)
                        if state is not None:
                            produtos_dict = self._parse_from_initial_state(state, max_results * 2 if max_results is not None else 1000)
                        if not produtos_dict:
                            produtos_dict = self._search_products_from_jsonld(html, max_results * 2 if max_results is not None else 1000)
                        if not produtos_dict:
                            produtos_dict = self._search_products_from_cards(html, max_results * 2 if max_results is not None else 1000)
                except Exception as e:
                    logger.debug("Tentativa sem cores Netshoes falhou: %s", e)

        # 4ª tentativa: Fallback via curl_cffi HTTP clássico
        if not produtos_dict:
            try:
                base_url = "https://www.netshoes.com.br/busca"
                url = f"{base_url}?{urllib.parse.urlencode(params)}"
                async with AsyncSession(impersonate="chrome120", timeout=10) as session:
                    response = await session.get(url, headers={"Referer": "https://www.netshoes.com.br/"})
                    if response.status_code == 200:
                        html = response.text
                        if self._is_challenge_page(html):
                            challenge_detected = True
                        state = self._extract_initial_state(html)
                        if state is not None:
                            produtos_dict = self._parse_from_initial_state(state, max_results * 2 if max_results is not None else 1000)
                        if not produtos_dict:
                            produtos_dict = self._search_products_from_jsonld(html, max_results * 2 if max_results is not None else 1000)
                        if not produtos_dict:
                            produtos_dict = self._search_products_from_cards(html, max_results * 2 if max_results is not None else 1000)
                    elif response.status_code in (403, 429):
                        challenge_detected = True
            except Exception as e:
                logger.debug("Fallback HTTP curl_cffi Netshoes falhou: %s", e)

        if not produtos_dict:
            logger.warning(f"Netshoes: produtos não encontrados para '{query}'")
            return BrandSearchResult(
                brand_key=self.brand_key,
                brand_name="Netshoes",
                products=[],
                total_found=0,
                error="Challenge page detectada (Access Denied / Akamai)" if challenge_detected else None,
            )

        produtos_dict = self.filter_mens_fashion(produtos_dict)
        if max_results is not None:
            produtos_dict = produtos_dict[:max_results]
        logger.info(f"Netshoes: {len(produtos_dict)} produtos encontrados para '{query}'")
        
        products = []
        for p in produtos_dict:
            shipping = ShippingInfo(status="Calculado no checkout", price=0.0 if False else None) if include_shipping else None
            products.append(SearchProductResult(
                brand="Netshoes",
                product_name=p["titulo"],
                url=p["url"],
                price_full=p["preco"],
                image_url=p["imagem"],
                available=True,
                seller=p.get("seller", "Netshoes"),
                product_code=p.get("product_code"),
                shipping=shipping,
                is_free_shipping=False,
                shipping_price=None
            ))
            
        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name="Netshoes",
            products=products,
            total_found=len(products)
        )

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[Dict[str, Any]]:
        return await self.calculate_shipping_advanced(product, zipcode)

    def _run_playwright_shipping(self, url: str, zipcode: str) -> Optional[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright
        import time
        try:
            logger.info(f"Calculando frete Netshoes via Playwright para {url} com CEP {zipcode}")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
                page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
                
                # Clica no "Enviar para" no header
                enviar_btn = page.query_selector('.header__cep, button:has-text("Enviar para"), a:has-text("Enviar para")')
                if enviar_btn:
                    enviar_btn.click()
                    time.sleep(1)
                
                cep_input = page.query_selector('#cepModal')
                if not cep_input:
                    cep_input = page.query_selector('input[name="cep"], input[placeholder*="CEP"]')
                
                if not cep_input:
                    logger.warning("Netshoes: Input de CEP não encontrado na página.")
                    browser.close()
                    return None
                    
                cep_input.fill(zipcode)
                
                calc_btn = page.query_selector('#cepModal + button, button:has-text("Calcular"), .shipping-calculate__btn')
                if calc_btn:
                    calc_btn.click()
                else:
                    cep_input.press("Enter")
                    
                time.sleep(4)
                
                html = page.content()
                browser.close()
                
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                shipping_elements = soup.find_all(class_=re.compile(r'fulfillment|freight|shipping|cep'))
                
                prices = []
                for el in shipping_elements:
                    text = el.text.strip()
                    if not text:
                        continue
                    
                    if re.search(r'gr[aá]tis', text, re.IGNORECASE):
                        return {
                            "is_free_shipping": True,
                            "shipping_price": 0.0
                        }
                        
                    match = re.search(r'R\$\s*(\d+,\d{2})', text)
                    if match:
                        try:
                            val = float(match.group(1).replace(",", "."))
                            prices.append(val)
                        except Exception as e:
                            logger.debug("Netshoes: falha ao converter preço de frete '%s': %s", match.group(1), e)
                            
                if prices:
                    return {
                        "is_free_shipping": False,
                        "shipping_price": min(prices)
                    }
                    
                return None
        except Exception as e:
            logger.error(f"Erro no Playwright Netshoes: {e}")
            return None

    async def calculate_shipping_advanced(self, product_url: str, zipcode: str) -> Optional[Dict[str, Any]]:
        url = product_url if isinstance(product_url, str) else getattr(product_url, "url", "")
        if not url:
            return None
        return await asyncio.to_thread(self._run_playwright_shipping, url, zipcode)
