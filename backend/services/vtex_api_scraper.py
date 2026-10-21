import asyncio
import aiohttp
import logging
import re
import json
import urllib.parse
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession

from config import settings
from core.models import RawProductBronze, BrandSearchResult, SearchProductResult, ShippingInfo
from pydantic import ValidationError
from core.vtex_schemas import VtexProduct, VtexItem
from services.review_service import get_single_review, get_bulk_reviews
from services.brand_service import brand_service
from services.category_resolver import resolve_query_to_vtex_category_path
from services.product_contract import normalize_specifications_aliases
from services import vtex_parsing
from services.vtex_shipping import filter_and_sort_slas, classify_result, select_candidate
from services.promotions.adapters.vtex import parse_teasers
from core.base_scraper import BaseScraper
from core.identity import IdentityManager
from core.browser_manager import browser_manager
from services.sanitization import sanitize_colors, sanitize_sizes

# Configuração de Logs
logger = logging.getLogger("VtexApiClient")

# ------------------------------------------------------------------
# Probe de conteúdo de categoria (D-07, Plano 01-07) — retry/backoff +
# classificação status-aware (hard_blocked vs api_unavailable vs count real).
# ------------------------------------------------------------------
_PROBE_MAX_RETRIES = 2
_PROBE_BACKOFF_BASE_SECONDS = 1.5

# ------------------------------------------------------------------
# Seletor de tile VTEX-IO theme-robusto (D-08a, Plano 01-10). Substitui o
# antigo `.vtex-product-summary-2-x-container` hardcoded (só cobria o tema
# da Hugo Boss) por uma união de padrões CSS que cobre qualquer versão do
# componente product-summary/product-card VTEX-IO. Confirmado ao vivo contra
# foxton (tema FastStore/Store Framework, classes CSS-module com hash de
# build, ex. `style_product-card--wrapper__eHBJf`) em 2026-07-11.
# ------------------------------------------------------------------
_VTEXIO_TILE_SELECTORS = [
    '[class*="vtex-product-summary"][class*="container"]',
    '[class*=product-card--wrapper]',
    '[data-testid*="product-summary"]',
    '[data-testid*="product-card"]',
]
_VTEXIO_TILE_SELECTOR_CSS = ", ".join(_VTEXIO_TILE_SELECTORS)


@dataclass(frozen=True)
class _CountProbe:
    """Resultado tipado da contagem legada — nunca confunde bloqueio
    persistente (`hard_blocked`) com API indisponível/VTEX-IO (`api_unavailable`)
    ou com uma contagem real (`count`).
    """

    count: Optional[int] = None
    hard_blocked: bool = False
    api_unavailable: bool = False


class VtexApiClient(BaseScraper):
    """
    Cliente robusto para integração direta com as APIs da VTEX (Intelligent Search & Catalog System).
    Implementa rotação de identidade, retries exponenciais e validação rigorosa com Pydantic.
    """

    def __init__(self, brand_name: str, session: Optional[aiohttp.ClientSession] = None):
        self.brand_name = brand_name
        self.session = session
        self._owns_session = session is None
        self.semaphore = asyncio.Semaphore(15)

        brand_key = self.brand_name.lower().split()[0]
        self.brand_info = brand_service.get_brand(brand_key)

        # Pré-popula a conta VTEX quando já configurada em brands.json
        # (ex.: Foxton -> "foxton"), eliminando o GET extra de auto-descoberta
        # em toda chamada de produto. Sem conta configurada, preserva o
        # comportamento antigo (None/False) e o fluxo de fallback existente.
        configured_account = getattr(self.brand_info, "vtex_account", None) if self.brand_info else None
        if configured_account:
            self.resolved_account = configured_account
            self.use_stable_fallback = True
        else:
            self.resolved_account = None
            self.use_stable_fallback = False

    @staticmethod
    def _discover_account_from_html(domain: str, html_content: str) -> str:
        """Extrai o nome da conta VTEX de um HTML bruto. Ver services.vtex_parsing."""
        return vtex_parsing.discover_account_from_html(domain, html_content)

    @staticmethod
    async def fetch_categories(domain: str, depth: int = 3) -> List[Dict[str, Any]]:
        """
        Motor de extração com Auto-Discovery do nome da conta VTEX.
        Perfeito para contornar setups Headless/FastStore sem depender de input manual.
        """
        domain = domain.replace("https://", "").replace("http://", "").strip("/")
        url_principal = f"https://{domain}/api/catalog_system/pub/category/tree/{depth}"

        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        try:
            async with AsyncSession(impersonate="chrome", timeout=15) as session:
                logger.info(f"[FETCH] Tentando domínio principal: {url_principal}")
                response = await session.get(url_principal, headers=headers)

                if response.status_code == 200:
                    try:
                        dados = response.json()
                        if isinstance(dados, list) and len(dados) > 0:
                            return dados
                        logger.warning("[WARNING] Árvore vazia ou inválida no domínio principal.")
                    except (json.JSONDecodeError, Exception):
                        logger.warning("[WARNING] O domínio principal retornou HTML ou JSON inválido.")
                else:
                    logger.warning(f"[WARNING] Status HTTP {response.status_code} no domínio principal.")

                # --- MÓDULO DE AUTO-DISCOVERY ---
                logger.info("[DEBUG] Inspecionando o HTML para descobrir o ID real da conta VTEX...")
                account_name = VtexApiClient._discover_account_from_html(domain, response.text)
                logger.info(f"[MATCH] Conta identificada: '{account_name}'")

                url_fallback = f"https://{account_name}.vtexcommercestable.com.br/api/catalog_system/pub/category/tree/{depth}"

                # --- TENTATIVA 2: Fallback Certeiro ---
                logger.info(f"[RETRY] Acionando Fallback Oculto: {url_fallback}")
                response_fb = await session.get(url_fallback, headers=headers)

                if response_fb.status_code == 200:
                    try:
                        dados = response_fb.json()
                        logger.info("[OK] Sucesso! Conectado diretamente ao Backend da VTEX.")
                        return dados
                    except Exception:
                        logger.error("[ERROR] Falha: O Fallback oculto também retornou dados inválidos.")
                        return []
                else:
                    logger.warning(f"[ERROR] O Fallback falhou com Status HTTP {response_fb.status_code}")

                # --- MÓDULO 3: EXTREME DISCOVERY ---
                # ... (resto do código igual)
                response_home = await session.get(f"https://{domain}/", headers=headers)
                if response_home.status_code == 200:
                    html = response_home.text.lower()
                    # Procura links que pareçam categorias (evita links de sistema, contato, etc)
                    # Busca por padrões como href="/categoria", href="/departamento/categoria"
                    paths = re.findall(r'href="(/[^"]+)"', html)
                    discovered = []
                    seen_paths = set()
                    
                    # Filtros de ruído
                    noise = [".js", ".css", ".png", ".jpg", "login", "carrinho", "checkout", "minha-conta", "institucional", "fale-conosco"]
                    
                    for p in paths:
                        p = p.split("?")[0].split("#")[0].strip("/")
                        if not p or p in seen_paths or any(n in p for n in noise):
                            continue
                        
                        # Categorias costumam ter entre 1 e 3 níveis de path
                        if 1 <= p.count("/") <= 3:
                            name = p.split("/")[-1].replace("-", " ").capitalize()
                            discovered.append({"name": name, "url": f"/{p}"})
                            seen_paths.add(p)
                    
                    if discovered:
                        logger.info(f"[EXTREME] Extreme Discovery encontrou {len(discovered)} links potenciais.")
                        return discovered

                return []

        except Exception as e:
            logger.error(f"[ERROR] Erro crítico na descoberta: {e}")
            return []

    @staticmethod
    async def validate_url(url: str) -> bool:
        """
        Valida se uma URL de categoria é funcional e contém produtos.
        Utiliza curl_cffi para bypass de WAF básico e emulação de browser.
        """
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        try:
            async with AsyncSession(impersonate="chrome", timeout=10) as session:
                logger.info(f"[VALIDATING] Validando URL: {url}")
                resp = await session.get(url, headers=headers, allow_redirects=True)
                
                if resp.status_code != 200:
                    logger.warning(f"[INVALID] URL inválida ({resp.status_code}): {url}")
                    return False
                
                # Heurística: se a página for muito pequena, provavelmente está vazia ou deu erro
                if len(resp.text) < 1000:
                    logger.warning(f"[INVALID] URL com conteúdo insuficiente: {url}")
                    return False

                # Busca por indícios de "vazio"
                content = resp.text.lower()
                empty_markers = ["nenhum produto encontrado", "0 produtos", "não encontramos produtos", "Ops!"]
                if any(marker in content for marker in empty_markers):
                    logger.warning(f"[INVALID] URL parece estar vazia: {url}")
                    return False
                
                logger.info(f"[OK] URL válida: {url}")
                return True
        except Exception as e:
            logger.warning(f"[WARNING] Erro ao validar URL {url}: {e}")
            return False

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": IdentityManager.get_random_user_agent(),
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
        }


    async def __aenter__(self):
        if self._owns_session and self.session is None:
            self.session = AsyncSession(
                impersonate="chrome120",
                timeout=settings.REQUEST_TIMEOUT_SECONDS,
                headers=self._get_headers()
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owns_session and self.session:
            await self.session.close()

    async def _ensure_account_resolved(self, domain: str):
        """Garante que a conta VTEX foi identificada para uso de fallback."""
        if self.resolved_account:
            return
        
        logger.info(f"[DEBUG] Identificando conta VTEX para {domain}...")
        headers = {"User-Agent": IdentityManager.get_random_user_agent()}
        try:
            # Tenta pegar a home para descobrir a conta
            async with AsyncSession(impersonate="chrome", timeout=10) as session:
                resp = await session.get(f"https://{domain}/", headers=headers)
                if resp.status_code == 200:
                    self.resolved_account = self._discover_account_from_html(domain, resp.text)
                    logger.info(f"[MATCH] Conta resolvida: {self.resolved_account}")
                else:
                    # Fallback básico pelo domínio
                    self.resolved_account = domain.split(".")[0] if "www" not in domain else domain.split(".")[1]
        except Exception as e:
            logger.warning(f"[WARNING] Falha ao resolver conta via HTML: {e}. Usando fallback de domínio.")
            self.resolved_account = domain.split(".")[0] if "www" not in domain else domain.split(".")[1]

    def _get_api_url(self, original_url: str) -> str:
        """Retorna a URL da API, possivelmente usando o domínio estável se habilitado."""
        if not self.use_stable_fallback or not self.resolved_account:
            return original_url
        
        parsed = urlparse(original_url)
        stable_domain = f"{self.resolved_account}.vtexcommercestable.com.br"
        return original_url.replace(parsed.netloc, stable_domain)

    def _sanitize_product_url(self, url: str, public_domain: str) -> str:
        """Aponta o link para o domínio público. Ver services.vtex_parsing."""
        return vtex_parsing.sanitize_product_url(url, public_domain)

    async def _request_json(self, url: str, **kwargs) -> Optional[Any]:
        """Realiza requisições GET e retorna o JSON validado, com auto-fallback."""
        if not self.session:
            return None

        parsed = urlparse(url)
        domain = parsed.netloc
        
        for attempt in range(settings.MAX_RETRIES):
            try:
                current_url = self._get_api_url(url)
                current_proxy = IdentityManager.get_proxy()
                resp = await self.session.get(current_url, proxy=current_proxy, **kwargs)
                resp_status = getattr(resp, "status_code", getattr(resp, "status", None))
                if resp_status in (200, 206):
                    try:
                        # Verifica se o content type é mesmo JSON
                        if "text/html" in resp.headers.get("Content-Type", ""):
                            raise ValueError("Recebido HTML em vez de JSON")
                        json_attr = getattr(resp, "json", None)
                        if callable(json_attr):
                            json_val = json_attr()
                            if asyncio.iscoroutine(json_val):
                                return await json_val
                            return json_val
                        elif asyncio.iscoroutine(json_attr):
                            return await json_attr
                        return json_attr
                    except (ValueError, json.JSONDecodeError):
                        if not self.use_stable_fallback:
                            logger.warning(f"[WARNING] {domain} retornou HTML. Tentando resolver fallback estável...")
                            await self._ensure_account_resolved(domain)
                            self.use_stable_fallback = True
                            # Recomeça o loop com a nova URL
                            continue
                        else:
                            logger.error(f"[ERROR] Fallback estável também falhou em {current_url}")
                            return None
                if resp_status == 403 or resp_status == 401:
                    logger.warning(f"[ANTIBOT] Bloqueio detectado ({resp_status}). Acionando Playwright Fallback...")
                    try:
                        html_content = await browser_manager.fetch_html(current_url)
                        # Se for uma API VTEX, o JSON pode estar dentro de um <pre> ou ser o conteúdo puro
                        # Se o Playwright pegou o HTML puro que parece JSON, tentamos fazer o parse
                        clean_json = html_content
                        if "<pre" in html_content:
                            match = re.search(r"<pre[^>]*>(.*?)</pre>", html_content, re.DOTALL)
                            if match:
                                clean_json = match.group(1)
                        
                        return json.loads(clean_json)
                    except Exception as e:
                        logger.error(f"[PLAYWRIGHT FAIL] Fallback falhou: {e}")
                        return None

                if resp_status == 429:
                    wait = (attempt + 1) * 5
                    logger.warning(f"Rate limit (429) em {current_url}. Aguardando {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                    
                if resp_status is not None and resp_status >= 500:
                    logger.warning(f"Erro de servidor ({resp_status}) em {current_url}. Retry {attempt+1}/{settings.MAX_RETRIES}")
                    await asyncio.sleep(2 ** attempt)
                    continue
                
                logger.debug(f"HTTP {resp_status} em {current_url}")
                return None

            except Exception as e:
                logger.error(f"Erro na requisição a {url} (Tentativa {attempt+1}): {type(e).__name__} {str(e)}")
                await asyncio.sleep(2 ** attempt)
        
        return None
    def _transform_url_to_api(self, url: str) -> str:
        """Converte URL de produto frontend para a API. Ver services.vtex_parsing."""
        return vtex_parsing.transform_url_to_api(url)

    def _extract_colors(self, p: VtexProduct) -> List[str]:
        """Extrai as cores do produto. Ver services.vtex_parsing."""
        aliases = []
        if hasattr(self, "brand_info") and self.brand_info and hasattr(self.brand_info, "color_aliases"):
            aliases = self.brand_info.color_aliases
        return vtex_parsing.extract_colors(p, aliases)

    async def _get_color_family(self, domain: str, p: VtexProduct, specs_dict: dict) -> List[str]:
        """Chama a API de Cross-Selling da VTEX e busca por Grupo de Similares para achar as outras cores do mesmo modelo."""
        cores_da_familia = set()
        url_similares = f"https://{domain}/api/catalog_system/pub/products/crossselling/similars/{p.productId}"
        
        json_data = await self._request_json(url_similares, timeout=aiohttp.ClientTimeout(total=5))
        if json_data and isinstance(json_data, list):
            for prod_data in json_data:
                try:
                    # Validação parcial via Pydantic para extração segura
                    vtex_prod = VtexProduct.model_validate(prod_data)
                    for cor in self._extract_colors(vtex_prod):
                        cores_da_familia.add(cor)
                except Exception:
                    continue
        
        # Estratégia de fallback: busca pelo grupo de família definido dinamicamente (ex: Reserva usa 'Grupo de Produtos Similares')
        product_group = None
        for alias in getattr(self.brand_info, "family_aliases", []):
            if alias in specs_dict:
                product_group = specs_dict[alias]
                break
                
        if product_group:
            url_group = f"https://{domain}/api/catalog_system/pub/products/search?fq=alternateIds_RefId:{product_group}*"
            group_data = await self._request_json(url_group, timeout=aiohttp.ClientTimeout(total=5))
            if group_data and isinstance(group_data, list):
                for prod_data in group_data:
                    try:
                        vtex_prod = VtexProduct.model_validate(prod_data)
                        for cor in self._extract_colors(vtex_prod):
                            cores_da_familia.add(cor)
                    except Exception:
                        continue
            
        return list(cores_da_familia)

    def _extract_sizes(self, items: List[VtexItem]) -> List[str]:
        """Extrai os tamanhos com estoque. Ver services.vtex_parsing."""
        extract_regex = False
        if hasattr(self, "brand_info") and self.brand_info and hasattr(self.brand_info, "extract_via_regex"):
            extract_regex = self.brand_info.extract_via_regex
        return vtex_parsing.extract_sizes(items, extract_regex)

    def _extract_size_grid(self, items: List[VtexItem]) -> List[str]:
        """Extrai a grade completa de tamanhos (com ou sem estoque). Ver services.vtex_parsing."""
        extract_regex = False
        if hasattr(self, "brand_info") and self.brand_info and hasattr(self.brand_info, "extract_via_regex"):
            extract_regex = self.brand_info.extract_via_regex
        return vtex_parsing.extract_size_grid(items, extract_regex)

    # ------------------------------------------------------------------
    # Category content probe (D-04/D-07, Planos 01-03/01-07) — count via
    # legacy Search API (limit 1: _from=0&_to=0), com fallback a tiles
    # VTEX-IO renderizados quando a contagem legada devolver 0 OU quando a
    # própria API legada estiver indisponível (VTEX-IO sem legacy Search,
    # ex. foxton: HTTP 200 com corpo não-JSON, ou 404/5xx não recuperado).
    # Bloqueio confirmado (403/429 persistente) NUNCA aciona o fallback caro
    # — D-07 evita amplificar carga contra um WAF já confirmado.
    # ------------------------------------------------------------------
    async def _legacy_search_count(self, path: str, domain: str) -> _CountProbe:
        """Conta produtos oficiais de uma categoria via o endpoint de busca legado.

        Classificação (D-07): HTTP 200/206 com total parseável -> `count`;
        HTTP 403/429 (após retries bounded para 429; 403 nunca re-tentado —
        bloqueio persistente de anti-bot) -> `hard_blocked=True`; qualquer
        outro não-200 (404, 5xx não recuperado, corpo não-JSON em 200) ->
        `api_unavailable=True`. `_PROBE_MAX_RETRIES`/`_PROBE_BACKOFF_BASE_SECONDS`
        limitam o retry a 429/5xx com backoff exponencial, mantendo a
        latência total bem abaixo de `PER_CANDIDATE_PROBE_TIMEOUT_SECONDS`.
        """
        if not self.session:
            return _CountProbe(api_unavailable=True)

        path_str = path if path.startswith("/") else f"/{path}"
        url = f"https://{domain}/api/catalog_system/pub/products/search{path_str}?_from=0&_to=0"

        attempt = 0
        while True:
            try:
                current_url = self._get_api_url(url)
                current_proxy = IdentityManager.get_proxy()
                resp = await self.session.get(
                    current_url, proxy=current_proxy, timeout=15
                )
                status = getattr(resp, "status_code", getattr(resp, "status", None))
                
                if status in (200, 206):
                    res_header = resp.headers.get("resources", "")
                    if "/" in res_header:
                        try:
                            return _CountProbe(count=int(res_header.split("/")[-1]))
                        except ValueError:
                            pass

                    try:
                        body = resp.json()
                        if asyncio.iscoroutine(body):
                            body = await body
                    except (ValueError, json.JSONDecodeError):
                        logger.warning(
                            "[%s] legacy count %s -> HTTP %s sem corpo JSON "
                            "(API legada mascarada por HTML, testando fallback backend estavel)",
                            domain, path, status,
                        )
                        if not self.use_stable_fallback:
                            await self._ensure_account_resolved(domain)
                            if self.resolved_account:
                                stable_url = f"https://{self.resolved_account}.vtexcommercestable.com.br/api/catalog_system/pub/products/search{path_str}?_from=0&_to=0"
                                try:
                                    resp_s = await self.session.get(stable_url, proxy=current_proxy, timeout=8)
                                    resp_s_status = getattr(resp_s, "status_code", getattr(resp_s, "status", None))
                                    if resp_s_status in (200, 206):
                                        res_h_s = resp_s.headers.get("resources", "")
                                        if "/" in res_h_s:
                                            try:
                                                self.use_stable_fallback = True
                                                return _CountProbe(count=int(res_h_s.split("/")[-1]))
                                            except ValueError:
                                                pass
                                        body_s = resp_s.json()
                                        if asyncio.iscoroutine(body_s):
                                            body_s = await body_s
                                        if isinstance(body_s, list):
                                            self.use_stable_fallback = True
                                            return _CountProbe(count=len(body_s))
                                except Exception:
                                    pass
                        return _CountProbe(api_unavailable=True)
                    if isinstance(body, list):
                        return _CountProbe(count=len(body))
                    return _CountProbe(api_unavailable=True)

                if status == 403:
                    # Bloqueio de anti-bot persistente — nunca re-tenta (D-07).
                    logger.warning(
                        "[%s] legacy count %s -> HTTP 403 (bloqueio, sem retry)",
                        domain, path,
                    )
                    return _CountProbe(hard_blocked=True)

                if status == 429 or status >= 500:
                    logger.warning(
                        "[%s] legacy count %s -> HTTP %s (tentativa %d/%d)",
                        domain, path, status, attempt + 1, _PROBE_MAX_RETRIES + 1,
                    )
                    if attempt < _PROBE_MAX_RETRIES:
                        await asyncio.sleep(_PROBE_BACKOFF_BASE_SECONDS * (2 ** attempt))
                        attempt += 1
                        continue
                    # Esgotou os retries: 429 é bloqueio confirmado; 5xx
                    # persistente é tratado como API indisponível.
                    return _CountProbe(
                        hard_blocked=(status == 429),
                        api_unavailable=(status >= 500),
                    )

                # Qualquer outro status (ex.: 404 em VTEX-IO) — tenta fallback estável antes de desistir
                logger.warning(
                    "[%s] legacy count %s -> HTTP %s", domain, path, status
                )
                if status == 404 and not self.use_stable_fallback:
                    await self._ensure_account_resolved(domain)
                    if self.resolved_account:
                        stable_url = f"https://{self.resolved_account}.vtexcommercestable.com.br/api/catalog_system/pub/products/search{path_str}?_from=0&_to=0"
                        try:
                            resp_s = await self.session.get(stable_url, proxy=current_proxy, timeout=8)
                            resp_s_status = getattr(resp_s, "status_code", getattr(resp_s, "status", None))
                            if resp_s_status in (200, 206):
                                res_h_s = resp_s.headers.get("resources", "")
                                if "/" in res_h_s:
                                    try:
                                        self.use_stable_fallback = True
                                        return _CountProbe(count=int(res_h_s.split("/")[-1]))
                                    except ValueError:
                                        pass
                                body_s = resp_s.json()
                                if asyncio.iscoroutine(body_s):
                                    body_s = await body_s
                                if isinstance(body_s, list):
                                    self.use_stable_fallback = True
                                    return _CountProbe(count=len(body_s))
                        except Exception:
                            pass
                return _CountProbe(api_unavailable=True)

            except Exception as exc:
                # T-01-07-02: apenas domínio, path e o TIPO da exceção — nunca a
                # mensagem completa (pode carregar proxy/URL interna).
                logger.warning(
                    "[%s] legacy count %s -> exception %s (tentativa %d/%d)",
                    domain, path, type(exc).__name__, attempt + 1, _PROBE_MAX_RETRIES + 1,
                )
                if attempt < _PROBE_MAX_RETRIES:
                    await asyncio.sleep(_PROBE_BACKOFF_BASE_SECONDS * (2 ** attempt))
                    attempt += 1
                    continue
                return _CountProbe(hard_blocked=True)

    async def category_result_count(self, path: str, domain: str) -> Optional[int]:
        """Wrapper fino e público (contrato preservado, hermeticamente travado
        em outros lugares): delega para `_legacy_search_count` e devolve só o
        `count` (`None` nunca significa 0 — nem bloqueio nem API indisponível
        viram silenciosamente uma contagem real, D-04)."""
        probe = await self._legacy_search_count(path, domain)
        return probe.count

    async def category_result_count_with_tiles_fallback(
        self, category_url: str, path: str, domain: str
    ) -> Optional[int]:
        """`_legacy_search_count` + fallback de tiles VTEX-IO renderizados.

        O fallback (`_browser_fallback_products` -> tiles) é acionado quando
        a contagem legada é genuinamente 0 OU quando a própria API legada
        está indisponível (`api_unavailable`, ex. VTEX-IO sem legacy Search —
        D-07 foxton fix). NUNCA é acionado sobre um bloqueio confirmado
        (`hard_blocked`) — isso amplificaria carga contra um WAF já
        confirmado e é a causa raiz do estouro de timeout por marca (D-07).
        """
        probe = await self._legacy_search_count(path, domain)
        if probe.hard_blocked:
            return None  # BLOCKED — nunca tenta o fallback caro sobre bloqueio confirmado
        if probe.count is not None:
            return probe.count

        # Fallback 1: VTEX Intelligent Search REST API (rápido, sem browser)
        clean_path = path.strip("/")
        if clean_path and self.session:
            try:
                is_url = f"https://{domain}/_v/api/intelligent-search/product_search/{clean_path}"
                is_resp = await asyncio.wait_for(
                    self.session.get(is_url, proxy=IdentityManager.get_proxy()),
                    timeout=4.0
                )
                if is_resp.status_code == 200:
                    is_data = is_resp.json()
                    if isinstance(is_data, dict):
                        records = is_data.get("recordsFiltered")
                        if records is not None and records > 0:
                            return records
                        if records == 0:
                            return 0
                        prods = is_data.get("products", [])
                        if prods:
                            return len(prods)
            except Exception:
                pass

        # D-08c (Plano 01-11): a URL absoluta devolvida pela árvore oficial da
        # VTEX pode apontar para o domínio genérico da conta (ex.
        # `foxton.vtexcommercestable.com.br`), que não necessariamente serve a
        # MESMA loja/tema do domínio de produção cadastrado — navegar para ela
        # pode renderizar conteúdo errado/vazio ou redirecionar para o login do
        # admin (achado ao vivo, foxton: 174/174 candidatos usavam esse
        # domínio genérico, todos falso-vazios). Reconstrói sempre a partir do
        # domínio confiável cadastrado da marca (`brand_service`) + o path da
        # categoria — nunca confia na URL absoluta da árvore.
        trusted_url = f"https://{domain}{path}"
        try:
            tiles = await self._browser_fallback_products(trusted_url, domain)
            return len(tiles) if tiles else 0
        except Exception as exc:
            logger.debug(
                "[%s] Fallback VTEX-IO (tiles) falhou para '%s': %s", domain, trusted_url, exc
            )
            return None  # unverified/bloqueado — nunca confundido com vazio

    async def parse_product_dict(self, data: dict, product_url: str, domain: str) -> Optional[RawProductBronze]:
        """Faz o parse estrutural de um produto (dict) retornando o modelo Camada Bronze."""
        try:
            # Validação Rigorosa com Pydantic
            p = VtexProduct.model_validate(data)
            
            # Extração de Preços (usa a primeira variação com estoque)
            price_full = 0.0
            price_discount = None
            has_availability = False
            selected_item = None
            selected_seller = None
            
            for item in p.items:
                for seller in item.sellers:
                    offer = seller.commertialOffer
                    if offer.AvailableQuantity > 0:
                        has_availability = True
                        if price_full == 0.0:
                            selected_item = item
                            selected_seller = seller
                            p_list = offer.ListPrice
                            p_sale = offer.Price
                            if p_sale:
                                if p_list and p_list > p_sale:
                                    price_full = p_list
                                    price_discount = p_sale
                                else:
                                    price_full = p_sale
                                    price_discount = None
                            break
                if price_full > 0.0:
                    break

            # Caso nenhum item tenha estoque, tenta pegar preço do primeiro sku
            if price_full == 0.0 and p.items:
                try:
                    selected_item = p.items[0]
                    selected_seller = selected_item.sellers[0]
                    offer = selected_seller.commertialOffer
                    price_full = offer.Price
                except (IndexError, AttributeError) as e:
                    logger.debug("Sem preço no primeiro SKU (produto esgotado): %s", e)

            # Extração de Especificações
            specs_dict = {}
            for k in p.allSpecifications:
                v = getattr(p, k, [])
                specs_dict[k] = v[0] if isinstance(v, list) and v else str(v)
            
            # Extração de Cores e Família
            cor_atual = self._extract_colors(p)
            cores_irmas = await self._get_color_family(domain, p, specs_dict)
            # dict.fromkeys preserva ordem de primeira ocorrência (D-01) — nunca usar
            # set() aqui, que embaralha a ordem/capitalização originais.
            todas_as_cores = list(dict.fromkeys(cor_atual + cores_irmas))
            
            # Avaliações (Integrado com o serviço existente)
            brand_key = self.brand_name.lower().split()[0]
            rating, count = await get_single_review(brand_key, p.productId)
            
            # Categoria
            category, sub_category = None, None
            breadcrumb: List[str] = []
            if p.categories:
                # VTEX categories são como "/Masculino/Calças/Jeans/"
                parts = [pt for pt in p.categories[0].split("/") if pt]
                if len(parts) >= 1:
                    category = parts[0]
                if len(parts) >= 2:
                    sub_category = parts[1]
                breadcrumb = parts

            normalized_specs = normalize_specifications_aliases(specs_dict)
            composition = normalized_specs.get("composition")
            gender = normalized_specs.get("gender") or specs_dict.get("GÊNERO") or specs_dict.get("Genero")
            age_group = normalized_specs.get("age_group") or specs_dict.get("SUB CATEGORIA") or specs_dict.get("Idade")
            product_group = normalized_specs.get("product_group") or specs_dict.get("Grupo de Produtos Similares") or specs_dict.get("PRODUTO BASE")
            # D-03: product_code é SEMPRE o productId nativo da VTEX — nunca
            # productReference/specifications (Referencia/Codigo/Ref.), que são
            # campos de negócio editáveis e não a identidade do produto na API.
            product_code = str(p.productId)

            # Imagem
            image_url = None
            if p.items:
                images = p.items[0].images
                if images and images[0].imageUrl:
                    image_url = images[0].imageUrl
            
            if not image_url:
                image_url = "https://via.placeholder.com/300?text=Sem+Imagem"

            tamanhos = self._extract_sizes(p.items)
            grade_tamanhos = self._extract_size_grid(p.items)
            # Contexto comercial autoritativo do item/seller selecionados (GAP-01,
            # Fase 03 Plano 13) — capturado INDEPENDENTEMENTE de haver Teaser/Benefit,
            # para que sku_id/seller_id/price_without_discount/collection_ids nunca
            # dependam de já existir uma oferta condicional detectada.
            commercial = selected_seller.commertialOffer if selected_seller is not None else None
            conditional_offers = []
            if selected_item is not None and selected_seller is not None:
                conditional_offers = parse_teasers(
                    {
                        "productId": p.productId,
                        "itemId": selected_item.itemId,
                        "sellerId": selected_seller.sellerId,
                        "Teasers": commercial.Teasers,
                        "Benefits": commercial.Benefits,
                    }
                )

            return RawProductBronze(
                url=product_url,
                brand=self.brand_name,
                raw_title=p.productName,
                raw_description=p.description or "Sem descrição",
                price_full=price_full,
                price_discount=price_discount,
                price_discount_is_delta=False,

                stock_availability=has_availability,
                category=category,
                sub_category=sub_category,
                breadcrumb=breadcrumb,
                composition=composition,
                gender=gender,
                age_group=age_group,
                product_group=product_group,
                product_code=product_code,
                available_colors=sanitize_colors(todas_as_cores),
                available_sizes=sanitize_sizes(tamanhos),
                # D-01: proveniência bruta — mesma extração explícita usada para
                # available_colors/available_sizes (sem normalização adicional
                # nesta camada); nunca reconstruída a partir de specs/título.
                raw_colors=list(todas_as_cores),
                raw_sizes=list(tamanhos),
                size_grid=sanitize_sizes(grade_tamanhos),
                raw_size_grid=list(grade_tamanhos),
                rating=rating,
                review_count=count,
                review_product_id=str(p.productId),
                specifications=normalized_specs,
                image_url=image_url,
                conditional_offers=conditional_offers,
                sku_id=str(selected_item.itemId) if selected_item is not None else None,
                seller_id=str(selected_seller.sellerId) if selected_seller is not None else None,
                price_without_discount=commercial.PriceWithoutDiscount if commercial is not None else None,
                collection_ids=list(getattr(p, "collectionIds", None) or []),
            )

        except Exception as e:
            logger.error(f"Erro no parse_product_dict para {product_url}: {e}")
            return None

    async def get_product_by_url(self, product_url: str) -> Optional[RawProductBronze]:
        """Extrai todos os dados estruturados de um produto VTEX usando apenas chamadas de API."""
        api_url = self._transform_url_to_api(product_url)
        parsed_url = urlparse(product_url)
        domain = parsed_url.netloc
        
        json_data = await self._request_json(api_url, timeout=aiohttp.ClientTimeout(total=10))
        if json_data and isinstance(json_data, list) and len(json_data) > 0:
            final_url = self._sanitize_product_url(product_url, domain)
            return await self.parse_product_dict(json_data[0], final_url, domain)
            
        return None


    # Delay entre tentativas de frete — definido aqui para ser patchável nos testes.
    _SHIPPING_RETRY_SLEEP: float = 0.3

    async def simulate_shipping(
        self,
        sku_id: str,
        seller_id: str,
        zipcode: str,
        domain: str,
        sales_channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Simula o frete via checkout VTEX e retorna estado + opções (sem mutar produto).

        Contrato de estados (D-13, D-14, D-15):
          - available            → shipping_options populado (ordenado por preço asc, prazo asc).
          - unavailable_for_cep  → 200 válido sem SLA de entrega; 1 chamada, sem retry.
          - temporary_failure    → erro de transporte/HTTP retryável após 2 tentativas.

        Retorna: {"state": str, "shipping_options": List[ShippingInfo]}.

        Segurança (T-33-01): URL construída somente do `domain` persistido — nunca de input do caller.
        Privacidade (T-33-02): CEP e payload nunca logados em info/error.
        Concorrência (T-33-03): retry permanece dentro de `self.semaphore`.
        """
        url = f"https://{domain}/api/checkout/pub/orderForms/simulation"
        if sales_channel:
            url = f"{url}?sc={sales_channel}"
        payload = {
            "items": [{"id": sku_id, "quantity": 1, "seller": seller_id}],
            "postalCode": zipcode,
            "country": "BRA",
        }

        _RETRYABLE_HTTP = {408, 429}

        try:
            async with self.semaphore:
                for attempt in range(2):  # 2 total attempts: 1 call + 1 retry (D-15)
                    try:
                        current_proxy = IdentityManager.get_proxy()

                        resp = await self.session.post(
                            url, json=payload, proxy=current_proxy, timeout=5
                        )
                        
                        resp_status = getattr(resp, "status_code", None)
                        if resp_status is None:
                            resp_status = getattr(resp, "status", None)

                        if resp_status == 200:
                            json_attr = getattr(resp, "json", None)
                            if callable(json_attr):
                                json_val = json_attr()
                                if asyncio.iscoroutine(json_val):
                                    data = await json_val
                                else:
                                    data = json_val
                            elif asyncio.iscoroutine(json_attr):
                                data = await json_attr
                            else:
                                data = json_attr or {}

                            # Flatten SLAs de TODAS as entradas de logisticsInfo (CR-02):
                            # hard-indexar [0] descarta frete quando a 1ª entrada não tem
                            # slas mas outras têm (multi-item / payloads malformados).
                            logistics = data.get("logisticsInfo") or []
                            all_slas = []
                            for entry in logistics:
                                if isinstance(entry, dict):
                                    all_slas.extend(entry.get("slas") or [])
                            options = filter_and_sort_slas(all_slas)
                            state = classify_result(options, transport_error=False)

                            if state == "available":
                                shipping_options = [
                                    ShippingInfo(
                                        price=opt["price_reais"],
                                        status="Grátis" if opt["is_free_shipping"] else "Disponível",
                                        estimated_delivery_days=opt.get("estimate_value"),
                                        raw_text=f"{opt.get('name', '')} - {opt.get('shippingEstimate', '')}",
                                        service_name=opt.get("name"),
                                        service_id=opt.get("id"),
                                        estimate_display=opt.get("estimate_display"),
                                        estimate_unit=opt.get("estimate_unit"),
                                        is_free_shipping=opt["is_free_shipping"],
                                    )
                                    for opt in options
                                ]
                                return {"state": "available", "shipping_options": shipping_options}

                            # 200 sem SLA domiciliar válido — resultado de negócio, NÃO retentar (D-15, pitfall 5)
                            return {"state": "unavailable_for_cep", "shipping_options": []}

                        # Retryable HTTP statuses or 5xx
                        if resp_status in _RETRYABLE_HTTP or (resp_status is not None and resp_status >= 500):
                            logger.warning(
                                "[%s] Simulação de frete retornou HTTP %s (tentativa %d/2)",
                                domain, resp_status, attempt + 1,
                            )
                            if attempt < 1:
                                await asyncio.sleep(self._SHIPPING_RETRY_SLEEP)
                                continue
                            # Second failure — fall through to temporary_failure below
                        else:
                            # Non-retryable 4xx — treat as transport error without retry
                            logger.warning(
                                "[%s] Simulação de frete retornou HTTP %s não-retryável",
                                domain, resp_status,
                            )
                            break

                    except Exception as exc:
                        logger.warning(
                            "[%s] Erro de transporte na simulação de frete (tentativa %d/2): %s",
                            domain, attempt + 1, type(exc).__name__,
                        )
                        if attempt < 1:
                            await asyncio.sleep(self._SHIPPING_RETRY_SLEEP)
                            continue
                        # Second failure — fall through

        except Exception as exc:
            # Absorbe erros do bloco do semaphore para não cancelar asyncio.gather siblings (D-13)
            logger.warning("[%s] Falha inesperada na simulação de frete: %s", domain, type(exc).__name__)

        # Chegou aqui: temporary_failure após esgotamento das tentativas
        return {"state": "temporary_failure", "shipping_options": []}

    async def _fetch_shipping(
        self,
        sku_id: str,
        seller_id: str,
        zipcode: str,
        domain: str,
        prod_result: Any,
    ) -> None:
        """Wrapper fino: simula o frete e popula o produto in-place (busca inline)."""
        result = await self.simulate_shipping(sku_id, seller_id, zipcode, domain)
        state = result["state"]
        options = result["shipping_options"]

        if state == "available" and options:
            prod_result.shipping_options = options
            # Primary = mais barato (primeiro após ordenação)
            primary = options[0]
            prod_result.shipping = primary
            prod_result.shipping_price = primary.price
            prod_result.is_free_shipping = primary.is_free_shipping
        elif state == "unavailable_for_cep":
            prod_result.shipping = ShippingInfo(
                status="Entrega indisponível para este CEP",
                raw_text="Sem modalidade de entrega para o CEP informado",
            )
        else:
            prod_result.shipping = ShippingInfo(
                status="Frete temporariamente indisponível",
                raw_text="Não foi possível simular o frete neste momento",
            )

    @classmethod
    async def calculate_for_brand(
        cls,
        brand_key: str,
        sku_id: str,
        seller_id: str,
        zipcode: str,
        sales_channel: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Calcula o frete de um SKU sob demanda, resolvendo o domínio pela marca persistida.

        Endpoint-facing: abre a própria sessão, valida que a marca é VTEX (D-03) e
        ancora a URL ao domínio cadastrado (T-33-01 — nunca a partir de input do caller).
        Levanta ValueError para marca inexistente/não-VTEX (o caller mapeia para HTTP 4xx).
        """
        brand_info = brand_service.get_brand(brand_key)
        if not brand_info:
            raise ValueError(f"Marca '{brand_key}' não cadastrada.")
        if (brand_info.engine or "vtex").lower() != "vtex":
            raise ValueError(
                f"Cálculo de frete via checkout só é suportado para marcas VTEX "
                f"(marca '{brand_key}' usa engine '{brand_info.engine}')."
            )

        if not sales_channel:
            sales_channel = getattr(brand_info, "vtex_sales_channel", None) or (
                brand_info.get("vtex_sales_channel") if isinstance(brand_info, dict) else None
            )

        domain = brand_info.domain.replace("https://", "").replace("http://", "").strip("/")
        client = cls(brand_name=brand_info.brand_name)
        async with AsyncSession(impersonate="chrome120", timeout=15) as session:
            client.session = session
            result = await client.simulate_shipping(
                sku_id, seller_id, zipcode, domain, sales_channel=sales_channel
            )

            # Se não retornou disponíveis e não tinha sales_channel explícito, para Calvin Klein tenta sc=18
            if result.get("state") != "available" and not sales_channel:
                if brand_key == "calvinklein" or "calvinklein" in domain:
                    ck_result = await client.simulate_shipping(
                        sku_id, seller_id, zipcode, domain, sales_channel="18"
                    )
                    if ck_result.get("state") == "available":
                        return ck_result

            if result.get("state") == "temporary_failure":
                fallback_domains = []
                if domain.startswith("www."):
                    fallback_domains.append(f"secure.{domain[4:]}")
                vtex_acc = getattr(brand_info, "vtex_account", None) or (
                    brand_info.get("vtex_account") if isinstance(brand_info, dict) else None
                )
                if vtex_acc:
                    fallback_domains.append(f"{vtex_acc}.vtexcommercestable.com.br")
                for alt_domain in fallback_domains:
                    alt_result = await client.simulate_shipping(
                        sku_id, seller_id, zipcode, alt_domain, sales_channel=sales_channel
                    )
                    if alt_result.get("state") in ("available", "unavailable_for_cep"):
                        result = alt_result
                        break

        return result

    async def scrape_category_paged(
        self,
        category_url: str,
        log_callback=None,
        cancel_event: Optional[asyncio.Event] = None,
        chunk_size=50,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        """
        Varre a categoria paginando o endpoint Search API.
        Processa todos os produtos de cada bloco de forma assíncrona.
        Dá yield em cada produto extraído (Streaming).
        """
        if not self.session:
            raise RuntimeError("VtexApiClient session not initialized. Use 'async with VtexApiClient(...) as client:'")
            
        def log(msg):
            if log_callback:
                if isinstance(msg, dict):
                    log_callback(msg)
                else:
                    log_callback({"type": "info", "message": msg})
            else:
                logger.info(msg)

        parsed = urlparse(category_url)
        domain = parsed.netloc
        path = parsed.path.strip("/")
        query_suffix = f"&{parsed.query}" if parsed.query else ""
        
        # A VTEX aceita mapeamento de diretório diretamente na busca
        api_base_url = f"https://{domain}/api/catalog_system/pub/products/search/{path}"

        pagina = 0

        log(f"[START] Iniciando extração API-First paginada: {category_url}")
        
        # Pequena pausa para garantir que o frontend (WebSocket) conecte antes de enviarmos os logs
        await asyncio.sleep(1.5)

        total_produtos = None
        stats_emitido = False

        async def build_product(p):
            link = p.get("link")
            if not link:
                link_text = p.get("linkText")
                if link_text:
                    link = f"https://{domain}/{link_text}/p"
            
            # Garante link absoluto e aponta para o domínio público
            if link:
                if not link.startswith("http"):
                    link = f"https://{domain}{link}"
                link = self._sanitize_product_url(link, domain)
                
            if not link:
                return None
                
            prod = await self.parse_product_dict(p, link, domain)
            if prod:
                log({"type": "brand_success", "message": f"Sucesso: {prod.raw_title}"})
            return prod

        async def build_product_safely(p):
            async with self.semaphore:
                return await build_product(p)

        while True:
            if cancel_event and cancel_event.is_set():
                log("🛑 Varredura interrompida pelo usuário.")
                break

            _from = pagina * chunk_size
            _to = _from + chunk_size - 1
            url = f"{api_base_url}?_from={_from}&_to={_to}{query_suffix}"
            
            log(f"📥 Buscando página {pagina + 1} (itens de {_from} a {_to})...")
            log(f"🔗 URL acessada: {url}")
            
            try:
                current_url = self._get_api_url(url)
                response = await self.session.get(current_url, proxy=IdentityManager.get_proxy(), timeout=15)
                # Detecta se retornou HTML em vez de JSON
                if "text/html" in response.headers.get("Content-Type", ""):
                    if not self.use_stable_fallback:
                        logger.warning(f"[WARNING] {domain} retornou HTML em busca. Tentando fallback...")
                        await self._ensure_account_resolved(domain)
                        self.use_stable_fallback = True
                        continue # Tenta a mesma página com a nova URL
                    else:
                        log(f"[ERROR] Fallback estável também falhou (HTML) em {current_url}")
                        break

                if response.status_code not in (200, 206):
                    log(f"[ERROR] API retornou HTTP {response.status_code}")
                    break
                    
                # Puxa o total de produtos pelo header na primeira requisição
                if not stats_emitido and "resources" in response.headers:
                    res_header = response.headers.get("resources", "")
                    if "/" in res_header:
                        try:
                             # Nao enviamos mais o total de produtos para evitar confusao de paridade
                             stats_emitido = True
                        except ValueError:
                            pass
                    
                raw_products = response.json()
                
                if not raw_products or not isinstance(raw_products, list) or len(raw_products) == 0:
                    if pagina == 0:
                        # 1. Fallback VTEX Intelligent Search (REST API ultra-rápida)
                        try:
                            is_url = f"https://{domain}/_v/api/intelligent-search/product_search/{path}"
                            if parsed.query:
                                is_url = f"{is_url}?{parsed.query}"
                            log(f"[INFO] Catalog System vazio na pág 1. Tentando VTEX Intelligent Search: {is_url}")
                            is_resp = await self.session.get(is_url, proxy=IdentityManager.get_proxy(), timeout=12)
                            if is_resp.status_code == 200:
                                is_data = is_resp.json()
                                is_prods = is_data.get("products", []) if isinstance(is_data, dict) else []
                                if is_prods:
                                    log(f"[OK] Fallback VTEX Intelligent Search extraiu {len(is_prods)} produtos!")
                                    is_tasks = [build_product_safely(p) for p in is_prods]
                                    is_results = await asyncio.gather(*is_tasks)
                                    for r in is_results:
                                        if r:
                                            yield r
                                    log("[OK] Fim da categoria alcançado!")
                                    break
                        except Exception as is_err:
                            logger.debug("Intelligent Search fallback falhou: %s", is_err)

                        # 2. Fallback visual Playwright caso APIs retornem vazio
                        log("[WARNING] API Catalog System retornou vazio na primeira página. Acionando Fallback VTEX IO (Browser)...")
                        try:
                            fallback_products = await self._browser_fallback_products(category_url, domain)
                            if fallback_products:
                                log(f"[OK] Fallback VTEX IO extraiu {len(fallback_products)} produtos!")
                            for prod in fallback_products:
                                yield prod
                        except Exception as e:
                            log(f"[ERROR] Fallback VTEX IO falhou: {e}")
                    
                    log("[OK] Fim da categoria alcançado!")
                    break

                # Executa as chamadas secundárias (ex: familia de cores) concorrentemente para todos do chunk
                tarefas = [build_product_safely(p) for p in raw_products]
                resultados_chunk = await asyncio.gather(*tarefas)

                for r in resultados_chunk:
                    if r:
                        yield r

                if not raw_products:
                    break

            except Exception as e:
                log({"type": "brand_error", "message": f"Falha ao varrer página {pagina + 1}: {e}"})
                break

            pagina += 1
            await asyncio.sleep(0.5) # Respeito ao rate limit VTEX
            
        log(f"[DONE] Extração concluída!")

    async def _browser_fallback_products(self, category_url: str, domain: str) -> List[RawProductBronze]:
        """Render a category page in a browser and extract products when the
        catalog_system Search API returns empty (multi-level / VTEX-IO storefronts).

        Strategies, zero regression to the legacy path:
          1) Legacy VTEX IO ROOT_QUERY Apollo blob (fast default render).
          2) VTEX-IO / Intelligent-Search rendered product-summary DOM tiles
             (e.g. Hugo Boss, foxton), which need a fuller render and carry no
             ROOT_QUERY blob. Two-tier wait strategy (D-08b, Plano 01-10):
             Tier A waits for the theme-robust tile selector to appear
             (fast — returns as soon as tiles render, no all-network-quiet hang
             on storefronts with persistent background network activity); Tier B
             is a tolerant retry WITHOUT `wait_selector` so a category that
             renders fine but genuinely has no tiles still resolves to an
             empty list (never a false BLOCKED) instead of a hard timeout.

        D-08e (Plano 01-12): stage 1 (ROOT_QUERY) failure is NOT fatal to the
        whole fallback. Some storefronts (e.g. reserva) serve unusually heavy
        category pages (2.5MB+ HTML) that take 40-50s just to reach
        `domcontentloaded` — well past stage 1's fetch, which used the
        default 30s timeout and previously re-raised immediately on any
        failure, never giving Tier A/B a chance. A stage-1 exception (timeout
        or otherwise) now falls through to Tier A/B instead of aborting,
        exactly like an empty ROOT_QUERY result already does.
        Returns a list of RawProductBronze (possibly empty). (#39 gap closure.)
        """
        try:
            html_content = await browser_manager.fetch_html(category_url)
            products = self._parse_root_query(html_content, domain, category_url)
            if products:
                return products
        except Exception as exc:
            # T-01-07-02: apenas o TIPO da exceção + a URL da categoria — nunca
            # a mensagem completa, que pode carregar URL privada/token.
            # D-08e: nao propaga — uma falha na checagem rapida de ROOT_QUERY
            # nao deve impedir a tentativa via Tier A/B abaixo.
            logger.debug(
                "[%s] fallback tiles (etapa ROOT_QUERY) falhou, tentando Tier A/B: %s",
                category_url, type(exc).__name__,
            )

        # Tier A (fast happy path): domcontentloaded + wait_selector on the
        # theme-robust tile CSS union. Returns as soon as the first real tile
        # is present instead of waiting for all network activity to go quiet
        # (D-08b: the removed all-network-quiet wait never fires on modern
        # storefronts with persistent background network activity —
        # analytics, chat, personalization).
        try:
            html_rendered = await browser_manager.fetch_html(
                category_url,
                wait_until="domcontentloaded",
                wait_selector=_VTEXIO_TILE_SELECTOR_CSS,
                timeout=20000,
                extra_sleep=1.5,
            )
            return self._parse_vtexio_tiles(html_rendered, domain)
        except Exception as exc:
            logger.warning(
                "[%s] fallback tiles (etapa render rapido) falhou: %s", category_url, type(exc).__name__
            )

        # Tier B (tolerant path, only on a Tier-A exception): retry WITHOUT
        # wait_selector. A category with no tiles at all fails Tier A's
        # wait_for_selector but renders fine here, so `_parse_vtexio_tiles`
        # correctly returns [] -> count 0 -> EMPTY (never a false BLOCKED).
        # If Tier B also raises, re-raise so the caller classifies a genuine
        # BLOCKED (real navigation error/block, not a false-empty).
        try:
            html_rendered = await browser_manager.fetch_html(
                category_url, wait_until="domcontentloaded", extra_sleep=4.0, timeout=30000
            )
        except Exception as exc:
            logger.warning(
                "[%s] fallback tiles (etapa render tolerante) falhou: %s", category_url, type(exc).__name__
            )
            raise
        return self._parse_vtexio_tiles(html_rendered, domain)

    def _parse_root_query(self, html_content: str, domain: str, category_url: str) -> List[RawProductBronze]:
        """Extract products from a legacy VTEX IO ROOT_QUERY Apollo cache blob."""
        products: List[RawProductBronze] = []
        match = re.search(r'<script>(\{"ROOT_QUERY.*?)</script>', html_content)
        if not match:
            return products
        data = json.loads(match.group(1))
        raw = [v for k, v in data.items() if isinstance(v, dict) and v.get("productId") and v.get("productName")]
        for p in raw:
            name = p.get("productName")
            link_text = p.get("linkText", "")
            link = p.get("link", f"/{link_text}/p" if link_text else "")
            full_url = self._sanitize_product_url(f"https://{domain}{link}", domain) if link else category_url
            price = 0.0
            image = None
            for ref in p.get("items", []):
                if isinstance(ref, dict) and "id" in ref:
                    item_obj = data.get(ref["id"], {})
                    for ir in item_obj.get("images", []):
                        if isinstance(ir, dict) and "id" in ir:
                            img_obj = data.get(ir["id"], {})
                            if not image and img_obj.get("imageUrl"):
                                image = img_obj.get("imageUrl")
                    for sr in item_obj.get("sellers", []):
                        if isinstance(sr, dict) and "id" in sr:
                            seller_obj = data.get(sr["id"], {})
                            comm_refs = seller_obj.get("commertialOffer", {})
                            if isinstance(comm_refs, dict) and "id" in comm_refs:
                                offer_obj = data.get(comm_refs["id"], {})
                                if offer_obj.get("Price") and offer_obj.get("Price") > 0:
                                    price = offer_obj.get("Price")
                                    break
                    if price > 0:
                        break
            try:
                products.append(RawProductBronze(
                    url=full_url, brand=self.brand_name, raw_title=name,
                    raw_description=p.get("description", "Sem descrição"),
                    price_full=price, price_discount=None,
                    stock_availability=True if price > 0 else False,
                    category=None, sub_category=None, composition=None,
                    available_colors=[], available_sizes=[], rating=None,
                    review_count=None, review_product_id=str(p.get("productId")),
                    # D-03: mesmo productId nativo do endpoint paginado — mantém a
                    # mesma identidade product-level entre bulk API e fallback ROOT_QUERY.
                    product_code=str(p.get("productId")) if p.get("productId") is not None else None,
                    specifications={}, image_url=image,
                ))
            except ValidationError:
                continue  # skip products missing price/image/title (model invariants)
        return products

    def _parse_vtexio_tiles(self, html_content: str, domain: str) -> List[RawProductBronze]:
        """Extract products from rendered VTEX-IO product-summary DOM tiles.

        Theme-robust (D-08a, Plano 01-10): the tile selector is a CSS union
        (`_VTEXIO_TILE_SELECTOR_CSS`) covering any VTEX-IO product-summary/
        product-card component version — not a single theme hardcoded to one
        brand. Title from the productBrand/name node (or, for themes without
        it, the tile's own `<h3>`), URL from the tile product anchor, price
        from the sellingPrice node (digits may be char-spaced in the DOM,
        e.g. 'R$ 1 . 460 , 00', or use a non-breaking space, e.g. 'R$\xa0298,00').
        (#39 gap closure; theme-robust selector: D-08a / Plano 01-10.)
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        products: List[RawProductBronze] = []
        seen = set()
        for tile in soup.select(_VTEXIO_TILE_SELECTOR_CSS):
            anchor = tile.find("a", href=re.compile(r"/p(\?|$)")) or tile.find("a", href=True)
            href = anchor.get("href", "") if anchor else ""
            if href.startswith("/"):
                href = f"https://{domain}{href}"
            if href:
                href = self._sanitize_product_url(href, domain)
            title_el = (
                tile.select_one("[class*=productBrand]")
                or tile.select_one("[class*=productNameContainer]")
                or tile.select_one("[class*=productName]")
                or tile.find("h3")
            )
            title = title_el.get_text(" ", strip=True) if title_el else ""
            if not title and anchor:
                title = (anchor.get("aria-label") or anchor.get_text(" ", strip=True) or "").strip()
            price_el = (
                tile.select_one("[class*=sellingPriceValue]")
                or tile.select_one("[class*=sellingPrice]")
                or tile.select_one("[class*=currencyContainer]")
                or tile.select_one("[class*=product-card--price]")
            )
            price_full = self._parse_brl_price(price_el.get_text(" ", strip=True)) if price_el else 0.0
            image = self._extract_tile_image(tile)
            # Skip tiles missing the model's hard invariants (title/url/price/image)
            # so one incomplete tile never aborts the whole category scan.
            if not title or not href or href in seen or price_full <= 0 or not image:
                continue
            seen.add(href)
            try:
                products.append(RawProductBronze(
                    url=href, brand=self.brand_name, raw_title=title,
                    raw_description="Sem descrição", price_full=price_full,
                    price_discount=None, stock_availability=True,
                    category=None, sub_category=None, composition=None,
                    available_colors=[], available_sizes=[], rating=None,
                    review_count=None, specifications={}, image_url=image,
                ))
            except ValidationError:
                continue
        return products

    @staticmethod
    def _parse_brl_price(text: str) -> float:
        """Parse a BRL price string, tolerating a char-spaced render ('R$ 1 . 460 , 00')."""
        if not text:
            return 0.0
        compact = re.sub(r"\s+", "", text)
        m = re.search(r"R\$?\s*([\d.]+,\d{2})", compact) or re.search(r"([\d.]+,\d{2})", compact)
        if not m:
            return 0.0
        num = m.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num)
        except ValueError:
            return 0.0

    @staticmethod
    def _extract_tile_image(tile) -> Optional[str]:
        """Best-effort product image URL from a VTEX product-summary tile (handles lazy-load)."""
        img = tile.find("img")
        if img:
            for attr in ("src", "data-src", "data-original"):
                v = img.get(attr)
                if v and v.strip() and not v.strip().startswith("data:"):
                    return v.strip()
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                first = srcset.split(",")[0].strip().split(" ")[0].strip()
                if first and not first.startswith("data:"):
                    return first
        source = tile.find("source")
        if source and source.get("srcset"):
            first = source.get("srcset").split(",")[0].strip().split(" ")[0].strip()
            if first and not first.startswith("data:"):
                return first
        return None

    @staticmethod
    def _fast_conditional_offers(
        product: dict[str, Any],
        *,
        product_id: str | None,
        sku_id: str | None,
        seller_id: str | None,
    ):
        """Forward only teaser evidence already present on the selected SKU/seller."""
        for item in product.get("items") or []:
            if sku_id is not None and str(item.get("itemId")) != sku_id:
                continue
            for seller in item.get("sellers") or []:
                if seller_id is not None and str(seller.get("sellerId")) != seller_id:
                    continue
                commercial = seller.get("commertialOffer") or {}
                return parse_teasers(
                    {
                        "productId": product_id,
                        "itemId": sku_id,
                        "sellerId": seller_id,
                        "Teasers": commercial.get("Teasers") or commercial.get("teasers") or [],
                        "Benefits": commercial.get("Benefits") or commercial.get("benefits") or [],
                    }
                )
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
        Executa a busca full-text na VTEX.
        Suporta ordenação.
        """
        brand_key = self.brand_name.lower().split()[0]
        brand_info = brand_service.get_brand(brand_key)
        brand_name = brand_info.brand_name if brand_info else self.brand_name

        if not brand_info:
            return BrandSearchResult(brand_key=brand_key, brand_name=brand_name, error="Marca não registrada.")

        engine_type = (getattr(brand_info, "engine", None) or "vtex").lower()
        if engine_type != "vtex":
            from services.engines.factory import engine_factory
            real_engine = engine_factory.get_engine(brand_key)
            return await real_engine.search(
                query=query,
                max_results=max_results,
                sort=sort,
                zipcode=zipcode,
                include_shipping=include_shipping,
            )

        domain = brand_info.domain.replace("https://", "").replace("http://", "").strip("/")
        sanitized_query = query.replace("-", " ")
        encoded_query = urllib.parse.quote(sanitized_query)

        # Mapeamento de Sort para VTEX Catalog System
        sort_map = {
            "price_asc": "OrderByPriceASC",
            "price_desc": "OrderByPriceDESC",
            "top_selling": "OrderByTopSaleDESC",
            "relevance": "OrderByScoreDESC",
            "recent": "OrderByReleaseDateDESC"
        }
        vtex_sort = sort_map.get(sort, "OrderByScoreDESC")

        chunk_size = 10
        # Numeric searches retain the small bounded fetch.  For "Tudo", the
        # upstream's empty page is authoritative; pagination also stops when a
        # page contains no new product identity.
        max_pages = 5 if max_results is not None else None

        category_path = resolve_query_to_vtex_category_path(query, brand_key)

        def _build_url(current_path: Optional[str], _from: int, _to: int) -> str:
            if current_path:
                if current_path.startswith("C:/") or current_path.startswith("B:"):
                    return f"https://{domain}/api/catalog_system/pub/products/search?fq={current_path}&_from={_from}&_to={_to}&O={vtex_sort}"
                path_str = current_path if current_path.startswith("/") else f"/{current_path}"
                if "?" in path_str:
                    base_p, query_p = path_str.split("?", 1)
                    return f"https://{domain}/api/catalog_system/pub/products/search{base_p}?{query_p}&_from={_from}&_to={_to}&O={vtex_sort}"
                return f"https://{domain}/api/catalog_system/pub/products/search{path_str}?_from={_from}&_to={_to}&O={vtex_sort}"
            return f"https://{domain}/api/catalog_system/pub/products/search/{encoded_query}?_from={_from}&_to={_to}&O={vtex_sort}"

        # SR-01: fetch up to max_raw raw candidates so that filter_mens_fashion
        # and validate_single have room to discard items without the final
        # result set falling below max_results.
        max_raw = max_results * 2 if max_results is not None else None

        async def _run_paging(current_path: Optional[str]):
            """Pagina o endpoint VTEX para um dado path (ou full-text se None). Retorna (produtos, erro)."""
            products = []
            shipping_tasks = []
            last_error = None
            page = 0
            seen_product_ids = set()
            while max_pages is None or page < max_pages:
                if max_raw is not None and len(products) >= max_raw:
                    break

                _from = page * chunk_size
                _to = _from + chunk_size - 1
                url = _build_url(current_path, _from, _to)

                try:
                    raw_products = await self._request_json(url)
                    if not raw_products or not isinstance(raw_products, list):
                        break
                    new_page_items = 0

                    valid_raw = raw_products

                    # Reviews em lote
                    pids = [str(p.get("productId")) for p in valid_raw if p.get("productId")]
                    reviews_dict = await get_bulk_reviews(brand_key, pids)

                    for p in valid_raw:
                        pid = str(p.get("productId"))
                        if pid and pid in seen_product_ids:
                            continue
                        if pid:
                            seen_product_ids.add(pid)
                        new_page_items += 1
                        rating, count = reviews_dict.get(pid, (None, None))

                        # Converte para o modelo de busca
                        items = p.get("items", [])

                        # Seleciona (sku_id, seller_id) do primeiro item com oferta disponível (D-01)
                        candidate = select_candidate(items)
                        if candidate:
                            sku_id, seller_id = candidate
                        else:
                            sku_id = items[0].get("itemId") if items else None
                            seller_id = "1"  # fallback para catálogos first-party legados

                        price_full = 0.0
                        price_discount = None
                        available = False

                        for item in items:
                            for seller in item.get("sellers", []):
                                offer = seller.get("commertialOffer", {})
                                if offer.get("AvailableQuantity", 0) > 0:
                                    available = True
                                    if price_full == 0.0:
                                        price_full = offer.get("Price", 0.0)
                                        lp = offer.get("ListPrice", 0.0)
                                        if lp > price_full:
                                            price_discount = lp - price_full
                                    break

                        # Fallback: Se ainda estiver zerado (esgotado), tenta pegar o preço base do primeiro SKU
                        if price_full == 0.0 and items:
                            try:
                                first_item = items[0]
                                first_seller = first_item.get("sellers", [{}])[0]
                                offer = first_seller.get("commertialOffer", {})
                                price_full = offer.get("Price", 0.0)
                                lp = offer.get("ListPrice", 0.0)
                                if lp > price_full:
                                    price_discount = lp - price_full
                            except Exception as e:
                                logger.debug("Fallback de preço do primeiro SKU falhou: %s", e)

                        image_url = items[0].get("images", [{}])[0].get("imageUrl") if items else None

                        prod_result = SearchProductResult(
                            brand=brand_key,
                            product_name=p.get("productName", ""),
                            url=self._sanitize_product_url(p.get("link", ""), domain),
                            price_full=price_full,
                            price_discount=price_discount,
                            price_discount_is_delta=True,

                            image_url=image_url,
                            category=p.get("categories", [""])[0].split("/")[-2] if p.get("categories") else None,
                            available=available,
                            # D-03: productId nativo — nunca productReference (campo de
                            # negócio editável, não identidade do produto na API VTEX).
                            product_code=str(p.get("productId")) if p.get("productId") is not None else None,
                            rating=rating,
                            review_count=count,
                            review_product_id=pid,
                            # Expostos para o cálculo de frete sob demanda (Phase 33.x)
                            sku_id=sku_id,
                            seller_id=seller_id,
                            conditional_offers=self._fast_conditional_offers(
                                p,
                                product_id=pid,
                                sku_id=str(sku_id) if sku_id is not None else None,
                                seller_id=str(seller_id) if seller_id is not None else None,
                            ),
                        )

                        if include_shipping and zipcode and sku_id:
                            shipping_tasks.append(
                                self._fetch_shipping(sku_id, seller_id, zipcode, domain, prod_result)
                            )

                        products.append(prod_result)

                        if max_raw is not None and len(products) >= max_raw:
                            break
                    if new_page_items == 0:
                        break
                    page += 1

                except Exception as e:
                    last_error = str(e)
                    break

            if shipping_tasks:
                await asyncio.gather(*shipping_tasks)

            return products, last_error

        # WR-02: reusa a sessão já injetada/pooled (com headers/identidade
        # corretos) em vez de abrir incondicionalmente uma sessão nova
        # sem headers — e nunca deixa self.session apontando para uma sessão
        # fechada ao final. Só abre (e fecha) uma sessão temporária quando
        # nenhuma sessão foi injetada.
        temp_session: Optional[aiohttp.ClientSession] = None
        if self.session is None:
            temp_session = aiohttp.ClientSession(
                headers=self._get_headers(), timeout=aiohttp.ClientTimeout(total=15)
            )
            self.session = temp_session

        try:
            products, last_error = await _run_paging(category_path)

            # Fallback full-text (S3): mapping de categoria stale/inválido (ex: Hering
            # '/bodies/camisa') retorna 0 silenciosamente. Se o path mapeado não trouxe
            # nada, refaz a busca pelo endpoint full-text /search/{query}, que funciona.
            if not products and category_path is not None:
                logger.info(
                    f"[{brand_key}] category_path '{category_path}' retornou 0 produtos; "
                    f"fallback full-text para query '{query}'."
                )
                products, last_error = await _run_paging(None)
        finally:
            if temp_session is not None:
                await temp_session.close()
                self.session = None

        if last_error and not products:
            return BrandSearchResult(brand_key=brand_key, brand_name=brand_name, error=last_error)

        from services.engines.base_engine import BaseEngine
        # SR-01: filter then truncate — fetch multiplier guarantees we have
        # enough raw candidates to fill max_results after Quality Gates.
        filtered_products = BaseEngine.filter_mens_fashion(products)
        if max_results is not None:
            filtered_products = filtered_products[:max_results]

        return BrandSearchResult(
            brand_key=brand_key,
            brand_name=brand_name,
            products=filtered_products,
            total_found=len(filtered_products)
        )

