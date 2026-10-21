"""
SFCC (Salesforce Commerce Cloud / Demandware) engine.

Extracts product catalog and price data from SFCC storefronts via
browser-rendered HTML (Playwright / BrowserManager) and curl_cffi.
Targets the BR storefronts `lacoste.com.br` and `hugoboss.com.br` (D-01).

Design decisions from Phase 31 CONTEXT.md:
  - D-03: search renders the native search page then enriches via PDP.
  - D-04: implements every abstract method in BaseEngine (no TypeError).
  - D-07: ALL results up to max_results are PDP-enriched (price + image).
  - D-08: max_results=10 default; Semaphore(3) throttles concurrent fetches.
  - D-09: calculate_shipping returns None — public path, no checkout.
  - D-06: discover_categories() and get_catalog() run the shared
          official -> scope -> probe -> offered pipeline
          (services.category_discovery) — nav renderizado oficial é a
          única autoridade; probe prova conteúdo real via tile parseado
          (Wave 2, Plan 06).
  - Akamai Bypass (Phase 15): uses StealthHttpClient (curl_cffi) as the primary 
          transport to bypass Akamai blocks natively on Lacoste, falling back to 
          Playwright for JS-heavy pages (Hugo Boss).

Security (threat model):
  - T-31-03 (open-redirect): query is URL-encoded; URL stays on brand domain.
  - T-31-04 (DoS / partial HTML): per-PDP try/except swallows failures;
            Semaphore(3) limits concurrent browser hits; validate_single
            rejects products missing url/raw_title/price_full/image_url.
  - T-31-05 (factory elevation): handled in factory.py guard split.
  - T-31-06 (false Frete Grátis): is_free_shipping / shipping_price never set.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus

from core.http_client import FetchOptions, StealthHttpClient
from core.models import BrandSearchResult, SearchProductResult
from services.engines.base_engine import BaseEngine
from services.engines.sfcc_parser import (
    extract_nav_categories,
    parse_pdp,
    parse_search_results,
    parse_search_tiles,
    _absolutize,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (D-08 / CRQ-3)
# ---------------------------------------------------------------------------

CATALOG_GROUP_LABEL: str = "Coleções / Categorias"
"""Frontend group label for SFCC categories — mirrors ShopifyEngine.get_catalog."""

DEFAULT_MAX_RESULTS: int = 10
"""Default number of PDPs to enrich per search.  Each PDP = one browser
navigation, so keep this modest to limit latency and anti-bot exposure."""

PDP_CONCURRENCY: int = 3
"""Maximum concurrent PDP browser sessions.  BrowserManager launches a fresh
Chromium per call; 3 concurrent sessions is a conservative RAM-safe limit."""

# T-31-03: search URL template kept as a module constant so a live smoke can
# update the pattern without touching call sites.  The query placeholder is
# filled with URL-encoded text at call time.
SEARCH_URL_TEMPLATE: str = "https://www.{domain}/search?q={query}"
"""SFCC BR search URL pattern.  Pitfall 5 notes this is unverified for the
BR stores; built as a constant/helper so the live smoke can correct it."""

_WWW_PREFIX_RE = re.compile(r"^www\.", re.IGNORECASE)


def _strip_www(domain: str) -> str:
    """Strip a leading ``www.`` from a stored domain.

    The SFCC URL builders (search/home) prepend ``www.`` themselves, so a
    domain stored as ``www.lacoste.com.br`` would otherwise yield a broken
    ``https://www.www.lacoste.com.br/...`` (404).  Normalizing to the bare
    apex here makes the engine tolerant of either stored form
    (Phase 31 REVIEW HIGH — double-www).
    """
    return _WWW_PREFIX_RE.sub("", domain.strip())


# ---------------------------------------------------------------------------
# SFCCEngine
# ---------------------------------------------------------------------------

class SFCCEngine(BaseEngine):
    """
    Engine for SFCC (Demandware) storefronts via public browser rendering.

    Mirrors ShopifyEngine structurally:
      - thin __init__ storing brand_key
      - search() renders the native search page then enriches each PDP
      - calculate_shipping() returns None (D-09)
      - discover_categories() / get_catalog() return [] stubs (D-06)

    All parsing logic stays in sfcc_parser.py (Clean Code / thin engine).
    """

    def __init__(self, brand_key: str) -> None:
        self.brand_key = brand_key

    # ------------------------------------------------------------------
    # BaseEngine contract — identity
    # ------------------------------------------------------------------

    def get_engine_name(self) -> str:
        """Return the friendly engine name."""
        return "SFCC"

    # ------------------------------------------------------------------
    # Stealth HTTP Client integration
    # ------------------------------------------------------------------

    @staticmethod
    def _is_anti_bot_block(html: str) -> bool:
        lowered = (html or "").lower()
        is_access_denied = "access denied" in lowered and "edgesuite.net" in lowered
        is_interstitial = "bm-verify" in lowered or "interstitial/ic.html" in lowered
        return is_access_denied or is_interstitial

    def _fetch_options(self, proxy_url: Optional[str], *, allow_pw: bool = True) -> FetchOptions:
        return FetchOptions(
            timeout_seconds=25.0,
            proxy_url=proxy_url,
            allow_playwright_fallback=allow_pw,
            playwright_timeout_ms=45000,
            playwright_extra_sleep=2.0,
        )

    async def _fetch_html(
        self,
        url: str,
        proxy_url: Optional[str],
        *,
        allow_pw: bool = True,
        context: str = "generic",
    ) -> Optional[str]:
        result = await StealthHttpClient.fetch(
            url,
            options=self._fetch_options(proxy_url, allow_pw=allow_pw),
        )

        if not result.ok:
            logger.warning(
                "[SFCC] %s fetch failed for %s: status=%s, strategy=%s, error=%s",
                context, url, result.status_code, result.strategy.value, result.error,
            )
            return None

        if self._is_anti_bot_block(result.html):
            logger.warning(
                "[SFCC] %s hit Akamai Access Denied for %s (strategy=%s)",
                context, url, result.strategy.value,
            )
            return None

        logger.info(
            "[SFCC] %s OK for %s (%dms, %d bytes, strategy=%s)",
            context, url, int(result.elapsed_ms), len(result.html), result.strategy.value,
        )
        return result.html

    async def _fetch_html_playwright_only(
        self,
        url: str,
        proxy_url: Optional[str],
        *,
        wait_selector: Optional[str] = None,
        context: str = "generic",
    ) -> Optional[str]:
        from core.browser_manager import BrowserManager
        
        opts = self._fetch_options(proxy_url, allow_pw=True)
        try:
            html = await BrowserManager.fetch_html(
                url,
                wait_selector=wait_selector,
                timeout=opts.playwright_timeout_ms,
                wait_until="domcontentloaded",
                extra_sleep=opts.playwright_extra_sleep,
                proxy=opts.proxy_url,
            )
        except Exception as exc:
            logger.warning("[SFCC] %s Playwright fetch failed for %s: %s", context, url, exc)
            return None

        if not html:
            return None

        if self._is_anti_bot_block(html):
            logger.warning("[SFCC] %s Playwright hit Akamai Access Denied for %s", context, url)
            return None

        logger.info(
            "[SFCC] %s Playwright OK for %s (%d bytes)",
            context, url, len(html),
        )
        return html


    # ------------------------------------------------------------------
    # BaseEngine contract — search (SC-1, SC-3)
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        max_results: Optional[int] = DEFAULT_MAX_RESULTS,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ) -> BrandSearchResult:
        """
        Render the store's native search page then enrich each PDP.

        Steps:
          1. Resolve brand domain from brand_service.
          2. Build the search URL (T-31-03: URL-encode query, same-domain).
          3. `await self._fetch_html(search_url)` using StealthHttpClient.
          4. `parse_search_results(html, domain)` → candidate PDP URLs.
          5. `_enrich_results(urls, max_results, brand_name)` →
             validated SearchProductResult list.
          6. Return BrandSearchResult.
        """
        from services.brand_service import brand_service  # lazy — avoid circular import

        brand = brand_service.get_brand(self.brand_key)

        # Resolve domain — prefer registered brand, fall back to {brand_key}.com.br
        # (BR storefront convention for Lacoste/HugoBoss: lacoste.com.br, hugoboss.com.br)
        domain: str = ""
        brand_name: str = self.brand_key
        if brand:
            domain = getattr(brand, "domain", None) or (
                brand.get("domain", "") if isinstance(brand, dict) else ""
            )
            brand_name = getattr(brand, "brand_name", None) or (
                brand.get("brand_name", self.brand_key) if isinstance(brand, dict) else self.brand_key
            )

        # T-31-03: If no domain from brand registry, derive from brand_key as BR domain
        # This enables smoke-testing before the brand is formally onboarded.
        if not domain:
            domain = f"{self.brand_key}.com.br"
            logger.info(
                "[SFCC] no registered domain for brand_key=%s; using fallback domain=%s",
                self.brand_key,
                domain,
            )

        # Phase 31 REVIEW (double-www): the template below prepends www., so
        # normalize a stored www.-prefixed domain to the bare apex first.
        domain = _strip_www(domain)

        # Per-brand overrides (D-09/D-10 — isolados por marca, não mudam o global):
        #   search_url_template — SFCC Lacoste usa o host canônico www.lacoste.com/br/;
        #     o lacoste.com.br redireciona à home e descarta o ?q= (spike 009).
        #   proxy_url — egress de IP limpo (Lacoste/Akamai bloqueia IP datacenter/corporativo);
        #     None = conexão direta (dev / redes limpas).
        search_url_template: Optional[str] = None
        proxy_url: Optional[str] = None
        if brand:
            search_url_template = getattr(brand, "search_url_template", None) or (
                brand.get("search_url_template") if isinstance(brand, dict) else None
            )
            proxy_url = getattr(brand, "proxy_url", None) or (
                brand.get("proxy_url") if isinstance(brand, dict) else None
            )

        # T-31-03: URL-encode the query; navigation stays on the brand domain.
        encoded_query = quote_plus(query.strip())
        if search_url_template:
            search_url = search_url_template.format(query=encoded_query, domain=domain)
        else:
            search_url = SEARCH_URL_TEMPLATE.format(domain=domain, query=encoded_query)
        logger.info(
            "[SFCC] search url: %s (brand=%s, proxy=%s)",
            search_url, self.brand_key, "yes" if proxy_url else "no",
        )

        search_html = await self._fetch_html(search_url, proxy_url, context="search")
        if not search_html:
            return BrandSearchResult(
                brand_key=self.brand_key,
                brand_name=brand_name,
                error=f"Search page fetch failed or blocked",
            )

        # Preferred path: extract products straight from the search-result tiles
        # (title+url+price+image+stock all present in the tile). Fewer browser hits
        # and less anti-bot exposure (D-13) than per-PDP enrichment.
        tile_products = parse_search_tiles(search_html, domain, brand_name)
        if tile_products:
            # SR-01: pass all tiles — _build_results_from_parsed will filter
            # and truncate to max_results internally.
            validated_products = self._build_results_from_parsed(
                tile_products, max_results, brand_name
            )
            logger.info(
                "[SFCC] %d products from search tiles (brand=%s)",
                len(validated_products), self.brand_key,
            )
        else:
            # Fallback (legacy): discover candidate PDP URLs and enrich each.
            candidate_urls = parse_search_results(search_html, domain)
            logger.info(
                "[SFCC] tiles empty; %d candidate PDP URLs for brand=%s",
                len(candidate_urls), self.brand_key,
            )
            # SR-01: enrich max_raw candidates so filter_mens_fashion has
            # room to discard without starving the result set.
            max_raw = self._resolve_max_raw_fetch(max_results) or 1000
            validated_products = await self._enrich_results(candidate_urls, max_raw, max_results, brand_name, proxy_url)

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=brand_name,
            products=validated_products,
            total_found=len(validated_products),
        )

    # ------------------------------------------------------------------
    # PDP enrichment (D-07, D-08 / T-31-04)
    # ------------------------------------------------------------------

    async def _enrich_results(
        self,
        candidate_urls: List[str],
        max_raw: int,
        max_results: int,
        brand_name: str,
        proxy_url: Optional[str] = None,
    ) -> List[SearchProductResult]:
        """
        Enrich up to `max_raw` candidate URLs by fetching each PDP.

        SR-01: callers pass max_raw = max_results * SEARCH_RAW_FETCH_MULTIPLIER
        so that filter_mens_fashion() and validate_single() have room to discard
        without shrinking the final list below max_results.

        Uses asyncio.Semaphore(PDP_CONCURRENCY) to throttle concurrent browser
        sessions (D-08 / T-31-04).  Per-PDP failures are swallowed (logged)
        so a single blocked/partial page does not abort the whole search.

        After parsing, applies:
          - filter_mens_fashion()    — CAT-01 consistency
          - validate_single()        — Quality Gate (RawProductBronze)
          - truncate to max_results  — SR-01 guarantee

        Never sets is_free_shipping or shipping_price (T-31-06).
        """
        sem = asyncio.Semaphore(PDP_CONCURRENCY)
        urls_to_fetch = candidate_urls[:max_raw]

        async def _enrich_one(url: str) -> Optional[Dict[str, Any]]:
            async with sem:
                html = await self._fetch_html(url, proxy_url, context="PDP")
                if not html:
                    return None
                try:
                    return parse_pdp(html, url)
                except Exception as exc:
                    logger.warning("[SFCC] PDP parse failed for %s: %s", url, exc)
                    return None

        raw_results = await asyncio.gather(*[_enrich_one(u) for u in urls_to_fetch])

        # Drop None (failed / empty PDPs), then run the shared CAT-01 filter +
        # Quality Gate + SearchProductResult build (identical to the tiles path).
        parsed: List[Dict[str, Any]] = [r for r in raw_results if r is not None]
        return self._build_results_from_parsed(parsed, max_results, brand_name)

    def _build_results_from_parsed(
        self,
        parsed: List[Dict[str, Any]],
        max_results: int,
        brand_name: str,
    ) -> List[SearchProductResult]:
        """
        Turn already-parsed product dicts (from search tiles OR PDP enrichment)
        into validated SearchProductResult objects, capped at ``max_results``.

        Shared tail of both search paths:
          - filter_mens_fashion()  — CAT-01 consistency (drops feminine/off-category)
          - validate_single()      — Quality Gate (rejects missing url/title/price/image)
          - never sets is_free_shipping / shipping_price (T-31-06)
        """
        filtered = self.filter_mens_fashion(parsed)

        validated: List[SearchProductResult] = []
        for p in filtered:
            if max_results is not None and len(validated) >= max_results:
                break
            validated_dict = self.validate_single(p)
            if validated_dict:
                validated.append(
                    SearchProductResult(
                        brand=brand_name,
                        product_name=validated_dict["raw_title"],
                        url=validated_dict["url"],
                        price_full=validated_dict.get("price_full"),
                        image_url=validated_dict.get("image_url"),
                        available=validated_dict.get("stock_availability"),
                        product_code=validated_dict.get("product_code"),
                        rating=validated_dict.get("rating"),
                        review_count=validated_dict.get("review_count"),
                        # T-31-06: do NOT set is_free_shipping or shipping_price
                    )
                )

        return validated

    # ------------------------------------------------------------------
    # BaseEngine contract — shipping (D-09 / SC-4)
    # ------------------------------------------------------------------

    async def calculate_shipping(
        self, product: Any, zipcode: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return None — SFCC public path has no checkout / shipping calc (D-09).

        Mirrors ShopifyEngine.calculate_shipping.  Returning None means:
          - No "Frete Grátis" badge in the frontend (App.tsx:1348, 1777).
          - No false shipping_price displayed (T-31-06).
        """
        return None

    # ------------------------------------------------------------------
    # BaseEngine contract — bulk scrape
    # ------------------------------------------------------------------

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ):
        """
        Yield products from a category URL (async generator).

        Renders the category page, extracts PDP URLs, enriches each PDP, then
        applies the Quality Gate and yields valid products one by one.
        Mirrors the ShopifyEngine.run_bulk_scrape emit pattern.
        """
        self.emit_log(log_callback, f"[SFCC] bulk scrape start: {category_url}")

        ctx = self._resolve_brand_context()
        proxy_url = ctx["proxy_url"]

        html = await self._fetch_html(category_url, proxy_url, context="bulk_scrape")
        if not html:
            self.emit_log(log_callback, f"[SFCC] category page fetch failed or blocked", type="error")
            return

        # Resolve brand domain for URL normalisation
        domain = ctx["domain"]
        brand_name = ctx["brand_name"]

        # Loop pagination (attempting to follow generic SFCC next page patterns)
        current_url = category_url
        seen_urls = set()
        page_count = 0
        
        from services.engines.sfcc_parser import parse_search_tiles
        from bs4 import BeautifulSoup
        
        while current_url and page_count < 50:
            if cancel_event and cancel_event.is_set():
                self.emit_log(log_callback, "[SFCC] bulk scrape cancelled")
                break
                
            page_count += 1
            if page_count > 1:
                self.emit_log(log_callback, f"[SFCC] Fetching page {page_count}: {current_url}")
                html = await self._fetch_html(current_url, proxy_url, context="bulk_scrape")
                if not html:
                    self.emit_log(log_callback, f"[SFCC] page {page_count} fetch failed", type="error")
                    break

            # 1. Try to extract directly from tiles (fast path, avoids PDP fetching)
            tile_products = parse_search_tiles(html, domain, brand_name)
            
            if tile_products:
                self.emit_log(log_callback, f"[SFCC] {len(tile_products)} products found in tiles (page {page_count})")
                new_count = 0
                for raw in tile_products:
                    url = raw.get("url")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1
                    
                    is_kids = any(k in category_url.lower() for k in ("infantil", "junior", "kids", "menino"))
                    filter_fn = self.filter_boys_fashion if is_kids else self.filter_mens_fashion
                    validated = self.validate_single(raw, log_callback=log_callback)
                    if validated:
                        if filter_fn([validated]):
                            yield validated
                        
                if new_count == 0:
                    self.emit_log(log_callback, f"[SFCC] No new products on page {page_count}, stopping pagination.")
                    break
            else:
                self.emit_log(log_callback, f"[SFCC] No tiles found on page {page_count}. Stopping pagination (hydration/bot block suspected).", type="error")
                break

            # Try to find the 'Next Page' link
            soup = BeautifulSoup(html, "html.parser")
            next_url = None
            # Common SFCC next page link patterns
            next_link = soup.select_one("a.page-next, a.next, li.next a, a[aria-label='Next'], a[rel='next']")
            if next_link and next_link.get("href"):
                from services.engines.sfcc_parser import _absolutize
                next_url = _absolutize(next_link["href"], domain)
            
            # Additional heuristic: if we couldn't find a next link via selector,
            # but URL or configuration indicates page param pagination (?page=X)
            cfg = getattr(self, "brand", {}) if isinstance(getattr(self, "brand", {}), dict) else {}
            if not next_url and ("page=" in category_url or cfg.get("use_page_param_pagination")) and tile_products:
                sep = "&" if "?" in category_url and "page=" not in category_url else "?"
                import re
                base_no_page = re.sub(r'([?&])page=\d+', '', category_url)
                if not base_no_page.endswith("?"):
                    sep = "?" if "?" not in base_no_page else "&"
                next_url = f"{base_no_page}{sep}page={page_count + 1}"
                
            if next_url and next_url != current_url:
                current_url = next_url
            else:
                break

    # ------------------------------------------------------------------
    # BaseEngine contract — single product details
    # ------------------------------------------------------------------

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        """Fetch and parse a single SFCC PDP."""
        ctx = self._resolve_brand_context()
        html = await self._fetch_html(product_url, ctx["proxy_url"], context="PDP")
        if not html:
            return None
        parsed = parse_pdp(html, product_url)
        if not parsed:
            return None
        return self.validate_single(parsed)

    # ------------------------------------------------------------------
    # BaseEngine contract — category discovery (D-02, D-04, D-05, D-06)
    # ------------------------------------------------------------------

    def _resolve_brand_context(self) -> Dict[str, Any]:
        """Resolve domain/brand_name/proxy_url used by category discovery.

        Mirrors the domain-resolution logic already used by ``search()``
        (BR domain fallback + ``_strip_www`` double-www guard), factored out
        so ``category_has_products`` and ``_discover_and_validate`` share a
        single source of truth instead of duplicating it.
        """
        from services.brand_service import brand_service  # lazy — avoid circular import

        brand = brand_service.get_brand(self.brand_key)

        domain: str = ""
        brand_name: str = self.brand_key
        proxy_url: Optional[str] = None
        if brand:
            domain = getattr(brand, "domain", None) or (
                brand.get("domain", "") if isinstance(brand, dict) else ""
            )
            brand_name = getattr(brand, "brand_name", None) or (
                brand.get("brand_name", self.brand_key) if isinstance(brand, dict) else self.brand_key
            )
            proxy_url = getattr(brand, "proxy_url", None) or (
                brand.get("proxy_url") if isinstance(brand, dict) else None
            )

        if not domain:
            domain = f"{self.brand_key}.com.br"
            logger.info(
                "[SFCC] no registered domain for brand_key=%s; using fallback domain=%s",
                self.brand_key,
                domain,
            )

        domain = _strip_www(domain)  # double-www guard (Phase 31 REVIEW)
        return {"domain": domain, "brand_name": brand_name, "proxy_url": proxy_url}

    async def category_has_products(self, candidate: Any) -> Any:
        """
        Content probe (D-04) for the shared discovery pipeline.

        Fetches the candidate category page and proves content by the
        presence of at least one parsed search tile (``parse_search_tiles``
        — the same tile parser used by ``search()``). ANY fetch exception
        (anti-bot block, timeout, network error) is classified `BLOCKED`,
        never coerced into `EMPTY` or `HAS_PRODUCTS` (D-04 fail-closed).
        """
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        ctx = self._resolve_brand_context()
        url = getattr(candidate, "url", "") or f"https://www.{ctx['domain']}{candidate.path}"

        html = await self._fetch_html(url, ctx["proxy_url"], context="category_probe")
        if not html:
            return CategoryProbeResult(ProbeStatus.BLOCKED, detail="fetch failed or blocked")

        tiles = parse_search_tiles(html, ctx["domain"], ctx["brand_name"])
        if tiles:
            return CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, detail=f"{len(tiles)} tile(s)")
        return CategoryProbeResult(ProbeStatus.EMPTY, detail="0 tiles")

    async def _discover_and_validate(self) -> Any:
        """Shared pipeline: official rendered nav -> scope -> probe -> offered.

        Strategy (D-05 — official menu is the only authority):
          1. Resolve brand domain/proxy via ``_resolve_brand_context``.
          2. Fetch the home page with StealthHttpClient (curl_cffi), then
             fallback to Playwright (with wait_selector="nav") if blocked or empty.
          3. Delegate all parsing to ``extract_nav_categories`` (pure function)
             — the ONLY source of candidates; no mapping/cache/fallback ever
             injects a new candidate here.
          4. Infer ``male_only_brand`` (D-06) from the official candidate set
             itself (no persisted config field) — single-gender catalogs
             (no feminine marker anywhere) allow neutral leaves; mixed
             catalogs keep requiring ancestor-confirmed masculine context.
          5. Run ``validate_discovered_categories`` (D-02/D-04/D-06).

        On ANY fetch exception (anti-bot block, timeout, network error) or an
        empty official nav, returns an empty outcome — never a failure that
        crashes the engine or blocks search (D-06 graceful fail-closed).
        """
        from services.category_discovery import (
            CategoryValidationOutcome,
            DiscoveredCategory,
            infer_male_only_brand,
            make_scope_policy,
            validate_discovered_categories,
        )

        ctx = self._resolve_brand_context()
        domain = ctx["domain"]
        home_url = f"https://www.{domain}"

        # Phase 1: curl_cffi fast fetch
        html = await self._fetch_html(
            home_url,
            ctx["proxy_url"],
            allow_pw=False,
            context="discover_categories/curl_cffi"
        )
        raw_categories: List[Dict[str, Any]] = []
        if html:
            raw_categories = extract_nav_categories(html, domain)
        
        # Phase 2: Playwright fallback (needs wait_selector for JS render)
        if not raw_categories:
            pw_html = await self._fetch_html_playwright_only(
                home_url,
                ctx["proxy_url"],
                wait_selector="nav",
                context="discover_categories/playwright"
            )
            if pw_html:
                raw_categories = extract_nav_categories(pw_html, domain)
            
        if not raw_categories:
            logger.warning(
                "[SFCC] discover_categories yielded 0 categories for brand=%s url=%s",
                self.brand_key,
                home_url,
            )
            return CategoryValidationOutcome(offered=[], probes={})

        candidates = [
            DiscoveredCategory(
                name=cat["name"],
                path=cat["path"],
                url=_absolutize(cat["path"], domain),
            )
            for cat in raw_categories
        ]

        # D-06: single-gender catalogs (no feminine marker anywhere in the
        # official nav) allow neutral leaves; mixed catalogs still require an
        # ancestor-confirmed masculine context — never assumed by omission.
        male_only_brand = infer_male_only_brand(candidates)

        return await validate_discovered_categories(
            candidates,
            scope_policy=make_scope_policy(male_only_brand=male_only_brand),
            has_products=self.category_has_products,
        )

    async def discover_categories(self) -> List[Dict[str, Any]]:
        """
        Flat list of offered categories — derived from the shared pipeline
        (official rendered nav -> scope -> probe -> offered). Only categories
        with at least one proven tile are returned (D-04); blocked/unverified
        is never coerced into an empty-but-successful list.
        """
        outcome = await self._discover_and_validate()
        return [{"name": c.name, "path": c.path} for c in outcome.offered]

    async def get_catalog(self) -> List[Dict[str, Any]]:
        """
        Grouped catalog derived from the SAME validated list used by
        ``discover_categories()`` (D-05) — never an independent cache or
        fallback path. ``flatten_catalog_paths(get_catalog()) ==
        discover_categories()`` is a provable equivalence (see contract tests).
        """
        from services.category_discovery import build_grouped_catalog

        outcome = await self._discover_and_validate()
        return build_grouped_catalog(outcome.offered)
