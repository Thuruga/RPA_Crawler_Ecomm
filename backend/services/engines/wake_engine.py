"""
Wake Commerce engine.

Extracts product catalog and price data from Wake Commerce storefronts via
the public GraphQL Storefront API (storefront-api.fbits.net/graphql) using
a TCS-Access-Token per store.  Target: Richards (www.richards.com.br).

Design decisions from Phase 32 CONTEXT.md and confirmed by spike 007:
  - D-05: Auto-extract TCS-Access-Token from the storefront home page via
          regex; cache per instance; support manual override via brand field.
  - D-06: Optional field wake_access_token in DynamicBrandCreate remains
          backward-compatible; committed brand data should use env overrides.
  - D-07: Token not resolved -> raise ValueError (clear diagnostic message);
          captured by factory._search_one as BrandSearchResult.error —
          never 0 products silently.
  - D-08: calculate_shipping -> None (no public checkout);
          discover_categories / get_catalog -> [] (graceful stubs).
  - D-09: Instantiated by EngineFactory.get_engine for engine_type='wake'.
  - D-10: Single GraphQL search query returns title + URL + price directly.
          No PDP enrichment round-trip.
  - D-11: aiohttp via SessionManager only; no browser rendering.

Security (threat model):
  - T-32-01 (open-redirect): allow_redirects=False in aiohttp GET for
             token auto-extraction — same pattern as T-25-01-SR.
  - T-32-02 (tampering / GraphQL injection): query sent as a GraphQL
             variable ($q: String!) — never string-interpolated into the
             query body.
  - T-32-05 (repudiation): ValueError raised on token absence; caught by
             _search_one and surfaced as BrandSearchResult.error.
  - T-32-06 (token leak): cache stored as instance attribute (_token_cache),
             never as a class variable — prevents cross-brand token leakage
             in concurrent asyncio.gather calls.

Spike 007 confirmed (REPORT.md):
  - Endpoint: https://storefront-api.fbits.net/graphql  -> HTTP 200
  - aliasComplete is relative: "produto/camisa-linho-hortencia-196863"
  - prices.price is int/float in reais (e.g. 479), NOT centavos
  - images[].url present in search.products.edges.node
  - available field present and correct
  - Token extracted via storefrontAccessToken regex from home page HTML
"""

from __future__ import annotations

import asyncio
from html import unescape
import json
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp
from bs4 import BeautifulSoup

from core.models import BrandSearchResult, SearchProductResult
from core.session_manager import SessionManager
from services.attribute_parsing import extract_color_fallback, extract_size_fallback
from services.engines.base_engine import BaseEngine
from services.sanitization import sanitize_colors, sanitize_sizes
from services.shipping.base import ShippingCalculation, apply_shipping_calculation
from services.shipping.resolver import resolve_shipping_provider
from services.wake_category_paths import canonicalize_wake_category_source
from services.wake_token import resolve_wake_access_token_override

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (D-10 / T-32-02)
# ---------------------------------------------------------------------------

GRAPHQL_ENDPOINT: str = "https://storefront-api.fbits.net/graphql"
"""Public Wake Commerce GraphQL endpoint — confirmed by spike 007."""

DEFAULT_MAX_RESULTS: int = 10
"""Default number of products returned per search call."""

_TOKEN_RE = re.compile(
    r"""storefrontAccessToken\s*:\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
"""Regex that extracts the storefront access token from the Wake SDK inline
script injected into the store home page HTML.  Confirmed working against
www.richards.com.br by spike 007 (strategy: regex storefrontAccessToken)."""

# ---------------------------------------------------------------------------
# GraphQL query (WakeSearch) — confirmed field set from spike 007
# ---------------------------------------------------------------------------

_WAKE_SEARCH_QUERY: str = """
query WakeSearch($q: String!, $first: Int!) {
  search(query: $q) {
    products(first: $first) {
      edges {
        node {
          productName
          aliasComplete
          productId
          productVariantId
          sku
          prices {
            price
            listPrice
          }
          images {
            url
          }
          available
          attributes {
            name
            value
          }
          informations {
            title
            value
          }
        }
      }
    }
  }
}
""".strip()


# GraphQL query (WakeHotsite) — collection/section listing (e.g. /sale/masculino,
# /camisas). Wake category/sale pages are "hotsites"; the products node is the same
# Product type as `search`, so the node field set mirrors _WAKE_SEARCH_QUERY.
_WAKE_HOTSITE_QUERY: str = """
query WakeHotsite($url: String!, $first: Int!, $after: String) {
  hotsite(url: $url) {
    products(first: $first, after: $after) {
      totalCount
      pageInfo {
        hasNextPage
        endCursor
      }
      edges {
        node {
          productName
          aliasComplete
          productId
          productVariantId
          sku
          prices {
            price
            listPrice
          }
          images {
            url
          }
          available
          attributes {
            name
            value
          }
          informations {
            title
            value
          }
        }
      }
    }
  }
}
""".strip()


_WAKE_PRODUCT_QUERY: str = """
query WakeProductDetails($id: Long!) {
  product(productId: $id) {
    productId
    productName
    aliasComplete
    available
    prices {
      price
      listPrice
    }
    images {
      url
    }
    informations {
      title
      value
    }
    attributes {
      name
      value
    }
    attributeSelections {
      selections {
        name
        values {
          value
          available
          selected
        }
      }
    }
  }
}
""".strip()


# A Aramis opera quatro vitrines Wake sob o mesmo domínio. O seletor de marcas
# fica fora do <nav> principal e, por isso, uma leitura exclusiva da homepage
# nunca alcança Urban, Next ou Outlet. As rotas abaixo são os entrypoints
# oficiais publicados no header da loja; as categorias continuam vindo do nav
# renderizado de cada vitrine (não de uma lista estática de categorias).
_ARAMIS_STOREFRONTS: tuple[tuple[str, str], ...] = (
    ("Aramis", ""),
    ("Urban", "/urban"),
    ("Next", "/infantil-next"),
    ("Outlet", "/outlet"),
)



class WakeEngine(BaseEngine):
    """Engine for Wake Commerce storefronts (e.g. Richards, Shop2gether).

    Uses the public GraphQL Storefront API with a TCS-Access-Token resolved
    per store instance (D-05/D-06).  No browser required — aiohttp only.
    """

    def __init__(self, brand_key: str) -> None:
        self.brand_key = brand_key
        self._token_cache: Optional[str] = None  # per-instance cache (T-32-06 / Armadilha 5)

    # ------------------------------------------------------------------
    # BaseEngine contract: metadata
    # ------------------------------------------------------------------

    def get_engine_name(self) -> str:
        return "Wake"

    # ------------------------------------------------------------------
    # BaseEngine contract: search (D-10 / D-11)
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        max_results: Optional[int] = DEFAULT_MAX_RESULTS,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ) -> BrandSearchResult:
        """Search Wake Commerce via GraphQL and return a BrandSearchResult.

        Steps:
          1. Resolve brand data (domain, brand_name) from brand_service.
          2. Resolve TCS-Access-Token (override > cache > auto-extract).
          3. POST GraphQL WakeSearch query with variables (T-32-02).
          4. Parse search.products.edges[].node into product dicts.
          5. Apply Quality Gates: filter_mens_fashion -> validate_single.
          6. Return BrandSearchResult.
        """
        from services.brand_service import brand_service  # lazy — avoid circular import

        brand = brand_service.get_brand(self.brand_key)

        # Resolve domain and brand_name with dict/Pydantic compat (PATTERNS §Shared)
        domain: str = ""
        brand_name: str = self.brand_key
        if brand:
            domain = getattr(brand, "domain", None) or (
                brand.get("domain", "") if isinstance(brand, dict) else ""
            )
            brand_name = getattr(brand, "brand_name", None) or (
                brand.get("brand_name", self.brand_key) if isinstance(brand, dict) else self.brand_key
            )

        if not domain:
            # Fallback — enables smoke-testing before brand is formally onboarded.
            # WR-01: use the www. form (not the bare apex) — Wake storefronts commonly
            # 301 apex -> www, which would silently break token auto-extraction with
            # allow_redirects=False. Stored brand domains already include www. (self-consistent;
            # avoids the double-www the GET builder would otherwise produce).
            domain = f"www.{self.brand_key}.com.br"
            logger.info(
                "[Wake] no registered domain for brand_key=%s; using fallback domain=%s",
                self.brand_key,
                domain,
            )

        # Resolve token — D-07: ValueError on failure, never 0-products silently
        token = await self._resolve_token(brand, domain)
        if not token:
            raise ValueError(
                f"Token Wake nao resolvido para '{self.brand_key}'. "
                "Configure wake_access_token na marca ou verifique o storefront."
            )

        # POST GraphQL — T-32-02: variables, not f-string interpolation
        session = await SessionManager.get_session()
        # SR-01 / WR-02: request max_raw = max_results * SEARCH_RAW_FETCH_MULTIPLIER
        # raw candidates from Wake so that filter_mens_fashion() has room to
        # discard off-category items without the result set falling below
        # max_results. The clamp to 50 is unchanged (WR-02: bounded positive int).
        max_raw = self._resolve_max_raw_fetch(max_results)
        if max_raw is None:
            max_raw = 1000
        try:
            first = max(1, min(int(max_raw), 50))
        except (TypeError, ValueError):
            first = DEFAULT_MAX_RESULTS * 2
        payload = {
            "query": _WAKE_SEARCH_QUERY,
            "variables": {
                "q": query.strip(),
                "first": first,
            },
        }
        headers = {"TCS-Access-Token": token}

        try:
            resp = await session.post(
                GRAPHQL_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),  # WR-05: bound a hung storefront
            )
            if True:
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("[Wake] GraphQL request failed for brand=%s: %s", self.brand_key, exc)
            return BrandSearchResult(
                brand_key=self.brand_key,
                brand_name=brand_name,
                error=f"GraphQL request failed: {exc}",
            )

        # GraphQL returns application errors as HTTP 200 with {"errors": [...], "data": null}.
        # raise_for_status() only catches non-2xx, so surface those here — otherwise the null
        # `data` would raise a cryptic AttributeError and bypass the D-07 structured-error path
        # (CR-01 / SC-2).
        gql_errors = data.get("errors")
        payload_data = data.get("data")
        if gql_errors or payload_data is None:
            if isinstance(gql_errors, list) and gql_errors:
                msg = "; ".join(str(e.get("message", e)) for e in gql_errors)
            else:
                msg = "resposta GraphQL sem dados (data=null)"
            logger.warning("[Wake] GraphQL error response for brand=%s: %s", self.brand_key, msg)
            return BrandSearchResult(
                brand_key=self.brand_key,
                brand_name=brand_name,
                error=f"GraphQL error: {msg}",
            )

        # Parse response — spike 007 confirmed the nested path
        search_node = payload_data.get("search") or {}
        products_node = search_node.get("products") or {}
        edges: List[Dict[str, Any]] = products_node.get("edges") or []

        parsed_dicts: List[Dict[str, Any]] = []
        for edge in edges:
            node = edge.get("node", {})
            parsed_node = self._search_node_to_dict(node, domain, brand_name)
            if parsed_node is None:
                logger.debug("[Wake] skipping incomplete node: %s", node)
                continue
            parsed_dicts.append(parsed_node)

        # SR-01: filter then truncate — fetch multiplier guarantees we have
        # enough raw candidates to fill max_results after Quality Gates.
        # Quality Gates order is mandatory: filter_mens_fashion -> validate_single
        filtered = self.filter_mens_fashion(parsed_dicts)
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
                        price_discount=validated_dict.get("price_discount"),
                        price_discount_is_delta=validated_dict.get("price_discount_is_delta", False),
                        image_url=validated_dict.get("image_url"),
                        available=validated_dict.get("stock_availability", True),
                        product_code=validated_dict.get("product_code"),
                        review_product_id=validated_dict.get("product_code"),
                        # Extensões específicas do Wake para simulação de frete
                        shipping_product_id=validated_dict.get("shipping_product_id"),
                        shipping_variant_id=validated_dict.get("shipping_variant_id"),
                        shipping_sku=validated_dict.get("shipping_sku"),
                    )
                )

        if include_shipping and zipcode and validated:
            await self._populate_shipping(validated, zipcode)

        logger.info(
            "[Wake] search brand=%s query=%r -> %d products (after quality gates)",
            self.brand_key,
            query,
            len(validated),
        )

        return BrandSearchResult(
            brand_key=self.brand_key,
            brand_name=brand_name,
            products=validated,
            total_found=len(validated),
        )

    # ------------------------------------------------------------------
    # Token resolution (D-05 / D-06 / D-07)
    # ------------------------------------------------------------------

    async def _resolve_token(self, brand: Any = None, domain: str = "") -> Optional[str]:
        """Resolve TCS-Access-Token with strict precedence (Armadilha 1):

        1. Manual/env override (D-06)
        2. In-memory instance cache (T-32-06 — per-instance, not class-level)
        3. Auto-extraction via GET to the store home page (D-05 / T-32-01)
        4. Return None -> caller raises ValueError (D-07)
        """
        # 1. Manual/env override
        if brand is not None:
            override: Optional[str] = getattr(brand, "wake_access_token", None) or (
                brand.get("wake_access_token") if isinstance(brand, dict) else None
            )
            override = override or resolve_wake_access_token_override(brand)
            if override:
                # WR-03: seed the instance cache so the documented
                # "override > cache > auto-extract" precedence holds consistently
                # and the override path is not re-resolved on every search call.
                self._token_cache = override
                logger.debug("[Wake] using manual token override for brand_key=%s", self.brand_key)
                return override

        # 2. Instance cache (avoids re-fetching home page on every search call)
        if self._token_cache:
            logger.debug("[Wake] using cached token for brand_key=%s", self.brand_key)
            return self._token_cache

        # 3. Auto-extraction from the store home page
        if not domain:
            return None

        store_url = f"https://{domain}"
        logger.info("[Wake] auto-extracting token from %s", store_url)
        try:
            session = await SessionManager.get_session()
            # T-32-01: allow_redirects=False — same pattern as T-25-01-SR in routes_brands.py:44.
            # WR-05: bound a hung storefront with an explicit timeout.
            resp = await session.get(
                store_url,
                allow_redirects=False,
                timeout=5,
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                logger.warning(
                    "[Wake] %s redirected (%s) with redirects disabled; token HTML not read. "
                    "Check the registered domain form (www vs apex).",
                    store_url,
                    resp.status_code,
                )
                return None
            if resp.status_code != 200:
                logger.warning(
                    "[Wake] home GET %s returned %s; skipping token extraction",
                    store_url,
                    resp.status_code,
                )
                return None
            html = resp.text
        except Exception as exc:
            logger.warning("[Wake] token auto-extraction GET failed for %s: %s", store_url, exc)
            return None

        match = _TOKEN_RE.search(html)
        if match:
            token = match.group(1)
            self._token_cache = token  # cache per instance — T-32-06
            logger.info("[Wake] token auto-extracted and cached for brand_key=%s", self.brand_key)
            return token

        logger.warning(
            "[Wake] storefrontAccessToken not found in HTML of %s — "
            "set wake_access_token override on the brand or verify the storefront template.",
            store_url,
        )
        return None

    # ------------------------------------------------------------------
    # BaseEngine contract: stubs (D-08)
    # ------------------------------------------------------------------

    async def calculate_shipping(
        self, product: Any, zipcode: str
    ) -> Optional[ShippingCalculation]:
        from services.brand_service import brand_service

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
        """Categorias oficiais da marca — pipeline compartilhado (D-02/D-04/D-05/D-06).

        Wake não expõe uma árvore JSON navegável como a VTEX; a autoridade
        oficial (D-05) é o menu/nav renderizado pela própria homepage da loja
        (`_fetch_nav_candidates`) — NUNCA `brand.mappings` (de/para estático,
        que o spike GO/NO-GO de `01-03-02` provou estar desatualizado/errado
        para Richards: paths sem o prefixo real `/masculino/`, slug divergente
        `tricot` vs `tricots-e-blusas`). Cada candidato do nav passa por
        escopo (D-02/D-06) e por uma probe real GraphQL `hotsite(url:)`
        (D-04) antes de ser oferecido. Ver
        `backend/tests/fixtures/phase01/wake_category_source.json` para o
        registro do spike que confirmou GO para este mecanismo.
        """
        outcome = await self._discover_and_validate()
        return [{"name": c.name, "path": c.path} for c in outcome.offered]

    async def get_catalog(self) -> List[Dict[str, Any]]:
        """Catálogo agrupado para o dropdown do frontend.

        Deriva da MESMA lista validada usada por `discover_categories`
        (`_discover_and_validate`) — `flatten_catalog_paths(get_catalog())`
        deve ser igual a `discover_categories()`.
        """
        from services.category_discovery import build_grouped_catalog

        outcome = await self._discover_and_validate()
        return build_grouped_catalog(outcome.offered)

    async def _discover_and_validate(self) -> Any:
        """Pipeline compartilhado: nav oficial -> escopo -> probe GraphQL -> oferecidas.

        `male_only_brand` é inferido (D-06) do próprio conjunto de
        candidatos desta chamada (`infer_male_only_brand`): Richards, por
        exemplo, expõe `/masculino/...` e `/feminino/...` no mesmo nav — o
        token `feminino` já presente no path do próprio candidato faz o
        escopo bloquear aquelas folhas independentemente de
        `male_only_brand`; uma futura loja Wake single-gender (sem nenhum
        marcador feminino na árvore) passaria a aceitar folhas neutras.
        """
        from services.brand_service import brand_service  # lazy — circular import
        from services.category_discovery import (
            CategoryValidationOutcome,
            infer_male_only_brand,
            make_scope_policy,
            validate_discovered_categories,
        )

        brand = brand_service.get_brand(self.brand_key)
        domain, _brand_name = self._resolve_domain_and_name(brand)

        candidates = await self._fetch_nav_candidates(domain)
        if not candidates:
            return CategoryValidationOutcome(offered=[], probes={})

        male_only_brand = infer_male_only_brand(candidates)

        return await validate_discovered_categories(
            candidates,
            scope_policy=make_scope_policy(male_only_brand=male_only_brand),
            has_products=self.category_has_products,
        )

    async def _fetch_nav_candidates(self, domain: str) -> List[Any]:
        """Enumera categorias reais a partir do menu server-rendered da loja.

        Na Aramis, lê separadamente as vitrines Aramis, Urban, Next e Outlet,
        pois cada uma publica sua própria raiz de navegação. Nas demais lojas
        Wake, mantém a leitura da homepage. Nunca usa ``brand.mappings`` como
        descoberta: falhas HTTP ou menus incompatíveis retornam uma fotografia
        incompleta, impedindo o refresher de remover dados válidos do banco.
        """
        self._last_category_snapshot_complete = False
        if not domain:
            return []

        store_url = f"https://{domain}"
        storefronts = (
            _ARAMIS_STOREFRONTS
            if self.brand_key.casefold() == "aramis"
            else (("", ""),)
        )
        session = await SessionManager.get_session()
        candidates: List[Any] = []
        fetched_all = True

        for group_name, storefront_path in storefronts:
            page_url = f"{store_url}{storefront_path}"
            try:
                resp = await session.get(
                    page_url,
                    allow_redirects=False,
                    timeout=10,
                )
                if resp.status_code != 200:
                    fetched_all = False
                    logger.warning(
                        "[Wake] nav GET %s returned %s; storefront skipped",
                        page_url,
                        resp.status_code,
                    )
                    continue
                html = resp.text
            except Exception as exc:
                fetched_all = False
                logger.warning("[Wake] nav GET failed for %s: %s", page_url, exc)
                continue

            parsed_candidates = self._parse_nav_candidates(
                html,
                store_url,
                group_override=group_name or None,
                storefront_path=storefront_path or None,
            )
            if not parsed_candidates:
                fetched_all = False
                logger.warning(
                    "[Wake] nav de %s não expôs categorias reconhecíveis; "
                    "fotografia marcada como incompleta",
                    page_url,
                )
                continue
            candidates.extend(parsed_candidates)

        # Consumido pelo refresher para só remover categorias antigas quando a
        # fotografia das quatro vitrines foi obtida por completo.
        self._last_category_snapshot_complete = fetched_all
        return candidates

    @staticmethod
    def _parse_nav_candidates(
        html: str,
        store_url: str,
        *,
        group_override: Optional[str] = None,
        storefront_path: Optional[str] = None,
    ) -> List[Any]:
        import re as _re
        from html import unescape as _unescape

        from services.category_discovery import DiscoveredCategory

        nav_blocks = _re.findall(r"<nav[^>]*>.*?</nav>", html, _re.DOTALL | _re.IGNORECASE)
        if not nav_blocks:
            return []
        # As vitrines da Aramis possuem <nav> aninhados e o menu desktop e o
        # responsivo não trazem sempre o mesmo subconjunto. O regex de blocos
        # não consegue representar corretamente essa estrutura aninhada; em
        # modo agrupado lemos as âncoras do documento inteiro e aplicamos logo
        # abaixo os filtros estritos de profundidade, prefixo e classe do menu.
        # Para as demais Wake mantemos o contrato histórico do maior <nav>.
        nav = html if group_override else max(nav_blocks, key=len)

        anchors = _re.findall(r"<a\b([^>]*)>(.*?)</a>", nav, _re.DOTALL | _re.IGNORECASE)
        candidates: List[Any] = []
        seen_paths: set = set()
        for attrs, inner in anchors:
            href_match = _re.search(r"""href=["']?([^"'\s>]+)""", attrs)
            if not href_match:
                continue
            href = _unescape(href_match.group(1))
            if not href.startswith("/"):
                continue
            path = href.split("?", 1)[0].rstrip("/")
            segments = [s for s in path.split("/") if s]

            if group_override:
                root_segments = [s for s in (storefront_path or "").split("/") if s]
                expected_depth = len(root_segments) + 1
                if len(segments) != expected_depth:
                    continue
                if root_segments and segments[: len(root_segments)] != root_segments:
                    continue

                class_match = _re.search(
                    r'''class=(?:"([^"]+)"|'([^']+)'|([^\s>]+))''',
                    attrs,
                    _re.IGNORECASE,
                )
                class_value = (
                    next(
                        (value for value in class_match.groups() if value),
                        "",
                    )
                    if class_match
                    else ""
                )
                classes = set(class_value.split())
                allowed_classes = {"wf-megamenu__navlink"}
                if group_override == "Outlet":
                    allowed_classes.add("wf-megamenu__link")
                if not classes.intersection(allowed_classes):
                    continue

                text = _re.sub(r"<[^>]+>", " ", inner)
                text = _unescape(_re.sub(r"\s+", " ", text)).strip()
                if not text or text.casefold() == "ver todas" or path in seen_paths:
                    continue

                ancestors = (group_override, "Meninos") if group_override == "Next" else (group_override,)
                seen_paths.add(path)
                candidates.append(
                    DiscoveredCategory(
                        name=text,
                        path=path,
                        url=f"{store_url}{path}",
                        group=group_override,
                        ancestors=ancestors,
                    )
                )
                continue

            gender_segment: Optional[str] = None
            if len(segments) == 2 and segments[0] in ("masculino", "feminino"):
                gender_segment = segments[0]
            elif len(segments) == 2 and segments[0] == "sale" and segments[1] in (
                "masculino",
                "feminino",
            ):
                gender_segment = segments[1]
            elif "data-category-link" in attrs:
                dept_match = _re.search(r"""data-menu-principal=["']?([^"'\s>]+)""", attrs)
                dept_name = dept_match.group(1).strip() if dept_match else "Roupas"
                gender_segment = dept_name
            elif any(s.endswith("-masculina") or s.endswith("-masculino") or "masculin" in s for s in segments):
                gender_segment = "masculino"

            if not gender_segment or path in seen_paths:
                continue

            text = _re.sub(r"<[^>]+>", " ", inner)
            text = _unescape(_re.sub(r"\s+", " ", text)).strip()
            if not text:
                continue

            seen_paths.add(path)
            candidates.append(
                DiscoveredCategory(
                    name=text,
                    path=path,
                    url=f"{store_url}{path}",
                    group=gender_segment.capitalize(),
                    ancestors=(gender_segment.capitalize(),),
                )
            )
        return candidates

    async def category_has_products(self, candidate: Any) -> Any:
        """Probe de conteúdo (D-04) — GraphQL `hotsite(url:)` com `first: 1`.

        Sobrescreve `BaseEngine.category_has_products` (sem default
        permissivo). Classifica `blocked` quando o token não resolve ou a
        requisição falha/retorna erro GraphQL; `empty` quando `hotsite` é
        `null` (path não é uma coleção Wake válida) ou não tem produtos;
        `has_products` quando ao menos 1 edge real é retornado.
        """
        from services.brand_service import brand_service
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        brand = brand_service.get_brand(self.brand_key)
        domain, _brand_name = self._resolve_domain_and_name(brand)
        token = await self._resolve_token(brand, domain)
        if not token:
            return CategoryProbeResult(
                ProbeStatus.BLOCKED, detail="token Wake nao resolvido"
            )
        return await self._probe_hotsite(candidate.path, domain, token)

    async def _probe_hotsite(self, path: str, domain: str, token: str) -> Any:
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        session = await SessionManager.get_session()
        payload = {
            "query": _WAKE_HOTSITE_QUERY,
            "variables": {"url": path, "first": 1},
        }
        try:
            resp = await session.post(
                GRAPHQL_ENDPOINT,
                json=payload,
                headers={"TCS-Access-Token": token},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if True:
                if resp.status_code != 200:
                    return CategoryProbeResult(
                        ProbeStatus.BLOCKED, detail=f"HTTP {resp.status_code}"
                    )
                data = resp.json()
        except Exception as exc:
            return CategoryProbeResult(ProbeStatus.BLOCKED, detail=str(exc))

        if data.get("errors") or data.get("data") is None:
            return CategoryProbeResult(
                ProbeStatus.BLOCKED, detail="GraphQL error/sem dados"
            )

        hotsite = (data.get("data") or {}).get("hotsite")
        if not hotsite:
            return CategoryProbeResult(
                ProbeStatus.EMPTY, detail="hotsite nulo (path nao e colecao valida)"
            )

        edges = ((hotsite.get("products") or {}).get("edges")) or []
        if edges:
            return CategoryProbeResult(
                ProbeStatus.HAS_PRODUCTS, detail=f"{len(edges)} produto(s) amostrados"
            )
        return CategoryProbeResult(ProbeStatus.EMPTY, detail="0 produtos")

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Any = None,
        cancel_event: Any = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False,
    ):
        """Varredura por categoria — hotsite primeiro, busca por termo como fallback.

        As páginas de coleção/seção do Wake (ex.: /sale/masculino, /camisas) são
        "hotsites". `hotsite(url:)` devolve a listagem REAL da coleção que o usuário
        vê no site — inclusive seções como Outlet que não correspondem a nenhum
        termo de busca (o motivo de "Outlet" antes retornar 0 produtos). Quando o
        path não é um hotsite, caímos para um termo de busca derivado do último
        segmento do path via `search(query:)` (spike 007).

        Yields product dicts validados (mesmo contrato de VTEX/Shopify), após os
        Quality Gates (filter_mens_fashion -> validate_single).
        """
        def emit(msg: Any) -> None:
            self.emit_log(log_callback, msg)

        from services.brand_service import brand_service  # lazy — avoid circular import

        brand = brand_service.get_brand(self.brand_key)
        domain, brand_name = self._resolve_domain_and_name(brand)
        token = await self._resolve_token(brand, domain)
        if not token:
            emit(
                {
                    "type": "error_done",
                    "message": (
                        f"[Wake] token não resolvido para '{self.brand_key}'. "
                        "Configure wake_access_token na marca ou verifique o storefront."
                    ),
                }
            )
            return

        # 1. HOTSITE — listagem real da coleção (cobre Outlet/Sale e categorias)
        resolved_category_url = canonicalize_wake_category_source(
            self.brand_key, category_url
        )
        if resolved_category_url != category_url:
            emit(
                f"[Wake] rota legada normalizada: '{category_url}' -> "
                f"'{resolved_category_url}'"
            )

        path = self._hotsite_path(resolved_category_url)
        hotsite_raw = await self._fetch_hotsite_products(path, domain, brand_name, token)
        if hotsite_raw:
            emit(f"[Wake] coleção '{path}' via hotsite: {len(hotsite_raw)} itens brutos")
            for product in self._apply_quality_gates(hotsite_raw, brand_name):
                if cancel_event is not None and cancel_event.is_set():
                    return
                yield product
            return

        # 2. FALLBACK — busca por termo derivado do path
        term = self._category_url_to_search_term(resolved_category_url)
        if not term:
            emit(
                {
                    "type": "brand_warning",
                    "message": (
                        "[Wake] sem hotsite e sem termo de busca derivável da "
                        f"categoria '{category_url}'. Varredura abortada."
                    ),
                }
            )
            return

        # Uma busca textual nao representa uma categoria: e limitada a 50 itens e
        # pode mesclar sessoes/generos. Emitir esse resultado como varredura faz a
        # UI mostrar uma contagem plausivel, mas falsa. Para categoria, a fonte
        # precisa ser o hotsite paginado; callers que desejarem busca usam
        # `search()` explicitamente.
        emit(
            {
                "type": "error_done",
                "message": (
                    f"[Wake] a rota '{path}' nao corresponde a uma colecao "
                    "autoritativa; a busca textual (limitada) foi descartada. "
                    "Atualize a categoria e execute novamente."
                ),
            }
        )
        return

    async def _fetch_hotsite_products(
        self, path: str, domain: str, brand_name: str, token: str
    ) -> Optional[List[Dict[str, Any]]]:
        """POST `hotsite(url:path)` e retorna dicts de produto paginados (pré-gate).
        Usa paginação por cursor (after) para extrair todos os produtos da coleção.
        """
        if not path:
            return None

        session = await SessionManager.get_session()
        raw: List[Dict[str, Any]] = []
        cursor = None
        has_next_page = True
        max_safety_pages = 20

        page_count = 0
        while has_next_page and page_count < max_safety_pages:
            page_count += 1
            variables = {"url": path, "first": 50}
            if cursor:
                variables["after"] = cursor
            payload = {
                "query": _WAKE_HOTSITE_QUERY,
                "variables": variables,
            }
            try:
                resp = await session.post(
                    GRAPHQL_ENDPOINT,
                    json=payload,
                    headers={"TCS-Access-Token": token},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning(
                    "[Wake] hotsite request failed for brand=%s path=%s: %s",
                    self.brand_key, path, exc,
                )
                break

            if data.get("errors") or data.get("data") is None:
                break
            hotsite = (data.get("data") or {}).get("hotsite")
            if not hotsite:
                break

            prods = hotsite.get("products") or {}
            edges = prods.get("edges") or []
            if not edges:
                break

            for edge in edges:
                parsed = self._node_to_dict(edge.get("node", {}), domain, brand_name)
                if parsed:
                    raw.append(parsed)

            page_info = prods.get("pageInfo") or {}
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")
            if not cursor:
                break

        return raw or None

    def _apply_quality_gates(
        self, raw: List[Dict[str, Any]], brand_name: str
    ) -> List[Dict[str, Any]]:
        """Quality Gates (CAT-01 + Pydantic): filter_mens_fashion -> validate_single.
        Preserva composição, cores e tamanhos normalizados.
        """
        out: List[Dict[str, Any]] = []
        for p in self.filter_mens_fashion(raw):
            validated = self.validate_single(p)
            if validated:
                out.append(
                    {
                        "raw_title": validated["raw_title"],
                        "url": validated["url"],
                        "price_full": validated.get("price_full"),
                        "price_discount": validated.get("price_discount"),
                        "image_url": validated.get("image_url"),
                        "brand": brand_name,
                        "raw_description": validated.get("raw_description", ""),
                        "composition": validated.get("composition"),
                        "available_colors": validated.get("available_colors", []),
                        "available_sizes": validated.get("available_sizes", []),
                        "raw_colors": validated.get("raw_colors", []),
                        "raw_sizes": validated.get("raw_sizes", []),
                        "size_grid": validated.get("size_grid", []),
                        "raw_size_grid": validated.get("raw_size_grid", []),
                        "stock_availability": validated.get("stock_availability"),
                        "product_code": validated.get("product_code"),
                    }
                )
        return out

    def _resolve_domain_and_name(self, brand: Any) -> tuple[str, str]:
        """Resolve (domain, brand_name) com compat dict/Pydantic (PATTERNS §Shared)."""
        domain: str = ""
        brand_name: str = self.brand_key
        if brand:
            domain = getattr(brand, "domain", None) or (
                brand.get("domain", "") if isinstance(brand, dict) else ""
            )
            brand_name = getattr(brand, "brand_name", None) or (
                brand.get("brand_name", self.brand_key) if isinstance(brand, dict) else self.brand_key
            )
        if not domain:
            domain = f"www.{self.brand_key}.com.br"
        return domain, brand_name

    @staticmethod
    def _search_node_to_dict(
        node: Dict[str, Any], domain: str, brand_name: str
    ) -> Optional[Dict[str, Any]]:
        """Convert a WakeSearch GraphQL product node into the `search()` parsed-dict shape.

        Mirrors `_node_to_dict` (hotsite/bulk path) plus the three shipping-identity
        fields (`shipping_product_id/variant_id/sku`) that `search()` additionally
        exposes for on-demand shipping simulation. Returns None when the node lacks
        the hard invariants (title/url/price).

        D-03: `product_code` is always `productId` — `productVariantId`/`sku`
        (variant-level identifiers) are preserved ONLY in the shipping_* fields,
        never promoted to product identity.
        """
        product_name = node.get("productName", "")
        alias = node.get("aliasComplete", "")
        # Armadilha 2: aliasComplete is relative (e.g. "produto/camisa-123")
        product_url = f"https://{domain}/{alias.lstrip('/')}" if alias else ""
        # Armadilha 4: prices.price is float/int in reais, NOT centavos (spike confirmed 479 for R$479)
        prices = node.get("prices") or {}
        price_raw = prices.get("price")
        list_price_raw = prices.get("listPrice")
        
        price_discount = float(price_raw) if price_raw is not None else None
        price_full = float(list_price_raw) if list_price_raw is not None else price_discount
        product_id = node.get("productId")
        product_variant_id = node.get("productVariantId")
        sku = node.get("sku")
        # Armadilha 3: images is a list (confirmed in spike 007)
        images = node.get("images") or []
        image_url = images[0].get("url") if images else None
        available = node.get("available", True)

        if not product_name or not product_url or price_full is None:
            return None

        return {
            "raw_title": product_name,
            "url": product_url,
            "price_full": price_full,
            "price_discount": price_discount,
            "price_discount_is_delta": False,
            "image_url": image_url,
            "brand": brand_name,
            "raw_description": "",
            "stock_availability": bool(available),
            "shipping_product_id": str(product_id) if product_id is not None else None,
            "shipping_variant_id": str(product_variant_id) if product_variant_id is not None else None,
            "shipping_sku": str(sku) if sku is not None else None,
            "product_code": str(product_id) if product_id is not None else None,
            "review_product_id": str(product_id) if product_id is not None else None,
        }

    @staticmethod
    def _node_to_dict(
        node: Dict[str, Any], domain: str, brand_name: str
    ) -> Optional[Dict[str, Any]]:
        """Converte um nó GraphQL de produto no dict do orchestrator (ou None se incompleto).

        Mesmas armadilhas tratadas em `search`: aliasComplete relativo (Armadilha 2),
        prices.price em reais (Armadilha 4), images é lista (Armadilha 3).
        Extrai cores, tamanhos e tecido/composição de attributes e informations.
        """
        product_name = node.get("productName", "")
        alias = node.get("aliasComplete", "")
        product_url = f"https://{domain}/{alias.lstrip('/')}" if alias else ""
        prices = node.get("prices") or {}
        price_raw = prices.get("price")
        list_price_raw = prices.get("listPrice")
        
        price_discount = float(price_raw) if price_raw is not None else None
        price_full = float(list_price_raw) if list_price_raw is not None else price_discount
        images = node.get("images") or []
        image_url = images[0].get("url") if images else None
        available = node.get("available", True)
        product_id = node.get("productId")
        if not product_name or not product_url or price_full is None:
            return None

        # Cores, tamanhos e escopo via attributes.  O gênero é um sinal
        # obrigatório para o quality gate: títulos de calças femininas podem
        # ser neutros e a URL da PDP não carrega o departamento.
        attributes = node.get("attributes") or []
        available_colors: List[str] = []
        available_sizes: List[str] = []
        attribute_values: Dict[str, str] = {}
        for attr in attributes:
            name = (attr.get("name") or "").lower()
            val = attr.get("value")
            if not val:
                continue
            normalized_name = re.sub(
                r"[^a-z0-9]+",
                "",
                BaseEngine._strip_accents(str(attr.get("name") or "")).lower(),
            )
            if normalized_name and normalized_name not in attribute_values:
                attribute_values[normalized_name] = str(val).strip()
            if name in ("cor", "cores", "color"):
                if val not in available_colors:
                    available_colors.append(val)
            elif name in ("tamanho", "tamanhos", "size"):
                if val not in available_sizes:
                    available_sizes.append(val)

        # Composição via informations
        informations = node.get("informations") or []
        composition = None
        for info in informations:
            title = (info.get("title") or "").lower()
            val = info.get("value") or ""
            if any(w in title for w in ["composi", "material"]):
                composition = val.replace("<BR>", " ").replace("<br>", " ").strip()
                break
            elif "descri" in title:
                val_clean = re.sub(r"<br\s*/?>", "\n", val, flags=re.I)
                m = re.search(r"Composi[çc][ãa]o(?:[^\n]*)\n+[\s•\-\*]*([^\n]+)", val_clean, re.I)
                if m:
                    composition = m.group(1).strip(" •-\r\t")
                elif not composition:
                    match_percent = re.search(r"(\d+%\s*[A-Za-zÀ-ÿ]+(?:\s*(?:e|\/|,)\s*\d+%\s*[A-Za-zÀ-ÿ]+)?)", val_clean)
                    if match_percent:
                        composition = match_percent.group(1).strip()

        return {
            "raw_title": product_name,
            "url": product_url,
            "price_full": price_full,
            "price_discount": price_discount,
            "price_discount_is_delta": False,
            "image_url": image_url,
            "brand": brand_name,
            "raw_description": "",
            "composition": composition,
            "available_colors": available_colors,
            "available_sizes": available_sizes,
            "raw_colors": list(available_colors),
            "raw_sizes": list(available_sizes),
            "size_grid": list(available_sizes),
            "raw_size_grid": list(available_sizes),
            "stock_availability": bool(available),
            "gender": (
                attribute_values.get("genero")
                or attribute_values.get("generogoogle")
                or attribute_values.get("gender")
            ),
            "category": (
                attribute_values.get("categoria")
                or attribute_values.get("category")
                or attribute_values.get("department")
            ),
            # D-03: productId nativo — nunca o sku de variante (mesma regra da busca).
            "product_code": str(product_id) if product_id is not None else None,
            "review_product_id": str(product_id) if product_id is not None else None,
        }

    @staticmethod
    def _hotsite_path(category_url: str) -> str:
        """Normaliza o path do hotsite (com barra inicial, sem host/query/barra final).

        Ex.: 'https://www.richards.com.br/sale/masculino' -> '/sale/masculino'
             'camisas' -> '/camisas'
        """
        if not category_url:
            return ""
        path = category_url
        if "://" in path:
            path = path.split("://", 1)[1]
            path = path[path.find("/"):] if "/" in path else ""
        path = path.split("?", 1)[0].rstrip("/")
        if path and not path.startswith("/"):
            path = "/" + path
        return path

    @staticmethod
    def _category_url_to_search_term(category_url: str) -> str:
        """Deriva um termo de busca a partir do path/URL da categoria.

        Ex.: 'https://www.richards.com.br/roupas-masculinas/camisas' -> 'camisas'
             '/roupas/polos' -> 'polos'
        Usa o último segmento não-vazio do path, normalizando hífens em espaços.
        """
        if not category_url:
            return ""
        # Remove esquema/host se vier URL completa
        path = category_url
        if "://" in path:
            path = path.split("://", 1)[1]
            path = path[path.find("/"):] if "/" in path else ""
        segments = [s for s in path.split("/") if s]
        if not segments:
            return ""
        last = segments[-1]
        # remove query string e normaliza
        last = last.split("?", 1)[0]
        return last.replace("-", " ").strip()

    @staticmethod
    def _clean_text(val: Any) -> str:
        if not val:
            return ""
        val = re.sub(r"<br\s*/?>", "\n", str(val), flags=re.IGNORECASE)
        val = re.sub(r"<[^>]+>", " ", val)
        val = unescape(val)
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in val.splitlines()]
        return "\n".join(line for line in lines if line)

    @classmethod
    def _parse_wake_pdp_html(cls, html: str, product_url: str) -> Dict[str, Any]:
        """Extrai cores, tamanhos, descrição, composição e especificações da página HTML da Wake."""
        parsed: Dict[str, Any] = {
            "available_colors": [],
            "raw_colors": [],
            "available_sizes": [],
            "raw_sizes": [],
            "size_grid": [],
            "raw_size_grid": [],
            "raw_description": "",
            "composition": None,
            "specifications": {},
            "breadcrumb": [],
            "stock_availability": None,
            "product_name": None,
            "price_full": None,
            "price_discount": None,
            "image_url": None,
            "product_code": None,
        }

        soup = BeautifulSoup(html, "html.parser")

        # 1. Modern Wake: let product = {...} (Oriba, etc.)
        match_prod = re.search(
            r"let\s+product\s*=\s*(\{.*?\});?\s*(?:let|const|var|\$\(|window|document|</script>)",
            html,
            re.DOTALL,
        )
        if not match_prod:
            match_prod = re.search(r"let\s+product\s*=\s*(\{.*?\})\s*</script>", html, re.DOTALL)

        if match_prod:
            prod_str = match_prod.group(1)

            # 1a. Tentar interpretar diretamente como JSON
            prod_obj = None
            try:
                prod_obj = json.loads(prod_str)
            except Exception:
                pass

            if isinstance(prod_obj, dict):
                if "product_id" in prod_obj or "id" in prod_obj:
                    parsed["product_code"] = str(prod_obj.get("product_id") or prod_obj.get("id"))
                if "product_name" in prod_obj or "name" in prod_obj:
                    parsed["product_name"] = unescape(str(prod_obj.get("product_name") or prod_obj.get("name")).strip())
                if "stock" in prod_obj:
                    parsed["stock_availability"] = int(prod_obj["stock"]) > 0
                elif "available" in prod_obj:
                    parsed["stock_availability"] = bool(prod_obj["available"])

                selections = (prod_obj.get("attribute_selections") or {}).get("selections", [])
                for sel in selections:
                    sel_name = (sel.get("name") or sel.get("attribute_name") or "").lower()
                    values = sel.get("values", [])
                    if any(c in sel_name for c in ("cor", "color")):
                        for v in values:
                            val_str = str(v.get("value") or "").strip()
                            is_avail = bool(v.get("available", True))
                            if val_str and val_str not in parsed["raw_colors"]:
                                parsed["raw_colors"].append(val_str)
                            if is_avail and val_str and val_str not in parsed["available_colors"]:
                                parsed["available_colors"].append(val_str)
                    elif any(t in sel_name for t in ("tamanho", "size")):
                        for v in values:
                            val_str = str(v.get("value") or "").strip()
                            is_avail = bool(v.get("available", True))
                            if val_str and val_str not in parsed["raw_size_grid"]:
                                parsed["raw_size_grid"].append(val_str)
                            if is_avail and val_str and val_str not in parsed["available_sizes"]:
                                parsed["available_sizes"].append(val_str)
                        parsed["size_grid"] = list(parsed["raw_size_grid"])
                        parsed["raw_sizes"] = list(parsed["available_sizes"])

                for attr in prod_obj.get("attributes", []):
                    k = str(attr.get("name") or "").strip()
                    v = cls._clean_text(str(attr.get("value") or ""))
                    k_lower = k.lower()
                    if (
                        k_lower in ("versatilidade", "descriçao", "descricao", "description")
                        and not parsed["raw_description"]
                    ):
                        parsed["raw_description"] = v
                    elif (
                        k_lower in ("composicao", "composiçao", "composition")
                        and not parsed["composition"]
                    ):
                        parsed["composition"] = v
                    elif k_lower not in (
                        "cor",
                        "tamanho",
                        "filtrocor",
                        "prevenda",
                        "gender",
                        "age",
                        "look",
                    ):
                        if k and v:
                            parsed["specifications"][k] = v
            else:
                # 1b. Fallback Regex para objeto JS cru/minificado
                pid_m = re.search(r"""["']?(?:product_id|id)["']?\s*:\s*(\d+)""", prod_str)
                if pid_m:
                    parsed["product_code"] = pid_m.group(1)

                pname_m = re.search(r"""["']?(?:product_name|name)["']?\s*:\s*["']([^"']+)["']""", prod_str)
                if pname_m:
                    parsed["product_name"] = unescape(pname_m.group(1).strip())

                stock_m = re.search(r"""["']?stock["']?\s*:\s*(\d+)""", prod_str)
                avail_prod_m = re.search(r"""["']?available["']?\s*:\s*(!0|true|!1|false)""", prod_str)
                if stock_m:
                    parsed["stock_availability"] = int(stock_m.group(1)) > 0
                elif avail_prod_m:
                    parsed["stock_availability"] = avail_prod_m.group(1) in ("!0", "true")

                # Cores a partir de attribute_selections
                cor_block = re.search(
                    r"""["']?(?:name|attribute_name)["']?\s*:\s*["'](?:Cor|Cores|Color)["'][^\]]*?["']?values["']?\s*:\s*(\[.*?\])\s*\}""",
                    prod_str,
                    re.DOTALL | re.IGNORECASE,
                )
                if cor_block:
                    for val_match in re.finditer(r"\{([^}]+)\}", cor_block.group(1)):
                        block = val_match.group(1)
                        val_m = re.search(r"""["']?value["']?\s*:\s*["']([^"']+)["']""", block)
                        avail_m = re.search(r"""["']?available["']?\s*:\s*(!0|true|!1|false)""", block)
                        if val_m:
                            raw_val = val_m.group(1).strip()
                            if raw_val and raw_val not in parsed["raw_colors"]:
                                parsed["raw_colors"].append(raw_val)
                            is_avail = avail_m.group(1) in ("!0", "true") if avail_m else True
                            if is_avail and raw_val and raw_val not in parsed["available_colors"]:
                                parsed["available_colors"].append(raw_val)

                # Tamanhos a partir de attribute_selections
                tam_block = re.search(
                    r"""["']?(?:name|attribute_name)["']?\s*:\s*["'](?:Tamanho|Tamanhos|Size)["'][^\]]*?["']?values["']?\s*:\s*(\[.*?\])\s*\}""",
                    prod_str,
                    re.DOTALL | re.IGNORECASE,
                )
                if tam_block:
                    for val_match in re.finditer(r"\{([^}]+)\}", tam_block.group(1)):
                        block = val_match.group(1)
                        val_m = re.search(r"""["']?value["']?\s*:\s*["']([^"']+)["']""", block)
                        avail_m = re.search(r"""["']?available["']?\s*:\s*(!0|true|!1|false)""", block)
                        if val_m:
                            raw_val = val_m.group(1).strip()
                            if raw_val and raw_val not in parsed["raw_size_grid"]:
                                parsed["raw_size_grid"].append(raw_val)
                            is_avail = avail_m.group(1) in ("!0", "true") if avail_m else True
                            if is_avail and raw_val and raw_val not in parsed["available_sizes"]:
                                parsed["available_sizes"].append(raw_val)
                    parsed["size_grid"] = list(parsed["raw_size_grid"])
                    parsed["raw_sizes"] = list(parsed["available_sizes"])

                # Atributos no objeto let product
                attrs_block = re.search(
                    r"""["']?attributes["']?\s*:\s*(\[.*?\])\s*(?:,|\})""",
                    prod_str,
                    re.DOTALL,
                )
                if attrs_block:
                    for attr_match in re.finditer(r"\{([^}]+)\}", attrs_block.group(1)):
                        block = attr_match.group(1)
                        name_m = re.search(r"""["']?name["']?\s*:\s*["']([^"']+)["']""", block)
                        val_m = re.search(r"""["']?value["']?\s*:\s*["']([^"']+)["']""", block)
                        if name_m and val_m:
                            k = name_m.group(1).strip()
                            v = cls._clean_text(val_m.group(1))
                            k_lower = k.lower()
                            if (
                                k_lower in ("versatilidade", "descriçao", "descricao", "description")
                                and not parsed["raw_description"]
                            ):
                                parsed["raw_description"] = v
                            elif (
                                k_lower in ("composicao", "composiçao", "composition")
                                and not parsed["composition"]
                            ):
                                parsed["composition"] = v
                            elif k_lower not in (
                                "cor",
                                "tamanho",
                                "filtrocor",
                                "prevenda",
                                "gender",
                                "age",
                                "look",
                            ):
                                if k and v:
                                    parsed["specifications"][k] = v

        # 2. data_layer_product_details
        match_dl = re.search(r"data_layer_product_details\s*=\s*(\{.*?\});", html, re.DOTALL)
        if match_dl:
            dl_str = match_dl.group(1)
            desc_m = re.search(r"item_description\s*:\s*[\"']([^\"']+)[\"']", dl_str)
            if desc_m and not parsed["raw_description"]:
                parsed["raw_description"] = cls._clean_text(desc_m.group(1))
            tax_m = re.search(r"taxonomy\s*:\s*\[([^\]]+)\]", dl_str)
            if tax_m and not parsed["breadcrumb"]:
                tax_items = re.findall(r"[\"']([^\"']+)[\"']", tax_m.group(1))
                parsed["breadcrumb"] = [unescape(t.strip()) for t in tax_items if t.strip()]
            price_m = re.search(r"price\s*:\s*([0-9.]+)", dl_str)
            if price_m and parsed["price_full"] is None:
                parsed["price_full"] = float(price_m.group(1))
                parsed["price_discount"] = float(price_m.group(1))
            img_m = re.search(r"image\s*:\s*[\"']([^\"']+)[\"']", dl_str)
            if img_m and not parsed["image_url"]:
                parsed["image_url"] = img_m.group(1).strip()

        # 3. JSON-LD Breadcrumbs & Product
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "{}")
                if data.get("@type") == "BreadcrumbList" and not parsed["breadcrumb"]:
                    items = data.get("itemListElement", [])
                    parsed["breadcrumb"] = [
                        i.get("name")
                        for i in items
                        if i.get("name") and i.get("name").strip().lower() not in ("início", "inicio", "home")
                    ]
                elif data.get("@type") == "Product":
                    if not parsed["product_name"] and data.get("name"):
                        parsed["product_name"] = data["name"]
                    if not parsed["image_url"] and data.get("image"):
                        imgs = data["image"] if isinstance(data["image"], list) else [data["image"]]
                        if imgs:
                            parsed["image_url"] = imgs[0]
                    if not parsed["raw_description"] and data.get("description"):
                        parsed["raw_description"] = cls._clean_text(data["description"])
                    offers = data.get("offers", {})
                    if isinstance(offers, dict) and parsed["price_full"] is None:
                        p = offers.get("price")
                        if p is not None:
                            parsed["price_full"] = float(p)
                            parsed["price_discount"] = float(p)
            except Exception:
                pass

        # 4. Elementos específicos de gaveta (Richards)
        comp_match = re.search(
            r"id=[\"']?drawerProductComposition[\"']?[^>]*>(.*?)</div>", html, re.DOTALL | re.IGNORECASE
        )
        if comp_match and not parsed["composition"]:
            parsed["composition"] = cls._clean_text(comp_match.group(1))

        desc_match = re.search(
            r"id=[\"']?drawerProductInfo[\"']?[^>]*>(.*?)</div>", html, re.DOTALL | re.IGNORECASE
        )
        if desc_match and not parsed["raw_description"]:
            parsed["raw_description"] = cls._clean_text(desc_match.group(1))

        # 5. Accordions / details-pdp (Oriba)
        for det in soup.find_all(
            ["details", "div"],
            class_=re.compile(r"\bdetails-pdp\b|\baccordion\b", re.I),
        ):
            text = det.get_text(separator=" ", strip=True)
            if re.search(r"composi[çc][ãa]o", text, re.I) and not parsed["composition"]:
                comp_clean = re.sub(r"^\s*composi[çc][ãa]o\s*[:-]?\s*", "", text, flags=re.I).strip()
                if comp_clean:
                    parsed["composition"] = cls._clean_text(comp_clean)
            elif re.search(r"descri[çc][ãa]o", text, re.I) and not parsed["raw_description"]:
                desc_clean = re.sub(r"^\s*descri[çc][ãa]o\s*[:-]?\s*", "", text, flags=re.I).strip()
                if desc_clean:
                    parsed["raw_description"] = cls._clean_text(desc_clean)
            elif re.search(r"sustentabilidade", text, re.I):
                sust_clean = re.sub(r"^\s*sustentabilidade\s*[:-]?\s*", "", text, flags=re.I).strip()
                if sust_clean and "Sustentabilidade" not in parsed["specifications"]:
                    parsed["specifications"]["Sustentabilidade"] = cls._clean_text(sust_clean)

        # 6. Fallback Richards para IDs de cor e tamanho (257 = cor, 258 = tamanho)
        if not parsed["available_colors"]:
            html_colors = re.findall(r"""id=["']?257__([^_"'>\s]+)""", html)
            if html_colors:
                parsed["raw_colors"] = list(html_colors)
                parsed["available_colors"] = list(html_colors)

        if not parsed["available_sizes"]:
            html_sizes = re.findall(r"""id=["']?258__([^_"'>\s]+)""", html)
            if html_sizes:
                parsed["raw_sizes"] = list(html_sizes)
                parsed["available_sizes"] = list(html_sizes)
                parsed["raw_size_grid"] = list(html_sizes)
                parsed["size_grid"] = list(html_sizes)

        # 7. Layout Wake/Fbits moderno (wf-buy - ex: Aramis)
        if not parsed["available_colors"]:
            for cor_inp in soup.find_all("input", class_=re.compile(r"\b(?:wf-buy__swatch-input|swatch-input)\b", re.I)):
                cname = cor_inp.get("data-color-name") or cor_inp.get("value") or cor_inp.get("attribute-value")
                if cname:
                    cname = cls._clean_text(str(cname))
                    if cname and cname not in parsed["raw_colors"]:
                        parsed["raw_colors"].append(cname)
                    is_dis = cor_inp.has_attr("disabled") or bool(cor_inp.get("disabled"))
                    if not is_dis and cname and cname not in parsed["available_colors"]:
                        parsed["available_colors"].append(cname)

        if not parsed["available_sizes"]:
            for size_inp in soup.find_all("input", class_=re.compile(r"\b(?:wf-buy__size-input|size-input)\b", re.I)):
                sval = size_inp.get("value") or size_inp.get("attribute-value")
                if sval:
                    sval = cls._clean_text(str(sval))
                    if sval and sval not in parsed["raw_size_grid"]:
                        parsed["raw_size_grid"].append(sval)
                    is_dis = size_inp.has_attr("disabled") or bool(size_inp.get("disabled"))
                    if not is_dis:
                        parent_lbl = size_inp.find_parent("label")
                        if parent_lbl:
                            lbl_cls = " ".join(parent_lbl.get("class") or [])
                            if any(w in lbl_cls.lower() for w in ("disable", "indisp", "esgot", "unavail")):
                                is_dis = True
                    if not is_dis and sval and sval not in parsed["available_sizes"]:
                        parsed["available_sizes"].append(sval)
            if parsed["raw_size_grid"] and not parsed["size_grid"]:
                parsed["size_grid"] = list(parsed["raw_size_grid"])
            if parsed["available_sizes"] and not parsed["raw_sizes"]:
                parsed["raw_sizes"] = list(parsed["available_sizes"])

        # Normalização canônica
        if parsed["available_colors"]:
            canonical = [extract_color_fallback(v) for v in parsed["available_colors"]]
            parsed["available_colors"] = sanitize_colors(canonical)
        if parsed["available_sizes"]:
            canonical = [extract_size_fallback(v) for v in parsed["available_sizes"]]
            parsed["available_sizes"] = sanitize_sizes(canonical)
        if parsed["size_grid"]:
            canonical = [extract_size_fallback(v) for v in parsed["size_grid"]]
            parsed["size_grid"] = sanitize_sizes(canonical)

        return parsed

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        """Busca os detalhes ricos do produto na Wake (GraphQL + PDP HTML)."""
        from services.brand_service import brand_service

        brand = brand_service.get_brand(self.brand_key)
        domain, brand_name = self._resolve_domain_and_name(brand)

        session = await SessionManager.get_session()

        # 1. Ler a página PDP HTML e identificar se houve redirect (ex: URL VTEX legada -> URL Wake)
        pdp_data: Dict[str, Any] = {}
        real_url = product_url
        try:
            resp = await session.get(product_url, timeout=15)
            if resp.status_code == 200:
                if hasattr(resp, "url") and resp.url and str(resp.url) != product_url:
                    real_url = str(resp.url)
                pdp_data = self._parse_wake_pdp_html(resp.text, real_url)
        except Exception as e:
            logger.warning("[Wake] Falha ao ler PDP HTML (%s): %s", product_url, e)

        # 2. Extração do ID e termo a partir da URL final ou PDP data
        clean_url = real_url.split("?")[0].split("#")[0].rstrip("/")
        if clean_url.endswith("/p"):
            clean_url = clean_url[:-2].rstrip("/")

        search_id = ""
        if "-" in clean_url:
            last_part = clean_url.split("-")[-1]
            if last_part.isdigit() and len(last_part) >= 4:
                search_id = last_part
        if not search_id and pdp_data.get("product_code"):
            code_str = str(pdp_data["product_code"]).strip()
            if code_str.isdigit():
                search_id = code_str

        search_term = search_id
        if not search_term:
            slug = clean_url.split("/")[-1]
            if slug and slug != "p":
                search_term = slug.replace("-", " ").strip()

        # 3. Busca GraphQL pelo nó do produto
        gql_node: Dict[str, Any] = {}
        gql_selections: List[Dict[str, Any]] = []

        token = await self._resolve_token(brand, domain)
        if token:
            # 3a. Se temos um ID numérico, consultar diretamente product(productId: $id) com attributeSelections
            if search_id and search_id.isdigit():
                try:
                    payload = {
                        "query": _WAKE_PRODUCT_QUERY,
                        "variables": {"id": int(search_id)},
                    }
                    resp = await session.post(
                        GRAPHQL_ENDPOINT,
                        json=payload,
                        headers={"TCS-Access-Token": token},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status_code == 200:
                        p_data = (resp.json().get("data") or {}).get("product")
                        if p_data:
                            gql_node = p_data
                            gql_selections = (
                                (p_data.get("attributeSelections") or {}).get("selections") or []
                            )
                except Exception as e:
                    logger.warning("[Wake] Falha na consulta GraphQL product em get_product_details: %s", e)

            # 3b. Fallback para busca por termo caso a consulta direta por ID não tenha retornado nó
            if not gql_node and search_term:
                try:
                    payload = {
                        "query": _WAKE_SEARCH_QUERY,
                        "variables": {"q": search_term, "first": 5},
                    }
                    resp = await session.post(
                        GRAPHQL_ENDPOINT,
                        json=payload,
                        headers={"TCS-Access-Token": token},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        edges = (
                            (data.get("data") or {})
                            .get("search", {})
                            .get("products", {})
                            .get("edges", [])
                        )
                        for edge in edges:
                            candidate_node = edge.get("node", {})
                            pid = str(candidate_node.get("productId", ""))
                            alias = (candidate_node.get("aliasComplete") or "").strip("/")
                            if (search_id and pid == search_id) or (alias and alias in clean_url):
                                gql_node = candidate_node
                                break
                        if not gql_node and edges:
                            if search_id:
                                for edge in edges:
                                    if str(edge.get("node", {}).get("productId", "")) == search_id:
                                        gql_node = edge["node"]
                                        break
                            elif not search_id:
                                gql_node = edges[0].get("node", {})
                except Exception as e:
                    logger.warning("[Wake] Falha na consulta GraphQL search em get_product_details: %s", e)

        # Se não obteve nem nó GraphQL nem dados PDP, não é possível montar o produto
        if not gql_node and not pdp_data.get("product_name"):
            return None

        # Processar seleções de atributos (cores e tamanhos) do GraphQL caso presentes
        for sel in gql_selections:
            s_name = (sel.get("name") or "").lower()
            vals = sel.get("values") or []
            if any(c in s_name for c in ("cor", "color")):
                for v in vals:
                    val_str = self._clean_text(str(v.get("value") or ""))
                    if not val_str:
                        continue
                    if val_str not in pdp_data.get("raw_colors", []):
                        pdp_data.setdefault("raw_colors", []).append(val_str)
                    if v.get("available", True) and val_str not in pdp_data.get("available_colors", []):
                        pdp_data.setdefault("available_colors", []).append(val_str)
            elif any(t in s_name for t in ("tamanho", "size")):
                for v in vals:
                    val_str = self._clean_text(str(v.get("value") or ""))
                    if not val_str:
                        continue
                    if val_str not in pdp_data.get("raw_size_grid", []):
                        pdp_data.setdefault("raw_size_grid", []).append(val_str)
                    if v.get("available", True) and val_str not in pdp_data.get("available_sizes", []):
                        pdp_data.setdefault("available_sizes", []).append(val_str)
                if pdp_data.get("raw_size_grid"):
                    pdp_data["size_grid"] = list(pdp_data["raw_size_grid"])
                if pdp_data.get("available_sizes"):
                    pdp_data["raw_sizes"] = list(pdp_data["available_sizes"])

        # 4. Unificação dos dados
        product_name = (
            gql_node.get("productName")
            or pdp_data.get("product_name")
            or search_term
        )
        product_code = (
            str(gql_node.get("productId"))
            if gql_node.get("productId")
            else pdp_data.get("product_code")
        )

        prices = gql_node.get("prices") or {}
        price_raw = prices.get("price")
        list_price_raw = prices.get("listPrice")
        price_discount = (
            float(price_raw)
            if price_raw is not None
            else pdp_data.get("price_discount")
        )
        price_full = (
            float(list_price_raw)
            if list_price_raw is not None
            else (pdp_data.get("price_full") or price_discount)
        )

        images = gql_node.get("images") or []
        image_url = images[0].get("url") if images else pdp_data.get("image_url")

        stock = pdp_data.get("stock_availability")
        if stock is None and "available" in gql_node:
            stock = bool(gql_node.get("available"))
        if stock is None:
            stock = True

        specifications = dict(pdp_data.get("specifications") or {})
        composition = pdp_data.get("composition")
        raw_description = pdp_data.get("raw_description") or ""

        # Enriquecimento com informações GraphQL
        for info in (gql_node.get("informations") or []):
            title = (info.get("title") or "").strip().lower()
            val = self._clean_text(info.get("value") or "")
            if not val:
                continue
            if any(w in title for w in ["composi", "material"]) and not composition:
                composition = val
            elif "descri" in title and "mini" not in title and not raw_description:
                raw_description = val
            elif "mini descri" in title and not raw_description:
                raw_description = val
            elif any(w in title for w in ["referência", "referencia", "código", "codigo"]):
                specifications.setdefault("Referência", val)

        for attr in (gql_node.get("attributes") or []):
            name = (attr.get("name") or "").strip()
            val = self._clean_text(attr.get("value") or "")
            if not name or not val:
                continue
            name_lower = name.lower()
            if name_lower in ("cor", "cores", "color"):
                if val not in pdp_data.get("available_colors", []):
                    pdp_data.setdefault("available_colors", []).append(val)
                if val not in pdp_data.get("raw_colors", []):
                    pdp_data.setdefault("raw_colors", []).append(val)
            elif name_lower in ("tamanho", "tamanhos", "size"):
                if val not in pdp_data.get("available_sizes", []):
                    pdp_data.setdefault("available_sizes", []).append(val)
                if val not in pdp_data.get("raw_sizes", []):
                    pdp_data.setdefault("raw_sizes", []).append(val)
            elif name_lower not in ("prevenda", "gender", "age", "filtrocor", "look"):
                specifications.setdefault(name, val)

        available_colors = sanitize_colors(
            [extract_color_fallback(v) for v in (pdp_data.get("available_colors") or [])]
        )
        available_sizes = sanitize_sizes(
            [extract_size_fallback(v) for v in (pdp_data.get("available_sizes") or [])]
        )
        size_grid = sanitize_sizes(
            [extract_size_fallback(v) for v in (pdp_data.get("size_grid") or [])]
        )

        def _unique_preserve(lst: List[str]) -> List[str]:
            seen = set()
            out = []
            for item in lst:
                k = item.strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    out.append(item.strip())
            return out

        raw_colors = _unique_preserve(pdp_data.get("raw_colors") or [])
        raw_sizes = _unique_preserve(pdp_data.get("raw_sizes") or [])
        raw_size_grid = _unique_preserve(pdp_data.get("raw_size_grid") or [])

        return {
            "url": product_url,
            "brand": brand_name,
            "raw_title": product_name,
            "raw_description": raw_description,
            "price_full": price_full,
            "price_discount": price_discount,
            "price_discount_is_delta": False,
            "image_url": image_url,
            "stock_availability": bool(stock),
            "product_code": product_code,
            "review_product_id": product_code,
            "available_colors": available_colors,
            "available_sizes": available_sizes,
            "raw_colors": raw_colors,
            "raw_sizes": raw_sizes,
            "size_grid": size_grid or available_sizes,
            "raw_size_grid": raw_size_grid or raw_sizes,
            "composition": composition,
            "specifications": specifications,
            "breadcrumb": pdp_data.get("breadcrumb") or [],
        }


