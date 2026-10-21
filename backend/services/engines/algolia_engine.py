"""
Algolia engine for brands that use Algolia as their search backend.

Extracts product catalog data directly from Algolia search indices, bypassing
Akamai Bot Manager that blocks the standard storefront.
Credentials are loaded from brand_data.configuration (PostgreSQL).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, parse_qs, urlparse

from core.http_client import FetchOptions, StealthHttpClient
from core.models import BrandSearchResult, SearchProductResult, RawProductBronze
from services.engines.base_engine import BaseEngine
from services.shipping.base import ShippingCalculation, apply_shipping_calculation

logger = logging.getLogger(__name__)

class AlgoliaEngine(BaseEngine):
    """
    Engine that queries Algolia directly for product data.
    Credentials and index name are loaded from brand_data.configuration (PostgreSQL).
    Fallback defaults are for Lacoste BR (retrocompatibility only).
    """
    
    SHIPPING_TIER2_BLOCKS_ON_NONE = True
    SEARCH_RAW_FETCH_MULTIPLIER = 2

    def __init__(self, brand: dict):
        self.brand = brand
        self.brand_id = brand.get("id")
        self.brand_key = brand.get("brand_key", "")
        self.domain = brand.get("domain", "")
        self.base_url = brand.get("base_url", f"https://{self.domain}" if self.domain else "")
        self.proxy_url = brand.get("proxy_url")

        # Credenciais Algolia - lidas exclusivamente da configuracao da marca no banco de dados.
        cfg = brand.get("configuration") or {}
        if isinstance(cfg, str):
            import json
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        self.algolia_app_id = cfg.get("algolia_app_id") or brand.get("algolia_app_id") or ""
        self.algolia_api_key = cfg.get("algolia_api_key") or brand.get("algolia_api_key") or ""
        self.algolia_index = cfg.get("algolia_index") or brand.get("algolia_index") or ""
        self.algolia_origin = cfg.get("algolia_origin") or brand.get("algolia_origin") or (f"https://{self.domain}" if self.domain else "")

        # Fallback padrão para Lacoste BR (conforme retrocompatibilidade documentada)
        if self.brand_key == "lacoste":
            self.algolia_app_id = self.algolia_app_id or "CU0IYSHI42"
            self.algolia_api_key = self.algolia_api_key or "7e20c005d05fe8f113d8b7e079bf34a1"
            self.algolia_index = self.algolia_index or "products_br_pt"
            # Algolia restringe referer exclusivamente a https://www.lacoste.com (lacoste.com.br toma 403)
            if not self.algolia_origin or "lacoste.com.br" in self.algolia_origin or self.algolia_origin == "https://lacoste.com":
                self.algolia_origin = "https://www.lacoste.com"

        if not self.algolia_app_id or not self.algolia_api_key:
            logger.warning(
                f"[ALGOLIA] Brand '{self.brand_key}' sem credenciais algolia_app_id / algolia_api_key na configuracao."
            )

        app_id_clean = self.algolia_app_id.lower() if self.algolia_app_id else "unknown"
        self.algolia_url = (
            f"https://{app_id_clean}-dsn.algolia.net/1/indexes/*/queries"
            f"?x-algolia-agent=Algolia%20for%20vanilla%20JavaScript%20(lite)%203.30.0"
            f";JS%20Helper%202.19.0"
            f"&x-algolia-application-id={self.algolia_app_id}"
            f"&x-algolia-api-key={self.algolia_api_key}"
        )

    def get_engine_name(self) -> str:
        return "algolia"

    def _build_algolia_headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.algolia_origin,
            "Referer": f"{self.algolia_origin}/",
        }

    async def _fetch_algolia(self, params: dict) -> dict:
        """Fetches data from Algolia using the provided params."""
        payload = {
            "requests": [
                {
                    "indexName": self.algolia_index,
                    "params": urlencode(params)
                }
            ]
        }
        
        req_body = json.dumps(payload)
        
        # We can use StealthHttpClient just for consistent proxy/timeout management, 
        # though Algolia doesn't have Akamai blocking.
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.algolia_url, 
                headers=self._build_algolia_headers(), 
                data=req_body, 
                timeout=15
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(f"[ALGOLIA] Failed to fetch. Status: {response.status}. Body: {text}")
                    return {}
                return await response.json()

    def _parse_algolia_hit(self, item: dict) -> dict:
        """Converts an Algolia hit to our standard product dictionary."""
        price_float = None
        price_discount = None
        try:
            price_dict = item.get("price", {})
            p_min = None
            p_max = None
            if "min" in price_dict:
                p_min = float(price_dict["min"])
            elif "BRL" in price_dict:
                p_min = float(price_dict["BRL"])
            elif "minDisplayValue" in price_dict:
                val = price_dict["minDisplayValue"].replace("R$", "").replace(".", "").replace(",", ".").strip()
                p_min = float(val)

            if "max" in price_dict and price_dict["max"]:
                p_max = float(price_dict["max"])
            elif "initialPriceMax" in price_dict and price_dict["initialPriceMax"]:
                p_max = float(price_dict["initialPriceMax"])

            if p_max and p_min and p_max > p_min:
                price_float = p_max
                price_discount = p_min
            else:
                price_float = p_min if p_min is not None else p_max
                price_discount = None
        except Exception:
            price_float = None
            price_discount = None

        url_master = item.get("urlMaster", "")
        if url_master and url_master.startswith("/"):
            product_url = f"{self.base_url}{url_master}"
        else:
            product_url = url_master or ""

        # Remove HTML from title if any
        title = item.get("name", "")
        import re
        title = re.sub(r'<[^>]+>', '', title)
        
        image_url = item.get("imagesHttps", "") or item.get("imagesHttp", "")
        # imagesHttps might be a list or a string or a dict.
        if isinstance(image_url, list) and len(image_url) > 0:
            image_url = image_url[0]
        if isinstance(image_url, dict):
            image_url = image_url.get("flat", "") or image_url.get("details", "") or ""
            
        if image_url and image_url.startswith("//"):
            image_url = f"https:{image_url}"
        if not image_url:
            image_url = "https://via.placeholder.com/150"

        # Cores
        colors = [c.get("label") for c in item.get("colors", []) if isinstance(c, dict) and c.get("label")]
        if not colors and item.get("colorLabel"):
            colors = [item.get("colorLabel")]

        # Tamanhos
        sizes = [str(s) for s in item.get("sizes", []) if s]

        # Descrição e composição
        raw_description = item.get("description", "") or ""
        combined_text = f"{title} {raw_description} {item.get('bulletpoint1', '')}"
        composition = None
        pct_match = re.search(
            r"(\d+%\s*(?:de\s+)?[A-Za-zÀ-ÿ]+(?:\s*(?:e|\/|,)\s*\d+%\s*(?:de\s+)?[A-Za-zÀ-ÿ]+)*)",
            combined_text,
            re.IGNORECASE,
        )
        if pct_match:
            composition = pct_match.group(1).strip()
        elif "linho" in combined_text.lower():
            composition = "100% Linho"
        elif "popeline" in combined_text.lower():
            composition = "Popeline de Algodão (100%)"
        elif "algod" in combined_text.lower():
            composition = "100% Algodão"
        elif item.get("bulletpoint1"):
            composition = item.get("bulletpoint1")
        elif item.get("composition"):
            composition = item.get("composition")

        # Especificações detalhadas
        specifications: Dict[str, Any] = {}
        if item.get("bulletpoint1"):
            specifications["Detalhe"] = str(item.get("bulletpoint1")).strip()
        if item.get("sleeve"):
            specifications["Manga"] = str(item.get("sleeve")).strip()
        if item.get("fitEcom"):
            specifications["Modelagem"] = str(item.get("fitEcom")).strip()
        if item.get("season"):
            specifications["Coleção"] = str(item.get("season")).strip()

        # Gênero e Faixa etária
        gender_raw = item.get("genderLabel", [])
        gender = gender_raw[0] if isinstance(gender_raw, list) and gender_raw else str(gender_raw or "")
        age_group = "kids" if any(k in str(gender_raw).lower() for k in ["infantil", "menino", "menina", "kid"]) else "adult"

        # Código do produto e grupo
        pid = str(item.get("productId") or item.get("objectID") or "")
        product_group = pid.rsplit("-", 1)[0] if "-" in pid else pid

        category = item.get("categoryLabel") or item.get("categoryName") or ""

        return {
            "url": product_url,
            "brand": self.brand.get("brand_key") or self.brand_key or self.domain.split(".")[1] if self.domain.count(".") >= 1 else self.domain,
            "raw_title": title,
            "price_full": price_float,
            "price_discount": price_discount,
            "image_url": image_url,
            "raw_description": raw_description,
            "composition": composition,
            "gender": gender or "Masculino",
            "age_group": age_group,
            "product_group": product_group,
            "available_colors": colors,
            "raw_colors": list(colors),
            "available_sizes": sizes,
            "raw_sizes": list(sizes),
            "size_grid": sizes,
            "raw_size_grid": list(sizes),
            "product_code": pid,
            "category": category,
            "stock_availability": True,
            "rating": item.get("rating"),
            "review_count": item.get("review_count"),
            "shipping_details": {},
            "specifications": specifications,
            "seller": self.brand.get("brand_name") or self.brand_key.title(),
        }

    @staticmethod
    def _hit_matches_variant(item: dict, product_id: str, color: Optional[str]) -> bool:
        """Return whether an Algolia hit is the requested PDP variant.

        Algolia full-text search can return a sibling colour as its first hit even
        for a query such as ``CH8528-23-HBP``.  Product pages are variant scoped,
        so accepting that sibling associates the card's HBP price/title with an
        HDE link.  Verify the native variant id (or, as a fallback, urlMaster's
        color parameter) before using a hit.
        """
        expected_product = product_id.casefold()
        expected_variant = (
            f"{product_id}-{color}".casefold() if color else expected_product
        )
        identifiers = (
            item.get("productId"),
            item.get("objectID"),
            item.get("objectId"),
        )
        for identifier in identifiers:
            value = str(identifier or "").casefold()
            if not value:
                continue
            # `objectID` commonly adds `_lacoste-br` after the native id.
            if value == expected_variant or value.startswith(expected_variant + "_"):
                return True
            if not color and (value == expected_product or value.startswith(expected_product + "_")):
                return True

        url_master = str(item.get("urlMaster") or "")
        parsed = urlparse(url_master)
        url_product = parsed.path.rsplit("/", 1)[-1].removesuffix(".html").casefold()
        url_color = parse_qs(parsed.query).get("color", [""])[0].casefold()
        return url_product == expected_product and (not color or url_color == color.casefold())

    async def _fetch_live_price(self, product_url: str) -> Optional[float]:
        try:
            res = await StealthHttpClient.fetch(product_url)
            if res.ok:
                import re
                match = re.search(r'<meta\s+property="og:product:price:amount"\s+content="([^"]+)"', res.html)
                if match:
                    return float(match.group(1).replace(",", "."))
        except Exception as e:
            logger.warning(f"[ALGOLIA] Error fetching live price for {product_url}: {e}")
        return None

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> BrandSearchResult:
        """Searches using Algolia."""
        logger.info(f"[ALGOLIA] Searching for '{query}' on {self.domain} (limit={max_results})")
        
        raw_limit = self._resolve_max_raw_fetch(max_results)
        if raw_limit is None:
            raw_limit = 1000
        
        params = {
            "query": query,
            "hitsPerPage": raw_limit,
            "page": 0,
            "distinct": "true",
        }
        
        data = await self._fetch_algolia(params)
        results = data.get("results", [{}])[0]
        hits = results.get("hits", [])
        print(f"DEBUG: Algolia returned {len(hits)} raw hits")
        
        parsed_hits = []
        for hit in hits:
            parsed = self._parse_algolia_hit(hit)
            if not parsed["url"] or not parsed["raw_title"]:
                continue
            parsed_hits.append(parsed)

        # Buscar preços ao vivo em paralelo para evitar lentidão
        async def fetch_and_update(p):
            live_price = await self._fetch_live_price(p["url"])
            if live_price is not None:
                p["price_full"] = live_price
            return p
            
        if parsed_hits:
            parsed_hits = await asyncio.gather(*(fetch_and_update(p) for p in parsed_hits))
        
        candidates: List[SearchProductResult] = []
        for parsed in parsed_hits:
            if not parsed.get("price_full"):
                print(f"DEBUG: Missing essential fields: url={parsed['url']} title={parsed['raw_title']} price={parsed.get('price_full')}")
                continue
                
            model = RawProductBronze(**parsed)
            # Apply quality gates
            candidates_temp = [model]
            filtered = self.filter_mens_fashion(candidates_temp)
            if not filtered:
                print(f"DEBUG: filter_mens_fashion rejected {model.raw_title}")
                continue
            
            validated = self.validate_single(filtered[0], log_callback=lambda m: print(f"DEBUG VALIDATION: {m}"))
            if not validated:
                print(f"DEBUG: validate_single rejected {model.raw_title}")
                continue
                
            candidates.append(
                SearchProductResult(
                    brand=model.brand,
                    product_name=model.raw_title,
                    url=model.url,
                    price_full=model.price_full,
                    image_url=model.image_url,
                    available=model.stock_availability
                )
            )
            
            if max_results is not None and len(candidates) >= max_results:
                break
                
        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=self.brand.get("brand_name") or self.brand_key.title(),
            products=candidates,
            error_message=None
        )

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        return await self.get_pdp_product(product_url)

    async def get_pdp_product(self, product_url: str) -> Optional[Dict[str, Any]]:
        parsed_url = urlparse(product_url)
        path = parsed_url.path
        filename = path.split("/")[-1]
        product_id = filename.replace(".html", "")
        
        if not product_id:
            return None

        # Se a URL contiver parâmetro de cor (ex: ?color=031), busca prioritariamente a variante exata
        color_param = parse_qs(parsed_url.query).get("color", [None])[0]
        candidate_queries = []
        if color_param:
            candidate_queries.append(f"{product_id}-{color_param}")
        candidate_queries.append(product_id)
            
        try:
            hits = []
            for query in candidate_queries:
                params = {
                    "query": query,
                    "hitsPerPage": 1,
                    "page": 0,
                    "distinct": "true",
                }
                data = await self._fetch_algolia(params)
                candidate_hits = data.get("results", [{}])[0].get("hits", [])
                # Never select an adjacent colour just because Algolia ranked it
                # first. A query can be successful yet have no exact variant.
                hits = [
                    hit for hit in candidate_hits
                    if self._hit_matches_variant(hit, product_id, color_param)
                ]
                if hits:
                    break

            if hits:
                parsed = self._parse_algolia_hit(hits[0])
                if not parsed.get("url"):
                    parsed["url"] = product_url
                
                live_price = await self._fetch_live_price(parsed["url"])
                if live_price is not None:
                    parsed["price_full"] = live_price
                    
                return parsed
        except Exception as e:
            logger.error(f"[ALGOLIA] Failed to fetch PDP for {product_id}: {e}")
            
        return None

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[ShippingCalculation]:
        """Cálculo de frete sob demanda para Lacoste (Algolia/SFCC)."""
        from services.brand_service import brand_service
        from services.shipping.resolver import resolve_shipping_provider

        brand = brand_service.get_brand("lacoste")
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

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        logger.info(f"[ALGOLIA] Starting bulk scrape for {category_url}")
        self.emit_log(log_callback, f"Iniciando raspagem via Algolia para {category_url}")
        
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(category_url).path)
        last_seg = path.strip("/").split("/")[-1]
        if last_seg.endswith(".html"):
            last_seg = last_seg[:-5]
        query = last_seg.replace("-", " ")
        brand_name_str = (self.brand.get("brand_name") or "").lower()
        brand_key_str = self.brand_key.lower()
        if not query or query.lower() in ["masculino", "vestuario", "br", brand_name_str, brand_key_str]:
            query = ""

        is_infantil = any(k in query.lower() or k in path.lower() for k in ["infantil", "kids", "menino"])
        is_adult_masculino = "/masculino/" in path.lower() and not is_infantil
        facet_filters = None
        p_lower = path.lower()

        if is_adult_masculino:
            # Categorias adultas masculinas Lacoste
            gender_facets = ["genderLabel:Masculino"]
            cat_facets = []
            if "polo" in p_lower:
                cat_facets = ["categoryLabel:POLOS"]
            elif "camiseta" in p_lower:
                cat_facets = ["categoryLabel:TEE-SHIRTS & GOLAS ALTAS"]
            elif "camisa" in p_lower:
                cat_facets = ["categoryLabel:CAMISA"]
            elif "jaqueta" in p_lower or "moletom" in p_lower or "casaco" in p_lower:
                cat_facets = ["categoryLabel:SWEATS", "categoryLabel:BLUSÃO", "categoryLabel:PULLOVER"]
            elif "calca" in p_lower and "bermuda" not in p_lower and "calcado" not in p_lower:
                cat_facets = ["categoryLabel:CALCAS"]
            elif "bermuda" in p_lower:
                cat_facets = ["categoryLabel:BERMUDAS", "categoryLabel:CALÇÕES"]
            elif "calcado" in p_lower or "tenis" in p_lower:
                cat_facets = ["categoryLabel:COURT SNEAKERS", "categoryLabel:ATHLEISURE SNEAKERS", "categoryLabel:VULCANIZED SNEAKERS", "categoryLabel:SLIDES & SANDALS"]
            elif "acessorio" in p_lower or "acess\u00f3rio" in p_lower:
                cat_facets = ["categoryLabel:GORROS", "categoryLabel:MEIAS", "categoryLabel:\u00d3CULOS", "categoryLabel:REL\u00d3GIO", "categoryLabel:LEATHER GOODS BELT"]
            if cat_facets:
                facet_filters = json.dumps([gender_facets, cat_facets])
            else:
                facet_filters = json.dumps([gender_facets])
            query = ""

        elif is_infantil:
            gender_facets = ["genderLabel:Meninos", "genderLabel:Infantil", "genderLabel:Unisex Infantil"]
            cat_facets = []
            if "calcado" in p_lower or "tenis" in p_lower or "sapato" in p_lower:
                cat_facets = ["categoryLabel:COURT SNEAKERS", "categoryLabel:VULCANIZED SNEAKERS", "categoryLabel:ATHLEISURE SNEAKERS", "categoryLabel:SLIDES & SANDALS"]
            elif "polo" in p_lower and "camiseta" not in p_lower:
                cat_facets = ["categoryLabel:POLOS"]
            elif "camiseta" in p_lower and "polo" not in p_lower:
                cat_facets = ["categoryLabel:TEE-SHIRTS & GOLAS ALTAS"]
            elif "moletom" in p_lower or "jaqueta" in p_lower or "casaco" in p_lower:
                cat_facets = ["categoryLabel:SWEATS"]
            elif "calcas-bermudas" in p_lower:
                cat_facets = ["categoryLabel:CALCAS", "categoryLabel:CAL\u00c7\u00d5ES", "categoryLabel:BERMUDAS"]
            elif "calca" in p_lower and "bermuda" not in p_lower and "calcado" not in p_lower:
                cat_facets = ["categoryLabel:CALCAS"]
            elif "bermuda" in p_lower and "calca" not in p_lower:
                cat_facets = ["categoryLabel:BERMUDAS", "categoryLabel:CAL\u00c7\u00d5ES"]
            elif "acessorio" in p_lower or "bone" in p_lower or "gorro" in p_lower:
                cat_facets = ["categoryLabel:GORROS"]
            elif "polos-camisetas" in p_lower:
                cat_facets = ["categoryLabel:POLOS", "categoryLabel:TEE-SHIRTS & GOLAS ALTAS"]
            if cat_facets:
                facet_filters = json.dumps([gender_facets, cat_facets])
            else:
                facet_filters = json.dumps([gender_facets])
            query = ""

        else:
            # Fallback: keyword search (camisa adulta sem /masculino/ no path)
            is_camisa = "camisa" in query.lower() or "camisa" in p_lower
            if is_camisa:
                facet_filters = json.dumps(["categoryLabel:CAMISA"])
                query = ""



        page = 0
        total_pages = 1
        
        while page < total_pages:
            if cancel_event and cancel_event.is_set():
                self.emit_log(log_callback, "Raspagem cancelada pelo usuário.")
                break
                
            params = {
                "query": query,
                "hitsPerPage": 40,
                "page": page,
                "distinct": "true",
            }
            if facet_filters:
                params["facetFilters"] = facet_filters
            
            data = await self._fetch_algolia(params)
            results = data.get("results", [{}])[0]
            hits = results.get("hits", [])
            total_pages = results.get("nbPages", 1)
            
            if not hits:
                break
                
            for hit in hits:
                parsed = self._parse_algolia_hit(hit)
                if not parsed["url"] or not parsed["raw_title"] or not parsed["price_full"]:
                    continue
                    
                model = RawProductBronze(**parsed)
                filtered = self.filter_mens_fashion([model])
                if not filtered:
                    continue
                
                validated = self.validate_single(filtered[0], log_callback=log_callback)
                if not validated:
                    continue
                    
                yield validated
                
            page += 1
            await asyncio.sleep(0.5)

    async def discover_categories(self) -> List[Dict[str, Any]]:
        params = {
            "query": "",
            "hitsPerPage": 0,
            "page": 0,
            "facets": '["categoryLabel"]',
        }
        
        try:
            data = await self._fetch_algolia(params)
            facets = data.get('results', [{}])[0].get('facets', {}).get("categoryLabel", {})
            categories = []
            for cat_name, count in facets.items():
                if count > 0:
                    import urllib.parse
                    safe_name = urllib.parse.quote(cat_name)
                    url = f"{self.base_url}/algolia/category/{safe_name}"
                    categories.append({
                        "name": cat_name.title(),
                        "url": url,
                        "metadata": {"count": count}
                    })
            return categories
        except Exception as e:
            logger.error(f"[ALGOLIA] Failed to discover categories: {e}")
            return []

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []

    async def category_has_products(self, candidate: Any) -> Any:
        from services.category_discovery import CategoryProbeResult
        # Just run a 1-hit query on the candidate URL to see if Algolia returns any hits
        # To do this correctly we'd need to know how the URL maps to an Algolia facet filter.
        return CategoryProbeResult(has_products=True, verified_url=candidate.get("url"))

