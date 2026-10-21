import asyncio
from typing import List, Dict, Any, Optional, Callable
from services.engines.base_engine import BaseEngine
from services.shopify_api_client import ShopifyApiClient
from core.session_manager import SessionManager
from services.brand_service import brand_service
from services.shipping.base import ShippingCalculation, apply_shipping_calculation
from services.shipping.resolver import resolve_shipping_provider


class ShopifyEngine(BaseEngine):
    """
    Motor de e-commerce para a plataforma Shopify.
    Utiliza APIs JSON públicas para extração de coleções e produtos.
    """

    def __init__(self, brand_key: str):
        self.brand_key = brand_key

    def get_engine_name(self) -> str:
        return "Shopify"

    # Handles/keywords que identificam coleções administrativas/promocionais
    # (não são categorias de produto reais) — excluídas ANTES do pipeline de
    # escopo/probe, pois category_policy não tem como saber que "policy"/
    # "terms" não são categorias de moda (não são marcadores de gênero).
    _NOISE_HANDLES = frozenset({"all", "frontpage", "test", "teste"})
    _NOISE_KEYWORDS = (
        "estoque", "hidden", "policy", "terms", "quem-somos", "quem somos",
        "ate-", "até", "desconto", "off", "giftcard", "reclame",
        "aumento-", "unt", "teste", "promo",
    )

    async def discover_categories(self) -> List[Dict[str, Any]]:
        """
        No Shopify, 'Collections' oficiais são tratadas como categorias
        (D-05: única autoridade). Cada coleção passa por escopo (D-02/D-06)
        e por uma probe real de conteúdo (D-04) antes de ser oferecida.
        """
        outcome = await self._discover_and_validate()
        return [{"name": c.name, "path": c.path} for c in outcome.offered]

    async def get_catalog(self) -> List[Dict[str, Any]]:
        """
        Retorna as coleções agrupadas para exibição no frontend, derivadas da
        MESMA lista validada usada por `discover_categories`.
        """
        from services.category_discovery import build_grouped_catalog

        outcome = await self._discover_and_validate()
        return build_grouped_catalog(outcome.offered)

    async def _discover_and_validate(self) -> Any:
        """Pipeline compartilhado: coleções oficiais -> escopo -> probe -> oferecidas."""
        from services.category_discovery import (
            CategoryValidationOutcome,
            infer_male_only_brand,
            make_scope_policy,
            validate_discovered_categories,
        )

        session = await SessionManager.get_session()
        client = ShopifyApiClient(self.brand_key, session=session)
        collections = await client.fetch_collections()
        if not collections:
            return CategoryValidationOutcome(offered=[], probes={})

        candidates = self._collections_to_candidates(collections, client)
        if not candidates:
            return CategoryValidationOutcome(offered=[], probes={})

        # D-06: catálogos single-gender (sem nenhum marcador feminino entre
        # as coleções oficiais) aceitam coleções neutras; catálogos mistos
        # exigem evidência masculina explícita — nunca assumida por omissão.
        male_only_brand = infer_male_only_brand(candidates)

        async def _probe(candidate: Any) -> Any:
            return await self.category_has_products(candidate, client=client)

        return await validate_discovered_categories(
            candidates,
            scope_policy=make_scope_policy(male_only_brand=male_only_brand),
            has_products=_probe,
            concurrency=20,
        )

    def _collections_to_candidates(
        self, collections: List[Dict[str, Any]], client: ShopifyApiClient
    ) -> List[Any]:
        from services.category_discovery import DiscoveredCategory

        candidates: List[Any] = []
        for c in collections:
            handle = (c.get("handle") or "").lower()
            title = (c.get("title") or "").strip()
            title_lower = title.lower()

            is_noise = (
                not handle
                or handle in self._NOISE_HANDLES
                or any(k in handle for k in self._NOISE_KEYWORDS)
                or any(k in title_lower for k in self._NOISE_KEYWORDS)
            )
            if is_noise:
                continue

            path = f"/collections/{handle}"
            candidates.append(
                DiscoveredCategory(
                    name=title or handle,
                    path=path,
                    url=f"{client.base_url}{path}",
                    group="Coleções / Categorias",
                )
            )
        return candidates

    async def category_has_products(
        self, candidate: Any, client: Optional[ShopifyApiClient] = None
    ) -> Any:
        """Probe de conteúdo (D-04) via `/collections/{handle}/products.json?limit=1`."""
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        if client is None:
            session = await SessionManager.get_session()
            client = ShopifyApiClient(self.brand_key, session=session)

        handle = candidate.path.rsplit("/", 1)[-1] if candidate.path else ""
        count = await client.category_result_count(handle)
        if count is None:
            return CategoryProbeResult(ProbeStatus.BLOCKED, detail="probe falhou/bloqueado")
        if count > 0:
            return CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, detail=f"{count} produto(s) amostrados")
        return CategoryProbeResult(ProbeStatus.EMPTY, detail="0 produtos")

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        """Executa varredura via ShopifyApiClient com streaming e logs padronizados."""
        session = await SessionManager.get_session()
        client = ShopifyApiClient(self.brand_key, session=session)

        # Wrapper para garantir contrato de logs padronizado
        def emit(msg):
            self.emit_log(log_callback, msg)

        is_kids = any(k in category_url.lower() for k in ("infantil", "junior", "kids", "menino"))
        filter_fn = self.filter_boys_fashion if is_kids else self.filter_mens_fashion

        async for product in client.scrape_category_paged(category_url, emit, cancel_event):
            # Aplica os Quality Gates antes de dar yield
            validated = self.validate_single(product, log_callback=log_callback)
            if validated:
                if filter_fn([validated]):
                    yield validated

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> Any:
        """Realiza busca via ShopifyApiClient."""
        session = await SessionManager.get_session()
        client = ShopifyApiClient(self.brand_key, session=session)
        result = await client.search(query, max_results, sort=sort)

        if include_shipping and zipcode and result and hasattr(result, 'products'):
            await self._populate_shipping(result.products, zipcode)
                
        return result

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        """Extrai detalhes de um único produto Shopify."""
        session = await SessionManager.get_session()
        client = ShopifyApiClient(self.brand_key, session=session)
        prod = await client.get_product_by_url(product_url)
        return self.validate_single(prod) if prod else None

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[ShippingCalculation]:
        brand = brand_service.get_brand(self.brand_key)
        if not brand:
            return None
        provider = resolve_shipping_provider(brand)
        return await provider.calculate(product, zipcode, brand)

    async def _populate_shipping(self, products: List[Any], zipcode: str) -> None:
        semaphore = asyncio.Semaphore(3)

        async def _one(product: Any) -> None:
            async with semaphore:
                try:
                    calculation = await self.calculate_shipping(product, zipcode)
                    if calculation is not None:
                        apply_shipping_calculation(product, calculation)
                except Exception:
                    # Frete nunca deve derrubar o produto ou a busca da marca.
                    return

        await asyncio.gather(*(_one(product) for product in products))
