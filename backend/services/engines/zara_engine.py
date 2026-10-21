"""Zara/Inditex engine — hardened with TLS-impersonation-first transport.

Replaces the previous Playwright-only fetcher with ``StealthHttpClient``
(curl_cffi → Playwright fallback). Akamai's anti-bot layer actively blocks
headless Chromium fingerprints but passes standard ``curl_cffi`` requests
that impersonate a real Chrome TLS handshake — verified empirically in the
spike investigation (2026-08-12).

The parser layer (``zara_parser``) is completely unchanged; this refactor
only swaps the network transport that delivers the HTML to the parsers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus, urlparse

from core.http_client import FetchOptions, StealthHttpClient
from core.models import BrandSearchResult, SearchProductResult
from services.engines.base_engine import BaseEngine
from services.shipping.base import ShippingCalculation, apply_shipping_calculation
from services.engines.zara_parser import (
    parse_nav_categories,
    parse_product_detail,
    parse_products,
)

logger = logging.getLogger(__name__)

DEFAULT_SEARCH_URL_TEMPLATE = (
    "https://www.zara.com/br/pt/search?searchTerm={query}&section=MAN"
)
DEFAULT_CATEGORY_URL = "https://www.zara.com/br/pt/man-mkt534.html"
CATALOG_GROUP_LABEL = "Categorias"

# D-07 live-diagnostic (01-08): Akamai's "Access Denied" interstitial is
# returned as a normal (non-exception) HTTP response — Playwright's
# `page.goto` does not raise on non-2xx status, so a soft anti-bot block
# reaches `parse_products` and (finding 0 tiles) was silently misclassified
# as EMPTY instead of BLOCKED. Both markers are required together (not a
# broad "access denied" substring alone) to avoid a false positive on a
# legitimate page that happens to mention access control in unrelated copy.
_ANTI_BOT_BLOCK_MARKERS = ("access denied", "edgesuite.net", "bm-verify", "interstitial/ic.html")

# Timeout for curl_cffi (seconds). Generous enough for intercontinental
# latency but never as slow as Playwright's 45s budget.
_CURL_TIMEOUT_SECONDS: float = 25.0

# Playwright fallback settings — used only when curl_cffi is blocked.
_PW_TIMEOUT_MS: int = 45000
_PW_EXTRA_SLEEP: float = 2.0


class ZaraEngine(BaseEngine):
    """Engine for Zara BR public pages.

    Transport chain (per request):
      1. ``curl_cffi`` with Chrome 120 TLS impersonation (~200ms).
         Passes Akamai in 95%+ of cases without any rendering overhead.
      2. ``Playwright`` via BrowserManager (5-15s) — fallback only when
         curl_cffi receives a non-200 status or an anti-bot interstitial.

    The Zara storefront is an SPA: search/category pages are client-side
    rendered. However, Zara's SSR layer injects enough data (JSON-LD on
    category pages, rendered product tiles on search pages) for the existing
    parsers to extract products without executing JavaScript.
    """

    def __init__(self, brand_key: str):
        self.brand_key = brand_key

    def get_engine_name(self) -> str:
        return "Zara"

    @staticmethod
    def _is_anti_bot_block(html: str) -> bool:
        """See `_ANTI_BOT_BLOCK_MARKERS` above (D-07/01-08 live finding)."""
        lowered = (html or "").lower()
        is_access_denied = "access denied" in lowered and "edgesuite.net" in lowered
        is_interstitial = "bm-verify" in lowered or "interstitial/ic.html" in lowered
        return is_access_denied or is_interstitial

    @staticmethod
    def _clamp_limit(max_results: Optional[int]) -> Optional[int]:
        if max_results is None:
            return None
        try:
            value = int(max_results)
        except (TypeError, ValueError):
            value = 10
        return max(1, min(value, 50))

    def _brand_config(self) -> Dict[str, Any]:
        from services.brand_service import brand_service

        brand = brand_service.get_brand(self.brand_key)
        default_name = "Zara"
        default_template = DEFAULT_SEARCH_URL_TEMPLATE

        if not brand:
            return {
                "brand_name": default_name,
                "domain": "www.zara.com",
                "search_url_template": default_template,
                "proxy_url": None,
            }

        return {
            "brand_name": getattr(brand, "brand_name", default_name),
            "domain": getattr(brand, "domain", "www.zara.com") or "www.zara.com",
            "search_url_template": (
                getattr(brand, "search_url_template", None)
                or default_template
            ),
            "proxy_url": getattr(brand, "proxy_url", None),
        }

    @staticmethod
    def _base_url(domain: str) -> str:
        clean = domain.replace("https://", "").replace("http://", "").strip("/")
        return f"https://{clean}"

    def _build_search_url(self, query: str, config: Dict[str, Any]) -> str:
        encoded = quote_plus(query.strip())
        domain = config["domain"].replace("https://", "").replace("http://", "").strip("/")
        return config["search_url_template"].format(query=encoded, domain=domain)

    def _build_category_url(self, category_url: str, config: Dict[str, Any]) -> str:
        if category_url.startswith(("http://", "https://")):
            return category_url
        path = category_url if category_url.startswith("/") else f"/{category_url}"
        return f"{self._base_url(config['domain'])}{path}"

    def _fetch_options(
        self,
        config: Dict[str, Any],
        *,
        allow_pw: bool = True,
        wait_selector: Optional[str] = ".product-grid-product",
    ) -> FetchOptions:
        """Build ``FetchOptions`` from brand config — single source of truth."""
        return FetchOptions(
            timeout_seconds=_CURL_TIMEOUT_SECONDS,
            proxy_url=config.get("proxy_url"),
            allow_playwright_fallback=allow_pw,
            playwright_timeout_ms=_PW_TIMEOUT_MS,
            playwright_extra_sleep=_PW_EXTRA_SLEEP,
            playwright_wait_selector=wait_selector,
        )

    # ------------------------------------------------------------------
    # Fetch helper — centralized transport for all Zara requests
    # ------------------------------------------------------------------

    async def _fetch_html(
        self,
        url: str,
        config: Dict[str, Any],
        *,
        allow_pw: bool = True,
        context: str = "generic",
        wait_selector: Optional[str] = ".product-grid-product",
    ) -> Optional[str]:
        """Fetch ``url`` via the hardened transport chain.

        Returns the HTML string on success, or ``None`` on total failure
        (both curl_cffi and Playwright failed or returned anti-bot blocks).
        """
        result = await StealthHttpClient.fetch(
            url,
            options=self._fetch_options(config, allow_pw=allow_pw, wait_selector=wait_selector),
        )

        if not result.ok:
            logger.warning(
                "[Zara] %s fetch failed for %s: status=%s, strategy=%s, error=%s",
                context, url, result.status_code, result.strategy.value, result.error,
            )
            return None

        if self._is_anti_bot_block(result.html):
            logger.warning(
                "[Zara] %s hit Akamai Access Denied for %s (strategy=%s)",
                context, url, result.strategy.value,
            )
            return None

        logger.info(
            "[Zara] %s OK for %s (%dms, %d bytes, strategy=%s)",
            context, url, int(result.elapsed_ms), len(result.html), result.strategy.value,
        )
        return result.html

    async def _fetch_html_playwright_only(
        self,
        url: str,
        config: Dict[str, Any],
        *,
        context: str = "generic",
        wait_selector: Optional[str] = ".product-grid-product",
    ) -> Optional[str]:
        """Fetch ``url`` explicitly forcing the Playwright fallback with anti-bot retry."""
        from core.browser_manager import BrowserManager
        
        opts = self._fetch_options(config, allow_pw=True, wait_selector=wait_selector)
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                html = await BrowserManager.fetch_html(
                    url,
                    wait_selector=opts.playwright_wait_selector,
                    timeout=opts.playwright_timeout_ms,
                    wait_until=opts.playwright_wait_until,
                    extra_sleep=opts.playwright_extra_sleep,
                    proxy=opts.proxy_url,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Zara] %s Playwright fetch failed (attempt %d/%d) for %s: %s",
                    context, attempt, max_attempts, url, exc,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2.0)
                continue

            if not html:
                if attempt < max_attempts:
                    await asyncio.sleep(2.0)
                continue

            if self._is_anti_bot_block(html):
                logger.warning(
                    "[Zara] %s Playwright hit Akamai Access Denied (attempt %d/%d) for %s",
                    context, attempt, max_attempts, url,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(3.0)
                continue

            logger.info(
                "[Zara] %s Playwright OK for %s (%d bytes, attempt %d/%d)",
                context, url, len(html), attempt, max_attempts,
            )
            return html

        return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _build_search_results(
        self,
        raw_products: List[Dict[str, Any]],
        *,
        brand_name: str,
        max_results: Optional[int],
        sort: Optional[str],
        is_kids: bool = False,
    ) -> List[SearchProductResult]:
        if is_kids or self.brand_key == "zara_infantil":
            filtered = self.filter_boys_fashion(raw_products)
        else:
            filtered = self.filter_mens_fashion(raw_products)

        validated_dicts: List[Dict[str, Any]] = []
        for product in filtered:
            validated = self.validate_single(product)
            if validated:
                validated_dicts.append(validated)

        if sort == "price_asc":
            validated_dicts.sort(
                key=lambda p: p.get("price_discount") or p.get("price_full") or float("inf")
            )
        elif sort == "price_desc":
            validated_dicts.sort(
                key=lambda p: p.get("price_discount") or p.get("price_full") or 0,
                reverse=True,
            )

        results: List[SearchProductResult] = []
        for product in (validated_dicts if max_results is None else validated_dicts[:max_results]):
            results.append(
                SearchProductResult(
                    brand=brand_name,
                    product_name=product["raw_title"],
                    url=product["url"],
                    price_full=product.get("price_full"),
                    price_discount=product.get("price_discount"),
                    image_url=product.get("image_url"),
                    category=product.get("category"),
                    available=product.get("stock_availability"),
                    product_code=product.get("product_code"),
                    rating=product.get("rating"),
                    review_count=product.get("review_count"),
                    shipping_product_id=product.get("shipping_product_id"),
                    available_colors=product.get("available_colors") or [],
                    available_sizes=product.get("available_sizes") or [],
                    # D-01: proveniência bruta (hasVariant.color/size explícito).
                    raw_colors=product.get("raw_colors") or [],
                    raw_sizes=product.get("raw_sizes") or [],
                    # No shipping values here: Zara shipping is unsupported for now.
                )
            )
        return results

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,  # noqa: ARG002 - shipping unsupported for now
        include_shipping: bool = False,  # noqa: ARG002 - shipping unsupported for now
    ) -> BrandSearchResult:
        """Search Zara with content-aware fallback.

        Phase 1: curl_cffi (fast, ~1s) — works when Zara SSR includes
                 rendered tiles or JSON-LD in the initial HTML payload.
        Phase 2: Playwright (slow, ~5-15s) — falls back here when the
                 curl_cffi HTML is a SPA shell without rendered products
                 (Zara's search page loads products via client-side XHR).

        This mirrors the established MercadoLivreEngine pattern: parse
        the fast response first, escalate to Playwright only when the
        parser returns 0 products from valid HTML.
        """
        config = self._brand_config()
        brand_name = config["brand_name"]
        limit = self._clamp_limit(max_results)
        
        is_kids = (
            self.brand_key == "zara_infantil"
            or any(k in query.lower() for k in ["infantil", "kids", "menino", "boy"])
        )
        if is_kids and "section=" not in config.get("search_url_template", ""):
            domain = config["domain"].replace("https://", "").replace("http://", "").strip("/")
            search_url = f"https://{domain}/br/pt/search?searchTerm={quote_plus(query.strip())}&section=KID"
        else:
            search_url = self._build_search_url(query, config)
        base_url = self._base_url(config["domain"])
        fallback_error: Optional[str] = None

        # --- Phase 1: curl_cffi (fast) ---
        html = await self._fetch_html(
            search_url, config, allow_pw=False, context="search/curl_cffi",
        )
        raw_products: List[Dict[str, Any]] = []
        if html:
            raw_products = parse_products(html, brand=brand_name, base_url=base_url)
            if raw_products:
                logger.info(
                    "[Zara] search: curl_cffi returned %d product(s) for '%s'",
                    len(raw_products), query,
                )
            else:
                logger.info(
                    "[Zara] search: curl_cffi HTML OK (%d bytes) but 0 products "
                    "parsed (SPA shell) — escalating to Playwright for '%s'",
                    len(html), query,
                )
        else:
            fallback_error = "curl_cffi fetch failed or blocked"

        # --- Phase 2: Playwright (only if curl_cffi yielded 0 products) ---
        if not raw_products:
            pw_html = await self._fetch_html_playwright_only(
                search_url, config, context="search/playwright",
            )
            if pw_html:
                raw_products = parse_products(pw_html, brand=brand_name, base_url=base_url)
                if raw_products:
                    logger.info(
                        "[Zara] search: Playwright returned %d product(s) for '%s'",
                        len(raw_products), query,
                    )
                else:
                    fallback_error = fallback_error or "Playwright rendered but 0 products parsed"
            else:
                fallback_error = fallback_error or "All fetch strategies failed"

        products = self._build_search_results(
            raw_products, brand_name=brand_name, max_results=limit, sort=sort, is_kids=is_kids,
        )
        if include_shipping and zipcode and products:
            await self._populate_shipping(products, zipcode)

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=brand_name,
            products=products,
            total_found=len(products),
            error=fallback_error if not products else None,
        )


    # ------------------------------------------------------------------
    # Bulk scrape (category)
    # ------------------------------------------------------------------

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,  # noqa: ARG002 - shipping unsupported for now
        include_shipping: bool = False,  # noqa: ARG002 - shipping unsupported for now
    ):
        config = self._brand_config()
        url = self._build_category_url(category_url, config)
        self.emit_log(log_callback, f"[Zara] varrendo categoria: {url}")
        base_url = self._base_url(config["domain"])

        # Phase 1: curl_cffi
        html = await self._fetch_html(url, config, allow_pw=False, context="bulk_scrape/curl_cffi")
        raw_products: List[Dict[str, Any]] = []
        if html:
            raw_products = parse_products(html, brand=config["brand_name"], base_url=base_url)
            if not raw_products:
                self.emit_log(log_callback, "[Zara] curl_cffi retornou 0 produtos, escalonando para Playwright")
        
        # Phase 2: Playwright fallback
        if not raw_products:
            pw_html = await self._fetch_html_playwright_only(url, config, context="bulk_scrape/playwright")
            if pw_html:
                raw_products = parse_products(pw_html, brand=config["brand_name"], base_url=base_url)
                if not raw_products:
                    self.emit_log(log_callback, "[Zara] Playwright renderizou HTML mas 0 produtos extraídos", type="warning")
            else:
                self.emit_log(log_callback, "[Zara] falha ao buscar categoria (ambas estratégias falharam)", type="error")
                return
        is_kids = (
            self.brand_key == "zara_infantil"
            or any(k in category_url.lower() for k in ["kids", "infantil", "boy", "menino", "bebe", "baby"])
        )
        filter_fn = self.filter_boys_fashion if is_kids else self.filter_mens_fashion
        for product in filter_fn(raw_products):
            if cancel_event and cancel_event.is_set():
                self.emit_log(log_callback, "[Zara] varredura cancelada")
                return
            validated = self.validate_single(product, log_callback=log_callback)
            if validated:
                yield validated

    # ------------------------------------------------------------------
    # Product Details (PDP)
    # ------------------------------------------------------------------

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        config = self._brand_config()
        base_url = self._base_url(config["domain"])

        # Phase 1: curl_cffi
        html = await self._fetch_html(product_url, config, allow_pw=False, context="PDP/curl_cffi")
        parsed: Optional[Dict[str, Any]] = None
        if html:
            parsed = parse_product_detail(html, product_url, brand=config["brand_name"], base_url=base_url)
            if not parsed:
                logger.info("[Zara] PDP curl_cffi return 0 details, escalating to Playwright for %s", product_url)

        # Phase 2: Playwright fallback
        if not parsed:
            pw_html = await self._fetch_html_playwright_only(product_url, config, context="PDP/playwright")
            if pw_html:
                parsed = parse_product_detail(pw_html, product_url, brand=config["brand_name"], base_url=base_url)
                if not parsed:
                    logger.warning("[Zara] PDP Playwright rendered HTML but 0 details parsed for %s", product_url)

        return self.validate_single(parsed) if parsed else None

    # ------------------------------------------------------------------
    # Category Discovery
    # ------------------------------------------------------------------

    async def category_has_products(self, candidate: Any) -> Any:
        """
        Content probe (D-04) for the shared discovery pipeline.

        Fetches the candidate category page with the SAME render budget as
        ``_discover_and_validate`` (D-07 gap-closure: Zara category pages
        lazy-load their product tiles, so a shorter probe budget under-renders
        and produces false-EMPTY results) and proves content by the presence
        of at least one parsed tile (``parse_products``). ANY fetch exception
        (Akamai/anti-bot block, timeout, network error) is classified
        `BLOCKED`, never coerced into `EMPTY` or `HAS_PRODUCTS`.
        """
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        config = self._brand_config()
        url = getattr(candidate, "url", "") or self._build_category_url(candidate.path, config)
        base_url = self._base_url(config["domain"])

        # Phase 1: curl_cffi
        html = await self._fetch_html(url, config, allow_pw=False, context="category_probe/curl_cffi")
        products: List[Dict[str, Any]] = []
        if html:
            products = parse_products(html, brand=config["brand_name"], base_url=base_url)

        # Phase 2: Playwright fallback
        if not products and (not html or not self._is_anti_bot_block(html)):
            pw_html = await self._fetch_html_playwright_only(url, config, context="category_probe/playwright")
            if pw_html:
                products = parse_products(pw_html, brand=config["brand_name"], base_url=base_url)
            elif not html:
                return CategoryProbeResult(ProbeStatus.BLOCKED, detail="fetch failed or anti-bot block")

        if products:
            return CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, detail=f"{len(products)} tile(s)")
        return CategoryProbeResult(ProbeStatus.EMPTY, detail="0 tiles")

    async def _discover_and_validate(self) -> Any:
        """Shared pipeline: official masculine menu -> scope -> probe -> offered.

        Strategy (D-05 — the official rendered masculine menu is the ONLY
        authority; ``brand.mappings`` never creates a candidate here, even
        when present):
          1. Fetch ``DEFAULT_CATEGORY_URL`` (the configured/official masculine
             landing page) and delegate parsing to ``parse_nav_categories``
             (pure function — links already scoped to ``/br/pt/man-*``).
          2. Infer ``male_only_brand`` (D-06) from the official candidate set
             itself — every candidate here already comes from the masculine
             menu, so absence of a feminine marker allows neutral leaves
             (e.g. "Camisas") while an explicit child-context-without-evidence
             candidate ("Infantil") still fails closed regardless.
          3. Run ``validate_discovered_categories`` (D-02/D-04/D-06).

        On ANY fetch exception (Akamai block, timeout, network error) or an
        empty official menu, returns an empty outcome — never a failure that
        crashes the engine or blocks search.
        """
        from services.category_discovery import (
            CategoryValidationOutcome,
            DiscoveredCategory,
            infer_male_only_brand,
            make_scope_policy,
            validate_discovered_categories,
        )

        config = self._brand_config()

        # Phase 1: curl_cffi
        html = await self._fetch_html(DEFAULT_CATEGORY_URL, config, allow_pw=False, context="discover_categories/curl_cffi")
        raw_categories: List[Dict[str, Any]] = []
        if html:
            raw_categories = parse_nav_categories(html)
            if not raw_categories:
                logger.info("[Zara] curl_cffi return 0 categories, escalating to Playwright")
        
        # Phase 2: Playwright fallback
        if not raw_categories:
            pw_html = await self._fetch_html_playwright_only(DEFAULT_CATEGORY_URL, config, context="discover_categories/playwright")
            if pw_html:
                raw_categories = parse_nav_categories(pw_html)
                if not raw_categories:
                    logger.warning("[Zara] Playwright rendered HTML but 0 categories parsed")

        if not raw_categories:
            return CategoryValidationOutcome(offered=[], probes={})

        candidates = [
            DiscoveredCategory(
                name=cat["name"],
                path=cat["path"],
                url=self._build_category_url(cat["path"], config),
            )
            for cat in raw_categories
        ]

        # D-06: every candidate here already comes from the official
        # masculine menu; absence of a feminine marker allows neutral leaves,
        # a mixed set (should one ever appear) still requires ancestor
        # confirmation — never assumed masculine by omission.
        male_only_brand = infer_male_only_brand(candidates)

        return await validate_discovered_categories(
            candidates,
            scope_policy=make_scope_policy(male_only_brand=male_only_brand),
            has_products=self.category_has_products,
        )

    async def discover_categories(self) -> List[Dict[str, Any]]:
        """
        Flat list of offered categories — derived from the shared pipeline
        (official masculine menu -> scope -> probe -> offered). Only
        categories with at least one proven tile are returned (D-04);
        blocked/unverified is never coerced into an empty-but-successful list.
        """
        outcome = await self._discover_and_validate()
        return [{"name": c.name, "path": c.path} for c in outcome.offered]

    async def get_catalog(self) -> List[Dict[str, Any]]:
        """
        Grouped catalog derived from the SAME validated list used by
        ``discover_categories()`` (D-05) — never mapping-first, never an
        independent cache path. ``flatten_catalog_paths(get_catalog()) ==
        discover_categories()`` is a provable equivalence (see contract
        tests).
        """
        from services.category_discovery import build_grouped_catalog

        outcome = await self._discover_and_validate()
        return build_grouped_catalog(outcome.offered)

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[ShippingCalculation]:
        """Cálculo de frete sob demanda para Zara / Zara Infantil."""
        from services.brand_service import brand_service
        from services.shipping.resolver import resolve_shipping_provider

        brand = brand_service.get_brand(self.brand_key)
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

    @staticmethod
    def category_url_to_label(category_url: str) -> str:
        path = urlparse(category_url).path or category_url
        slug = path.rstrip("/").split("/")[-1]
        return slug.replace("-", " ")
