import asyncio
import aiohttp
import logging
import json
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict, Any, Callable
from urllib.parse import urljoin, urlparse
import yarl
from bs4 import BeautifulSoup

from core.models import RawProductBronze, BrandSearchResult, SearchProductResult, ColorSwatch
from services.brand_service import brand_service
from services.product_contract import normalize_specifications_aliases
from services.promotions.adapters.shopify import parse_product as parse_shopify_promotions

from core.base_scraper import BaseScraper
from core.session_manager import SessionManager
from core.browser_manager import browser_manager
from services.sanitization import sanitize_colors, sanitize_sizes
import json
import re

logger = logging.getLogger("ShopifyApiClient")

# D-07 gap-closure (01-08): bounded retry/backoff for 429/5xx + a politeness
# throttle on the collection content-probe, so scanning a brand with many
# collections (e.g. ricardoalmeida's 90) does not itself trip Shopify's rate
# limiter and turn real collections into false-BLOCKED. Mirrors the naming/
# shape of vtex_api_scraper.py's _PROBE_MAX_RETRIES/_PROBE_BACKOFF_BASE_SECONDS
# (01-07) for a consistent retry idiom across engines.
_SHOPIFY_PROBE_MAX_RETRIES = 2
_SHOPIFY_PROBE_BACKOFF_BASE_SECONDS = 1.5
_SHOPIFY_PROBE_THROTTLE_SECONDS = 0.34  # ~3 req/s

# Debug ricardo-almeida-cores-vtex (2026-09-28): _get_color_family() dependia
# 100% de uma busca textual HTML fuzzy/por relevância (/search?q=...) para
# descobrir produtos-irmãos (outras cores do mesmo modelo, cada uma um
# PRODUTO Shopify separado — não uma variante). Reprodução ao vivo confirmou
# que essa busca perde produtos-irmãos reais (9 de 15 encontrados), mesmo
# estendendo a paginação, porque o ranking de relevância não garante posição
# previsível. Fonte determinística adicionada: sitemap_products_*.xml,
# filtrado pelo prefixo de handle compartilhado pela família de cores. A
# busca textual é mantida como fallback (não removida) para marcas/produtos
# onde o prefixo não possa ser derivado ou o sitemap esteja inacessível.
_SHOPIFY_SIBLING_FETCH_CONCURRENCY = 5
_SHOPIFY_SITEMAP_MAX_SIBLINGS = 60

# Debug ricardo-almeida-cores-vtex (2026-09-30, rodada de generalização):
# a descoberta via sitemap + prefixo de handle compartilhado NÃO generaliza
# "de graça" para toda marca Shopify. Validada contra ground-truth (código
# "Referencia: NN.NN.NNNN" em body_html) para a marca Buckman ("bck"),
# 10/85 (11.8%) das derivações produziram FALSOS POSITIVOS: o prefixo
# casou produtos de referências/coleções REALMENTE distintas que só
# coincidem no descritor genérico de tecido/estilo (ex.: "linho puro"),
# porque o código que realmente distingue as famílias vem DEPOIS da cor no
# handle da Buckman — exatamente o trecho que o algoritmo descarta. Como
# `_get_color_family_via_sitemap` só cai no fallback textual quando o
# resultado é VAZIO, essa contaminação não é pega por nenhum guard
# existente — seria devolvida como se fosse a família real (pior que o
# comportamento atual). Por isso: ALLOWLIST explícita e fail-safe (default
# = fallback textual legado `_get_color_family_via_search`, comportamento
# inalterado) em vez de uma heurística implícita. Toda marca fora desta
# lista usa exclusivamente o caminho antigo, mesmo que o prefixo pareça
# derivável.
#
# NUNCA adicionar "bck"/Buckman aqui sem antes implementar um parser
# dedicado do código "Referencia: NN.NN.NNNN" de `body_html` (ou
# equivalente) que resolva a ambiguidade com segurança — ver Evidence
# completa (85 derivações testadas, metodologia e exemplos de contaminação)
# em .planning/debug/resolved/ricardo-almeida-cores-vtex.md.
_SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST = frozenset({"ricardoalmeida"})

class ShopifyApiClient(BaseScraper):
    """
    Cliente robusto para extração de dados de lojas Shopify via APIs JSON públicas.
    Implementa suporte a coleções, produtos e bypass de Cloudflare via Playwright fallback.
    """

    def __init__(self, brand_key: str, session: Optional[aiohttp.ClientSession] = None):
        self.brand_key = brand_key
        self.brand_info = brand_service.get_brand(brand_key)
        self.domain = self.brand_info.domain if self.brand_info else ""
        self.base_url = f"https://{self.domain}"
        self.session = session
        self.semaphore = asyncio.Semaphore(10)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            # Explicitamente sem 'br' (Brotli) pois aiohttp nao suporta por padrao
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }

    async def fetch_collections(self) -> List[Dict[str, Any]]:
        """Busca todas as coleções públicas da loja via collections.json."""
        collections = []
        page = 1
        
        session = await SessionManager.get_session()
        while True:
            url = f"{self.base_url}/collections.json?page={page}"
            try:
                resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=15), headers=self.headers)
                if True:
                    if resp.status_code == 200:
                        data = resp.json()
                        batch = data.get("collections", [])
                        if not batch:
                            break
                        collections.extend(batch)
                        page += 1
                    else:
                        logger.error(f"[{self.brand_key}] Erro ao buscar coleções: {resp.status_code}")
                        break
            except Exception as e:
                logger.error(f"[{self.brand_key}] Falha na requisição de coleções: {e}")
                break
        
        return collections

    async def fetch_collection_count(self, collection_handle: str) -> int:
        """Busca o total de produtos em uma colecao via endpoint JSON da colecao."""
        # O Shopify retorna metadados da colecao em /{handle}.json
        url = f"{self.base_url}/collections/{collection_handle}.json"
            
        session = await SessionManager.get_session()
        try:
            resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=10), headers=self.headers)
            if True:
                if resp.status_code == 200:
                    data = resp.json()
                    # O campo products_count fica dentro do objeto 'collection'
                    return data.get("collection", {}).get("products_count", 0)
        except Exception as e:
            logger.warning(f"[{self.brand_key}] Nao foi possivel obter contagem para '{collection_handle}': {e}")
        return 0

    async def category_result_count(self, collection_handle: str) -> Optional[int]:
        """Prova conteúdo real de uma coleção oficial (D-04) via
        `/collections/{handle}/products.json?limit=1` — pede apenas 1 item
        (equivalente ao "limit 1" do plano). Retorna `None` (diagnóstico de
        bloqueio) em qualquer resposta não confiável (não-200 após os retries
        abaixo, ou exceção) — NUNCA confunde bloqueio com categoria vazia. `0`
        só é retornado quando o endpoint respondeu 200 com uma lista de
        produtos vazia.

        D-07 (01-08): HTTP 429/5xx recebem retry com backoff exponencial
        bounded (`_SHOPIFY_PROBE_MAX_RETRIES`/`_SHOPIFY_PROBE_BACKOFF_BASE_SECONDS`)
        — um 429 transitório (rate limit ao varrer muitas coleções em
        sequência) se recupera em vez de virar bloqueio falso; um 404 (coleção
        genuinamente inexistente) NUNCA é re-tentado como se fosse transiente.
        Uma pequena pausa de cortesia (`_SHOPIFY_PROBE_THROTTLE_SECONDS`,
        ~3 req/s) antes de cada probe evita que 90 coleções sequenciais
        estourem o rate limiter da Shopify.
        """
        session = await SessionManager.get_session()
        url = f"{self.base_url}/collections/{collection_handle}/products.json?limit=1"

        await asyncio.sleep(_SHOPIFY_PROBE_THROTTLE_SECONDS)

        attempt = 0
        while True:
            try:
                resp = await session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10), headers=self.headers
                )
                if True:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        if attempt < _SHOPIFY_PROBE_MAX_RETRIES:
                            logger.warning(
                                "[%s] Probe da colecao '%s' retornou HTTP %s "
                                "(tentativa %d/%d) — retry com backoff",
                                self.brand_key, collection_handle, resp.status_code,
                                attempt + 1, _SHOPIFY_PROBE_MAX_RETRIES + 1,
                            )
                            await asyncio.sleep(
                                _SHOPIFY_PROBE_BACKOFF_BASE_SECONDS * (2 ** attempt)
                            )
                            attempt += 1
                            continue
                        logger.warning(
                            "[%s] Probe da colecao '%s' bloqueada apos %d tentativas (HTTP %s)",
                            self.brand_key, collection_handle, attempt + 1, resp.status_code,
                        )
                        return None
                    if resp.status_code != 200:
                        return None
                    data = resp.json()
                    products = data.get("products")
                    if not isinstance(products, list):
                        return None
                    return len(products)
            except Exception as e:
                logger.debug(
                    f"[{self.brand_key}] Falha ao provar colecao '{collection_handle}': {e}"
                )
                return None

    async def fetch_products_from_collection(
        self, 
        collection_handle: str, 
        limit_pages: int = 10,
        log_callback: Optional[Callable] = None
    ):
        """Extrai produtos de uma coleção específica via JSON (Streaming). Mercadão da Roupa e similares."""
        page = 1
        
        session = await SessionManager.get_session()
        while page <= limit_pages:
            # Shopify permite até 250 produtos por página no endpoint de collections
            url = f"{self.base_url}/collections/{collection_handle}/products.json?page={page}&limit=250"
            
            if log_callback:
                log_callback(f"Buscando pagina {page} da colecao '{collection_handle}'...")
                log_callback(f"🔗 URL acessada: {url}")

            try:
                resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=20), headers=self.headers)
                if True:
                    if resp.status_code == 200:
                        data = resp.json()
                        products_json = data.get("products", [])
                        if not products_json:
                            break
                        
                        for p in products_json:
                            bronze = self._map_to_bronze(p, collection_handle)
                            if bronze:
                                if log_callback:
                                    log_callback({"type": "brand_success", "message": f"Sucesso: {bronze.raw_title}"})
                                yield bronze
                        
                        page += 1
                    elif resp.status_code == 404:
                        # Tenta fallback para o endpoint global se a coleção não existir
                        if page == 1:
                            logger.warning(f"[{self.brand_key}] Coleção '{collection_handle}' não encontrada. Tentando global.")
                            async for prod in self.fetch_all_products(limit_pages, log_callback):
                                yield prod
                            return
                        break
                    elif resp.status_code == 403:
                        logger.warning(f"[{self.brand_key}] Bloqueio Shopify (403). Acionando Playwright Fallback...")
                        try:
                            html_content = await browser_manager.fetch_html(url)
                            # Extrai JSON de produtos do HTML
                            clean_json = html_content
                            if "<pre" in html_content:
                                match = re.search(r"<pre[^>]*>(.*?)</pre>", html_content, re.DOTALL)
                                if match: clean_json = match.group(1)
                            
                            data = json.loads(clean_json)
                            products_json = data.get("products", [])
                            if not products_json: break
                            for p in products_json:
                                bronze = self._map_to_bronze(p, collection_handle)
                                if bronze:
                                    if log_callback: log_callback({"type": "brand_success", "message": f"Sucesso: {bronze.raw_title}"})
                                    yield bronze
                            page += 1
                            continue 
                        except Exception as e:
                            logger.error(f"Fallback Playwright falhou: {e}")
                            break
                    else:
                        if log_callback:
                            log_callback({"type": "brand_error", "message": f"Erro {resp.status_code} em {url}"})
                        break
            except Exception as e:
                if log_callback:
                    log_callback({"type": "brand_error", "message": f"Erro ao paginar Shopify JSON: {e}"})
                break
                    
        return

    async def fetch_all_products(self, limit_pages: int = 5, log_callback: Optional[Callable] = None):
        """Fallback para buscar produtos globais da loja (Streaming)."""
        page = 1
        session = await SessionManager.get_session()
        while page <= limit_pages:
            url = f"{self.base_url}/products.json?page={page}&limit=250"
            try:
                resp = await session.get(url, timeout=aiohttp.ClientTimeout(total=20), headers=self.headers)
                if True:
                    if resp.status_code == 200:
                        data = resp.json()
                        products_json = data.get("products", [])
                        if not products_json:
                            break
                        for p in products_json:
                            bronze = self._map_to_bronze(p, "Geral")
                            if bronze:
                                yield bronze
                        page += 1
                    else:
                        break
            except Exception as e:
                logger.error(f"[{self.brand_key}] Erro ao buscar todos os produtos Shopify: {e}")
                break

    def _map_to_bronze(self, p: Dict[str, Any], category: Optional[str]) -> Optional[RawProductBronze]:
        """Converte JSON nativo da Shopify para o modelo RawProductBronze."""
        try:
            handle = p.get("handle", "")
            if not handle:
                return None
            
            variants = p.get("variants", [])
            first_variant = variants[0] if variants else {}
            
            # Preços
            price_full = float(first_variant.get("price", 0))
            price_old = first_variant.get("compare_at_price")
            price_old = float(price_old) if price_old else None
            
            # Imagem
            images = p.get("images", [])
            image_url = images[0].get("src") if images else None
            
            # Atributos
            color_aliases = {"cor", "color", "colours", "colores"}
            if hasattr(self.brand_info, "color_aliases") and self.brand_info.color_aliases:
                color_aliases.update(str(a).lower() for a in self.brand_info.color_aliases)
            
            size_aliases = {"tamanho", "size", "sizes"}
            if hasattr(self.brand_info, "size_aliases") and self.brand_info.size_aliases:
                size_aliases.update(str(a).lower() for a in self.brand_info.size_aliases)

            # Se a marca requer regex de extração, aceitamos a chave padrão 'title' se não achar os outros
            if hasattr(self.brand_info, "extract_via_regex") and self.brand_info.extract_via_regex:
                size_aliases.add("title")
                color_aliases.add("title")

            available_colors = self._extract_available_option_values(p, variants, color_aliases)
            available_sizes = self._extract_available_option_values(p, variants, size_aliases)
            # Grade completa (todas as variantes de tamanho da Shopify, com ou sem
            # estoque) — distinta de available_sizes acima, que reflete só o que
            # tem estoque agora (D-01: mesma fonte, sem inferência adicional).
            size_grid = self._extract_available_option_values(
                p, variants, size_aliases, only_available=False
            )

            raw_colors = list(available_colors)
            raw_sizes = list(available_sizes)
            raw_size_grid = list(size_grid)

            if hasattr(self.brand_info, "extract_via_regex") and self.brand_info.extract_via_regex:
                new_sizes = []
                for s in available_sizes:
                    match = re.search(r'\b(PP|P|M|G|GG|XG|XXG|XGG|U|UN|\d{1,3})\b$', s.strip(), re.IGNORECASE)
                    if match:
                        new_sizes.append(match.group(1).upper())
                    else:
                        new_sizes.append(s)
                available_sizes = new_sizes
                
                new_colors = []
                for c in available_colors:
                    if "/" in c:
                        new_colors.append(c.split("/")[0].strip())
                    elif " - " in c:
                        new_colors.append(c.split(" - ")[0].strip())
                    else:
                        new_colors.append(c)
                available_colors = new_colors

            # D-01: removido o fallback que tratava o título INTEIRO da variante
            # (ex.: "Azul / M") como tamanho — isso inventava um atributo que nunca
            # foi uma opção "Tamanho"/"Size" explícita da loja. Ausência de opção
            # de tamanho explícita produz [] (fail-closed), nunca um valor inferido.
            stock_availability = self._variants_available(variants, default=False)
            specifications = normalize_specifications_aliases(
                {
                    "vendor": p.get("vendor", ""),
                    "product_type": p.get("product_type", ""),
                    "tags": ", ".join(p.get("tags", []))
                    if isinstance(p.get("tags"), list)
                    else p.get("tags", ""),
                }
            )
            # Contexto de oferta condicionada capturado no payload nativo da Shopify
            # (tags/variantes), independente de o produto ter ou não uma promoção —
            # mesmo padrão de `vtex_api_scraper.parse_product_dict()` (GAP-01, Fase 03
            # Plano 13): sem isso, `conditional_offers` fica sempre `[]` e o probe
            # `shopify_structured` nunca encontra nada em produção, porque o payload
            # reconstruído em `runtime._product_payload()` é sempre no formato VTEX
            # (items/sellers/commertialOffer), nunca no formato nativo Shopify
            # (variants/tags) que `shopify.parse_product()` espera.
            conditional_offers = parse_shopify_promotions(p)

            return RawProductBronze(
                url=urljoin(self.base_url, f"/products/{handle}"),
                brand=self.brand_key,
                raw_title=p.get("title", "Sem título"),
                raw_description=p.get("body_html", ""),
                price_full=price_old if price_old and price_old > price_full else price_full,
                price_discount=price_full if price_old and price_old > price_full else None,

                stock_availability=stock_availability,
                category=category,
                breadcrumb=[category] if category else [],
                image_url=image_url,
                available_colors=sanitize_colors(available_colors),
                available_sizes=sanitize_sizes(available_sizes),
                # D-01: proveniência bruta — mesmos option values explícitos usados em
                # available_colors/available_sizes, sem normalização adicional aqui.
                raw_colors=raw_colors,
                raw_sizes=raw_sizes,
                size_grid=sanitize_sizes(size_grid),
                raw_size_grid=raw_size_grid,
                specifications=specifications,
                # D-03: product.id nativo em TODOS os endpoints — nunca o SKU da
                # primeira variante (identidade ambígua, promove um SKU de variante
                # a identidade de produto).
                product_code=str(p.get("id")) if p.get("id") is not None else None,
                conditional_offers=conditional_offers,
            )
        except Exception as e:
            logger.error(f"Erro ao mapear produto Shopify: {e}")
            return None

    @staticmethod
    def _variants_available(variants: List[Dict[str, Any]], default: bool) -> bool:
        if not variants:
            return default
        return any(variant.get("available") is True for variant in variants)

    @staticmethod
    def _extract_available_option_values(
        product: Dict[str, Any],
        variants: List[Dict[str, Any]],
        accepted_names: set[str],
        *,
        only_available: bool = True,
    ) -> List[str]:
        """Extrai os valores de uma opção (ex.: tamanho/cor) a partir das variantes.

        Com ``only_available=True`` (default), pula variantes com
        ``available is False`` — reflete o que tem estoque agora. Com
        ``only_available=False``, retorna a grade completa (todas as variantes
        registradas na Shopify, independente de estoque).
        """
        options = product.get("options", [])
        position = None
        for option in options:
            name = str(option.get("name") or "").strip().lower()
            if name in accepted_names:
                try:
                    position = int(option.get("position") or 0)
                except (TypeError, ValueError):
                    position = None
                break

        if not position:
            return []

        values: List[str] = []
        variant_key = f"option{position}"
        for variant in variants:
            if only_available and variant.get("available") is False:
                continue
            value = str(variant.get(variant_key) or "").strip()
            if value and value not in values:
                values.append(value)
        return values

    async def get_product_by_url(self, product_url: str) -> Optional[RawProductBronze]:
        """Extrai dados de um único produto via URL (append .json)."""
        json_url = product_url.split("?")[0] + ".json"
        session = await SessionManager.get_session()
        product_data = None
        try:
            resp = await session.get(json_url, timeout=aiohttp.ClientTimeout(total=15), headers=self.headers)
            if True:
                if resp.status_code == 200:
                    data = resp.json()
                    product_data = data.get("product")
        except Exception as e:
            logger.error(f"Erro ao buscar detalhes do produto Shopify: {e}")
        detail_category = None
        if product_data:
            detail_category = str(product_data.get("product_type") or "").strip() or None
        mapped = self._map_to_bronze(product_data, detail_category) if product_data else None
        if not mapped:
            return None

        if self._should_enrich_product_details(product_data, mapped):
            enrichment = await self._fetch_product_page_enrichment(product_url, mapped.raw_title)
            if enrichment:
                mapped = self._merge_product_enrichment(mapped, enrichment)

        mapped.available_colors = await self._get_color_family(mapped)

        return mapped

    async def _get_color_family(self, mapped: RawProductBronze) -> List[str]:
        """Descobre as outras cores do mesmo modelo Shopify (cada cor é um
        PRODUTO separado, não uma variante dentro do mesmo produto).

        Fonte primária (determinística, SOMENTE para marcas na allowlist
        `_SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST`): sitemap_products_*.xml,
        filtrado pelo prefixo de handle compartilhado pela família de cores —
        ver `_get_color_family_via_sitemap`. Marcas fora da allowlist (ex.:
        Buckman/"bck" — ver comentário da allowlist) NUNCA passam por esse
        caminho, mesmo que um prefixo pareça derivável, porque o algoritmo
        pode contaminar famílias de cor com produtos de referências
        distintas para elas (falso positivo silencioso, ver debug session).

        Fonte de fallback (heurística, mecanismo legado, usada para toda
        marca fora da allowlist E como fallback fail-safe para marcas na
        allowlist quando a fonte determinística não encontra um prefixo
        válido ou falha): busca textual HTML paginada.
        """
        if not mapped.available_colors or len(mapped.available_colors) != 1:
            return mapped.available_colors

        if self.brand_key in _SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST:
            try:
                sitemap_colors = await self._get_color_family_via_sitemap(mapped)
                if sitemap_colors:
                    return sitemap_colors
            except Exception as e:
                logger.warning(
                    f"[{self.brand_key}] Falha ao buscar familia de cores via sitemap: {e}"
                )

        return await self._get_color_family_via_search(mapped)

    async def _get_color_family_via_sitemap(self, mapped: RawProductBronze) -> List[str]:
        """Busca determinística de produtos-irmãos via sitemap_products_*.xml.

        Retorna lista VAZIA (não exceção) quando não consegue derivar um
        prefixo de handle válido a partir da cor conhecida, ou quando o
        sitemap não traz nenhum produto com esse prefixo — sinal para o
        chamador (`_get_color_family`) acionar o fallback de busca textual.
        """
        color = mapped.available_colors[0]
        handle = urlparse(mapped.url).path.rstrip("/").rsplit("/", 1)[-1]
        prefix = self._derive_sibling_handle_prefix(handle, color)
        if not prefix:
            return []

        sitemap_urls = await self._fetch_sitemap_product_urls()
        if not sitemap_urls:
            return []

        sibling_urls: List[str] = []
        for url in sitemap_urls:
            sib_handle = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
            if sib_handle == handle:
                continue
            if sib_handle.startswith(prefix):
                sibling_urls.append(url)

        if not sibling_urls:
            return []

        sibling_urls = sibling_urls[:_SHOPIFY_SITEMAP_MAX_SIBLINGS]
        semaphore = asyncio.Semaphore(_SHOPIFY_SIBLING_FETCH_CONCURRENCY)

        async def _bounded_fetch(sibling_url: str) -> Optional[Dict[str, Optional[str]]]:
            async with semaphore:
                return await self._fetch_sibling_details(sibling_url)

        results = await asyncio.gather(
            *(_bounded_fetch(u) for u in sibling_urls), return_exceptions=True
        )

        # Swatches (D-Opção A, ricardo-almeida-cores-vtex): um por
        # produto-irmão, reaproveitando SOMENTE dados já buscados nesta
        # mesma descoberta de família (nenhuma chamada de rede nova). O
        # próprio produto entra primeiro, usando a imagem já carregada por
        # `_map_to_bronze` (mapped.image_url) e o código extraído do seu
        # próprio handle — sem custo adicional.
        sibling_colors = {color}
        swatches = [
            ColorSwatch(
                name=color,
                image_url=mapped.image_url,
                sibling_code=self._extract_sibling_code(handle, prefix),
            )
        ]
        for sibling_url, result in zip(sibling_urls, results):
            if isinstance(result, Exception) or not result:
                continue
            sib_color = result.get("color")
            if not sib_color:
                continue
            sibling_colors.add(sib_color)
            sib_handle = urlparse(sibling_url).path.rstrip("/").rsplit("/", 1)[-1]
            swatches.append(
                ColorSwatch(
                    name=sib_color,
                    image_url=result.get("image_url"),
                    sibling_code=self._extract_sibling_code(sib_handle, prefix),
                )
            )

        mapped.color_swatches = swatches
        return list(sibling_colors)

    async def _fetch_sitemap_product_urls(self) -> List[str]:
        """Retorna todas as URLs de produto publicadas no(s)
        sitemap_products_*.xml da loja — fonte completa e determinística do
        catálogo (ao contrário da busca textual, que é fuzzy/por relevância).
        """
        session = await SessionManager.get_session()

        async def _fetch_xml(url: str) -> Optional[str]:
            try:
                resp = await session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10), headers=self.headers
                )
                if resp.status_code != 200:
                    return None
                return resp.text
            except Exception as e:
                logger.debug(f"[{self.brand_key}] Falha ao buscar sitemap {url}: {e}")
                return None

        index_xml = await _fetch_xml(f"{self.base_url}/sitemap.xml")
        product_sitemap_urls: List[str] = []
        if index_xml:
            product_sitemap_urls = self._parse_sitemap_locs(index_xml, "sitemap_products")

        if not product_sitemap_urls:
            # Algumas lojas não expõem um índice separado — tenta o padrão
            # default direto como último recurso.
            product_sitemap_urls = [f"{self.base_url}/sitemap_products_1.xml"]

        urls: List[str] = []
        for sitemap_url in product_sitemap_urls:
            xml_content = await _fetch_xml(sitemap_url)
            if not xml_content:
                continue
            urls.extend(self._parse_sitemap_locs(xml_content, "/products/"))

        return urls

    @staticmethod
    def _parse_sitemap_locs(xml_content: str, filter_substr: str) -> List[str]:
        """Extrai todos os valores `<loc>` de um XML de sitemap cujo texto
        contenha `filter_substr`. Ignora namespace (compara só o nome local
        da tag) para funcionar com qualquer variante de sitemap protocol."""
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return []

        locs: List[str] = []
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "loc" and elem.text:
                loc = elem.text.strip()
                if filter_substr in loc:
                    locs.append(loc)
        return locs

    async def _fetch_sibling_details(self, product_url: str) -> Optional[Dict[str, Optional[str]]]:
        """Busca cor (opção 'Cor') e imagem principal de um produto-irmão via
        seu endpoint `.json` público — MESMA chamada de rede já feita para
        descobrir a cor da família (D-Opção A: nenhuma requisição extra
        introduzida só para popular `color_swatches`). Retorna None em
        qualquer falha (rede, parsing, opção de cor ausente) — nunca propaga
        exceção ao chamador."""
        session = await SessionManager.get_session()
        json_url = product_url.split("?")[0] + ".json"
        try:
            resp = await session.get(
                json_url, timeout=aiohttp.ClientTimeout(total=8), headers=self.headers
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception as e:
            logger.debug(
                f"[{self.brand_key}] Falha ao buscar detalhes do produto-irmao {product_url}: {e}"
            )
            return None

        product = data.get("product") if isinstance(data, dict) else None
        if not product:
            return None

        variants = product.get("variants", [])
        color_aliases = {"cor", "color", "colours", "colores"}
        if hasattr(self.brand_info, "color_aliases") and self.brand_info.color_aliases:
            color_aliases.update(str(a).lower() for a in self.brand_info.color_aliases)

        colors = self._extract_available_option_values(product, variants, color_aliases)
        if not colors:
            return None

        images = product.get("images", [])
        image_url = None
        if images and isinstance(images[0], dict):
            image_url = images[0].get("src")

        return {"color": colors[0], "image_url": image_url}

    @staticmethod
    def _extract_sibling_code(handle: str, prefix: str) -> Optional[str]:
        """Extrai o código numérico estável de um produto (irmão ou o
        próprio) a partir do seu handle — o primeiro segmento puramente
        numérico encontrado após o prefixo de família compartilhado (ex.:
        handle='camisa-polo-...-cinza-55578-76775', prefix=
        'camisa-polo-...-' -> '55578'). Esse código já aparece publicamente
        no handle e no nome do arquivo de imagem do produto (ver Evidence
        em .planning/debug/resolved/ricardo-almeida-cores-vtex.md) — não
        requer nenhuma chamada de rede adicional. Retorna None quando
        nenhum segmento numérico existe após o prefixo."""
        remainder = handle[len(prefix):] if handle.startswith(prefix) else handle
        for segment in remainder.split("-"):
            if segment.isdigit():
                return segment
        return None

    @classmethod
    def _derive_sibling_handle_prefix(cls, handle: str, color: str) -> Optional[str]:
        """Deriva o prefixo de handle compartilhado pela família de cores,
        removendo o(s) segmento(s) de cor (slugificados) do handle do
        produto atual — o restante à direita (códigos numéricos) é
        descartado junto.

        Ex.: handle='camisa-polo-cinza-64859-76775', color='Cinza' ->
        prefix='camisa-polo-'.

        Retorna None quando a cor não aparece como segmento(s) hifenizado(s)
        explícito(s) no handle — sinal para o chamador usar o fallback de
        busca textual legado.
        """
        color_slug = cls._slugify(color)
        if not color_slug:
            return None

        color_parts = color_slug.split("-")
        segments = handle.split("-")
        n = len(color_parts)
        if n == 0 or n > len(segments):
            return None

        # Varre da direita para a esquerda: a cor tende a aparecer perto do
        # final do handle (seguida pelos códigos numéricos), então o último
        # match é o mais confiável.
        for i in range(len(segments) - n, -1, -1):
            if segments[i:i + n] == color_parts:
                prefix_segments = segments[:i]
                if not prefix_segments:
                    return None
                return "-".join(prefix_segments) + "-"
        return None

    @staticmethod
    def _slugify(value: str) -> str:
        """Normaliza texto para o formato de segmento de handle Shopify
        (minúsculo, sem acentos, hífens no lugar de espaços/pontuação)."""
        normalized = unicodedata.normalize("NFKD", value)
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
        ascii_value = ascii_value.lower().strip()
        return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")

    async def _get_color_family_via_search(self, mapped: RawProductBronze) -> List[str]:
        """[Fallback legado] Tenta buscar outras cores do mesmo modelo usando
        a busca HTML do Shopify (paginada). Fuzzy/por relevância — pode não
        encontrar todos os produtos-irmãos (ver Resolution.root_cause em
        .planning/debug/resolved/ricardo-almeida-cores-vtex.md). Mantido
        apenas como fallback quando a descoberta determinística via sitemap
        (`_get_color_family_via_sitemap`) não está disponível OU quando a
        marca não está na allowlist de descoberta via sitemap (ver
        `_SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST`, ex.: Buckman/"bck").

        `color_swatches` (D-Opção A) é preenchido aqui só com `name` —
        `image_url`/`sibling_code` ficam `None`, pois esta busca textual não
        traz imagem nem handle do produto-irmão (nenhuma chamada de rede
        nova é introduzida para obtê-los). Reflete EXATAMENTE a mesma lista
        de siblings que este método já retornava antes desta mudança — nem
        mais, nem menos (sem risco novo de contaminação para marcas que
        permanecem neste caminho, ex. Buckman)."""
        if not mapped.available_colors or len(mapped.available_colors) != 1:
            return mapped.available_colors

        color = mapped.available_colors[0]
        # Remove a cor do final do título (ex: "Camisa Polo Cinza" -> "Camisa Polo")
        base_title = re.sub(rf'\b{re.escape(color)}\b', '', mapped.raw_title, flags=re.IGNORECASE).strip()
        base_title = re.sub(r'[-\/|]+$', '', base_title).strip()

        if not base_title or len(base_title) < 5:
            mapped.color_swatches = [ColorSwatch(name=c) for c in mapped.available_colors]
            return mapped.available_colors

        try:
            q = urllib.parse.quote(base_title)
            parsed_url = urlparse(mapped.url)
            
            session = await SessionManager.get_session()
            sibling_colors = set(mapped.available_colors)
            
            for page in range(1, 4):
                url_search = f"https://{parsed_url.netloc}/search?page={page}&q={q}"
                resp = await session.get(url_search, timeout=aiohttp.ClientTimeout(total=5), headers=self.headers)
                if True:
                    if resp.status_code != 200:
                        break
                    html = resp.text
                    soup = BeautifulSoup(html, "html.parser")
                    
                    found_on_page = 0
                    for tag in soup.find_all(string=re.compile(base_title, re.IGNORECASE)):
                        text = tag.strip()
                        if text.lower().startswith(base_title.lower()):
                            color_part = text[len(base_title):].strip()
                            color_part = re.sub(r'^[-\/|]+', '', color_part).strip()
                            if color_part and len(color_part) < 20:
                                sibling_colors.add(color_part)
                                found_on_page += 1
                                
                    if found_on_page == 0:
                        break

            colors = list(sibling_colors)
            mapped.color_swatches = [ColorSwatch(name=c) for c in colors]
            return colors
        except Exception as e:
            logger.warning(f"Erro ao buscar família de cores no Shopify via HTML: {e}")

        mapped.color_swatches = [ColorSwatch(name=c) for c in mapped.available_colors]
        return mapped.available_colors

    @staticmethod
    def _should_enrich_product_details(
        product_data: Dict[str, Any],
        mapped: RawProductBronze,
    ) -> bool:
        variants = product_data.get("variants", []) if isinstance(product_data, dict) else []
        has_explicit_variant_availability = any(
            isinstance(variant.get("available"), bool)
            for variant in variants
            if isinstance(variant, dict)
        )
        return (
            not has_explicit_variant_availability
            or mapped.rating is None
            or mapped.review_count is None
        )

    async def _fetch_product_page_enrichment(
        self,
        product_url: str,
        raw_title: str,
    ) -> Dict[str, Any]:
        session = await SessionManager.get_session()
        try:
            resp = await session.get(
                product_url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers=self.headers,
            )
            if True:
                if resp.status_code != 200:
                    return {}
                html = resp.text
        except Exception as e:
            logger.debug(f"[{self.brand_key}] Falha ao enriquecer PDP Shopify via HTML: {e}")
            return {}
        return self._extract_product_page_enrichment(html, raw_title)

    @classmethod
    def _extract_product_page_enrichment(
        cls,
        html: str,
        raw_title: str,
    ) -> Dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        enrichment: Dict[str, Any] = {}

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            content = script.string or script.get_text(" ", strip=True)
            if not content:
                continue
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            for node in cls._iter_json_ld_nodes(payload):
                cls._merge_json_ld_node_into_enrichment(node, enrichment, raw_title)

        return enrichment

    @staticmethod
    def _iter_json_ld_nodes(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            nodes = []
            graph = payload.get("@graph")
            if isinstance(graph, list):
                nodes.extend(item for item in graph if isinstance(item, dict))
            nodes.append(payload)
            return nodes
        return []

    @classmethod
    def _merge_json_ld_node_into_enrichment(
        cls,
        node: Dict[str, Any],
        enrichment: Dict[str, Any],
        raw_title: str,
    ) -> None:
        node_type = node.get("@type")
        if isinstance(node_type, list):
            node_types = {str(item).lower() for item in node_type}
        else:
            node_types = {str(node_type).lower()}
        if "product" not in node_types and "productgroup" not in node_types:
            return

        aggregate = node.get("aggregateRating")
        if isinstance(aggregate, dict):
            rating = cls._parse_rating_value(aggregate.get("ratingValue"))
            review_count = cls._parse_review_count_value(
                aggregate.get("reviewCount") or aggregate.get("ratingCount")
            )
            if rating is not None:
                enrichment["rating"] = rating
            if review_count is not None:
                enrichment["review_count"] = review_count

        variants = node.get("hasVariant")
        if isinstance(variants, list):
            available_sizes: List[str] = list(enrichment.get("available_sizes") or [])
            any_in_stock = False
            found_availability = False
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                in_stock = cls._offers_in_stock(variant.get("offers"))
                if in_stock is None:
                    continue
                found_availability = True
                if in_stock:
                    any_in_stock = True
                    display_name = cls._variant_display_name(variant, raw_title)
                    if display_name and display_name not in available_sizes:
                        available_sizes.append(display_name)
            if found_availability:
                enrichment["stock_availability"] = any_in_stock
            if available_sizes:
                enrichment["available_sizes"] = available_sizes
            return

        offer_state = cls._offers_in_stock(node.get("offers"))
        if offer_state is not None:
            enrichment["stock_availability"] = offer_state

    @staticmethod
    def _offers_in_stock(offers: Any) -> Optional[bool]:
        if isinstance(offers, list):
            states = [ShopifyApiClient._offers_in_stock(item) for item in offers]
            states = [state for state in states if state is not None]
            if not states:
                return None
            return any(states)
        if not isinstance(offers, dict):
            return None
        availability = str(offers.get("availability") or "").lower()
        if "instock" in availability or "em estoque" in availability:
            return True
        if "outofstock" in availability or "esgotado" in availability or "fora de estoque" in availability:
            return False
        return None

    @staticmethod
    def _variant_display_name(variant: Dict[str, Any], raw_title: str) -> Optional[str]:
        name = str(variant.get("name") or "").strip()
        if not name:
            return None
        prefix = f"{raw_title.strip()} - "
        if raw_title and name.lower().startswith(prefix.lower()):
            return name[len(prefix):].strip() or None
        return name

    @staticmethod
    def _parse_rating_value(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return round(float(str(value).replace(",", ".")), 1)
        except Exception:
            return None

    @staticmethod
    def _parse_review_count_value(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        digits = re.sub(r"[^\d]", "", str(value))
        if not digits:
            return None
        try:
            return int(digits)
        except Exception:
            return None

    @staticmethod
    def _merge_product_enrichment(
        product: RawProductBronze,
        enrichment: Dict[str, Any],
    ) -> RawProductBronze:
        payload = product.model_dump()
        for key in ("stock_availability", "rating", "review_count"):
            if enrichment.get(key) is not None:
                payload[key] = enrichment[key]
        if enrichment.get("available_sizes"):
            payload["available_sizes"] = enrichment["available_sizes"]
        return RawProductBronze.model_validate(payload)

    async def scrape_category_paged(
        self, 
        category_url: str, 
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        chunk_size: int = 50
    ):
        """
        Varre uma coleção completa (Streaming).
        """
        handle = category_url.split("/")[-1] if "/" in category_url else category_url
        if not handle or handle == "products":
            handle = "all"
            
        if log_callback:
            log_callback(f"[START] Iniciando extracao Shopify para a colecao: {handle}")
            
        async for prod in self.fetch_products_from_collection(handle, log_callback=log_callback):
            if cancel_event and cancel_event.is_set():
                break
            yield prod

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None
    ) -> BrandSearchResult:
        """Busca produtos via endpoint de busca da Shopify.

        SR-01: request max_raw = max_results * 2 raw candidates so that
        filter_mens_fashion() has room to discard off-category items without
        the final result set falling below max_results.

        Pipeline:
          1. suggest.json — fastest path (Shopify Predictive Search API).
             Soft-cap: limited to ~10 suggestions by the Shopify API itself,
             so this path alone cannot guarantee max_raw. Harvest all it has.
          2. search.json (paginated) — triggered when suggest.json returns
             fewer than max_raw candidates. Paginates with `page` param until
             max_raw is reached or the store returns an empty page.
          3. filter_mens_fashion → truncate to max_results.
        """
        from services.engines.base_engine import BaseEngine

        max_raw = BaseEngine._resolve_max_raw_fetch(max_results)
        raw_products: list = []
        session = await SessionManager.get_session()

        def _parse_shopify_price(raw) -> float:
            try:
                val = str(raw).replace(",", ".").strip()
                numeric = float(val)
                if "." not in str(raw) and numeric > 1000:
                    return round(numeric / 100, 2)
                return round(numeric, 2)
            except Exception:
                return 0.0

        import urllib.parse
        encoded_query = urllib.parse.quote(query)

        # --- Step 1: suggest.json (Predictive Search API) ---
        suggest_url_str = f"{self.base_url}/search/suggest.json?q={encoded_query}&resources[type]=product"
        suggest_url = yarl.URL(suggest_url_str, encoded=True)
        try:
            resp = await session.get(str(suggest_url), timeout=aiohttp.ClientTimeout(total=15), headers=self.headers)
            if True:
                if resp.status_code == 200:
                    data = resp.json()
                    suggestions = data.get("resources", {}).get("results", {}).get("products", [])
                    for p in suggestions:
                        img = p.get("featured_image") or {}
                        image_url = img.get("url") if isinstance(img, dict) else img
                        if not image_url:
                            image_url = p.get("image")

                        available = self._variants_available(p.get("variants", []), default=True)

                        raw_products.append(SearchProductResult(
                            brand=self.brand_key,
                            product_name=p.get("title") or "Sem titulo",
                            url=urljoin(self.base_url, p.get("url", "/")),
                            price_full=_parse_shopify_price(p.get("price") or 0),
                            image_url=image_url,
                            available=available,
                            product_code=str(p.get("id")) if p.get("id") else None,
                        ))
        except Exception as e:
            logger.debug("[%s] Falha na busca via suggest.json: %s", self.brand_key, e)

        # --- Step 2: search.json (paginated) — fill the gap to max_raw ---
        # Always executed if suggest returned less than max_raw, so we have
        # enough raw candidates for filter_mens_fashion to not starve us.
        if max_raw is None or len(raw_products) < max_raw:
            seen_codes: set = {p.product_code for p in raw_products if p.product_code}
            page = 1
            max_pages = 5 if max_raw is not None else None
            while (max_raw is None or len(raw_products) < max_raw) and (max_pages is None or page <= max_pages):
                search_url_str = (
                    f"{self.base_url}/search.json?type=product"
                    f"&q={encoded_query}&page={page}"
                )
                search_url = yarl.URL(search_url_str, encoded=True)
                try:
                    resp = await session.get(
                        str(search_url),
                        timeout=aiohttp.ClientTimeout(total=15),
                        headers=self.headers,
                    )
                    if True:
                        if resp.status_code != 200:
                            break
                        data = resp.json()
                        page_results = data.get("results", [])
                        if not page_results:
                            break  # store exhausted
                        for p in page_results:
                            code = str(p.get("id")) if p.get("id") is not None else None
                            if code and code in seen_codes:
                                continue  # dedup against suggest.json results
                            variants = p.get("variants", [])
                            first_variant = variants[0] if variants else {}
                            images = p.get("images", [])
                            image_url = images[0].get("src") if images else None
                            available = self._variants_available(variants, default=False)
                            raw_products.append(SearchProductResult(
                                brand=self.brand_key,
                                product_name=p.get("title") or "Sem titulo",
                                url=urljoin(self.base_url, f"/products/{p.get('handle', '')}"),
                                price_full=_parse_shopify_price(first_variant.get("price") or 0),
                                image_url=image_url,
                                available=available,
                                # D-03: product.id nativo em todos os endpoints (mesma
                                # identidade de _map_to_bronze/suggest.json) — nunca o
                                # SKU da primeira variante.
                                product_code=code,
                            ))
                            if code:
                                seen_codes.add(code)
                            if max_raw is not None and len(raw_products) >= max_raw:
                                break
                        page += 1
                except Exception as e:
                    logger.debug("[%s] Falha na busca paginada via search.json (page=%d): %s", self.brand_key, page, e)
                    break

        # --- Step 3: Quality Gates + truncate (SR-01) ---
        filtered_products = BaseEngine.filter_mens_fashion(raw_products)
        if max_results is not None:
            filtered_products = filtered_products[:max_results]

        logger.info(
            "[Shopify] search brand=%s query=%r -> %d raw, %d after filter (max_results=%d)",
            self.brand_key, query, len(raw_products), len(filtered_products), max_results,
        )

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=self.brand_info.brand_name if self.brand_info else self.brand_key,
            products=filtered_products,
            total_found=len(filtered_products)
        )

