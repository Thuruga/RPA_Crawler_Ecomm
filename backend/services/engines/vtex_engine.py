import logging
import asyncio
from typing import List, Dict, Any, Optional, Callable
from services.engines.base_engine import BaseEngine
from services.vtex_api_scraper import VtexApiClient

logger = logging.getLogger("VTEXEngine")

# D-07 (Plano 01-07): limite de tempo por candidato para o probe de conteúdo
# (category_result_count_with_tiles_fallback). Um único candidato lento (ex.
# fallback de tiles renderizado via Playwright) nunca pode esgotar sozinho o
# orçamento total por marca — timeout vira BLOCKED diagnóstico, nunca derruba
# a varredura inteira com um asyncio.TimeoutError não tratado.
PER_CANDIDATE_PROBE_TIMEOUT_SECONDS = 60


class VTEXEngine(BaseEngine):
    """
    Motor especializado em plataformas VTEX.
    """

    def __init__(self, brand_key: str):
        self.brand_key = brand_key

    def get_engine_name(self) -> str:
        return "VTEX"

    async def discover_categories(self) -> List[Dict[str, Any]]:
        """
        Descobre a árvore de categorias real da VTEX (autoridade oficial,
        D-05), aplica escopo (D-02/D-06) e probe de conteúdo (D-04), e
        retorna a lista plana das categorias oferecidas.
        """
        outcome = await self._discover_and_validate()
        return [{"name": c.name, "path": c.path} for c in outcome.offered]

    async def get_catalog(self) -> List[Dict[str, Any]]:
        """
        Retorna o catálogo agrupado da VTEX, derivado da MESMA lista
        validada usada por `discover_categories` — nunca de cache
        expirado/fallback estático (D-05). `VTEXCatalogService` delega para
        este método para garantir identidades/ordem equivalentes.
        """
        from services.category_discovery import build_grouped_catalog

        outcome = await self._discover_and_validate()
        return build_grouped_catalog(outcome.offered)

    async def _discover_and_validate(self) -> Any:
        """Pipeline compartilhado: árvore oficial VTEX -> escopo -> probe -> oferecidas."""
        from services.brand_service import brand_service
        from services.category_discovery import (
            CategoryValidationOutcome,
            infer_male_only_brand,
            make_scope_policy,
            validate_discovered_categories,
        )

        logger.info(f"[{self.brand_key}] Buscando categorias via API VTEX...")

        brand_data = brand_service.get_brand(self.brand_key)
        if not brand_data:
            return CategoryValidationOutcome(offered=[], probes={})

        raw_tree = await VtexApiClient.fetch_categories(brand_data.domain)
        if not raw_tree:
            return CategoryValidationOutcome(offered=[], probes={})

        candidates = self._tree_to_candidates(raw_tree)
        if not candidates:
            return CategoryValidationOutcome(offered=[], probes={})

        # D-06: catálogos single-gender (ex. Aramis, sem nenhum marcador
        # feminino na árvore) aceitam folhas neutras; catálogos mistos (ex.
        # Reserva/Tommy, com departamentos /feminino) exigem ancestral
        # masculino confirmado — nunca assumido por omissão.
        male_only_brand = infer_male_only_brand(candidates)

        domain = brand_data.domain
        from core.session_manager import SessionManager

        session = await SessionManager.get_session()
        async with VtexApiClient(self.brand_key, session=session) as client:

            async def _probe(candidate: Any) -> Any:
                return await self._probe_category(client, candidate, domain)

            return await validate_discovered_categories(
                candidates,
                scope_policy=make_scope_policy(male_only_brand=male_only_brand),
                has_products=_probe,
                concurrency=20,
            )

    def _tree_to_candidates(
        self, tree: List[Dict[str, Any]], department: str = "", ancestors: tuple = ()
    ) -> List[Any]:
        """Achata a árvore oficial VTEX em `DiscoveredCategory`, preservando
        breadcrumb/ancestrais e o agrupamento por departamento (nó raiz)."""
        from services.category_discovery import DiscoveredCategory

        flat: List[Any] = []
        for node in tree:
            name = node.get("name", "")
            url = node.get("url", "")
            has_children = node.get("hasChildren", False)
            children = node.get("children", [])
            dept = department or name

            name_lower = name.lower()
            url_lower = (url or "").lower()
            if any(marker in name_lower for marker in ("inativo", "zz excluir", "kit teste")) or "/inativo" in url_lower:
                continue

            if url and name:
                path = self._extract_category_path(url) or url
                display_name = f"{ancestors[-1]} ({name})" if ancestors and name.lower() in ("coleção", "promoção", "colecao", "promocao") else (f"{ancestors[-1]} - {name}" if ancestors else name)
                flat.append(
                    DiscoveredCategory(
                        name=display_name,
                        path=path,
                        url=url,
                        group=dept,
                        ancestors=ancestors,
                    )
                )

            if has_children and children:
                flat.extend(
                    self._tree_to_candidates(
                        children,
                        department=dept,
                        ancestors=ancestors + (name,) if name else ancestors,
                    )
                )
        return flat

    @staticmethod
    def _extract_category_path(url: str) -> str:
        """Extrai o path relativo de uma URL de categoria VTEX (absoluta ou já relativa)."""
        if not url:
            return ""
        if "://" in url:
            rest = url.split("://", 1)[1]
            slash_idx = rest.find("/")
            return rest[slash_idx:] if slash_idx >= 0 else ""
        if url.startswith("/"):
            return url
        return ""

    async def category_has_products(self, candidate: Any) -> Any:
        """Probe de conteúdo (D-04) por conta/search-count VTEX — ver `_probe_category`."""
        from services.brand_service import brand_service
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        brand_data = brand_service.get_brand(self.brand_key)
        if not brand_data:
            return CategoryProbeResult(ProbeStatus.BLOCKED, detail="marca nao registrada")

        from core.session_manager import SessionManager

        session = await SessionManager.get_session()
        async with VtexApiClient(self.brand_key, session=session) as client:
            return await self._probe_category(client, candidate, brand_data.domain)

    @staticmethod
    async def _probe_category(client: VtexApiClient, candidate: Any, domain: str) -> Any:
        from services.category_discovery import CategoryProbeResult, ProbeStatus

        try:
            count = await asyncio.wait_for(
                client.category_result_count_with_tiles_fallback(
                    candidate.url, candidate.path, domain
                ),
                timeout=PER_CANDIDATE_PROBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # D-07: um candidato lento nunca deve estourar o orçamento da
            # marca inteira — vira BLOCKED diagnóstico, não uma exceção que
            # derruba a varredura toda (hering/austral/hugoboss root cause).
            return CategoryProbeResult(ProbeStatus.BLOCKED, detail="probe timeout")
        if count is None:
            return CategoryProbeResult(ProbeStatus.BLOCKED, detail="probe falhou/bloqueado")
        if count > 0:
            return CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, detail=f"{count} produto(s)")
        return CategoryProbeResult(ProbeStatus.EMPTY, detail="0 produtos")

    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> Any:
        """
        Executa a busca via VtexApiClient usando sessão compartilhada.
        """
        from core.session_manager import SessionManager
        session = await SessionManager.get_session()
        async with VtexApiClient(self.brand_key, session=session) as client:
            return await client.search(
                query=query,
                max_results=max_results,
                sort=sort,
                zipcode=zipcode,
                include_shipping=include_shipping
            )

    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        """
        Extrai detalhes de um único produto via VtexApiClient usando sessão compartilhada.
        """
        from core.session_manager import SessionManager
        session = await SessionManager.get_session()
        async with VtexApiClient(self.brand_key, session=session) as client:
            prod = await client.get_product_by_url(product_url)
            return self.validate_single(prod) if prod else None

    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        """
        Implementa o pipeline de extração paginada da VTEX com streaming.
        """
        def emit(msg):
            self.emit_log(log_callback, msg)

        is_kids = any(k in category_url.lower() for k in ("infantil", "junior", "kids", "menino"))
        filter_fn = self.filter_boys_fashion if is_kids else self.filter_mens_fashion

        async with VtexApiClient(self.brand_key) as client:
            async for product in client.scrape_category_paged(
                category_url=category_url,
                log_callback=emit,
                cancel_event=cancel_event,
                chunk_size=50,
                zipcode=zipcode,
                include_shipping=include_shipping
            ):
                # Aplica os Quality Gates em cada produto individualmente (Streaming)
                validated = self.validate_single(product, log_callback=log_callback)
                if validated:
                    # Pureza de gênero: descarta itens femininos/fora do escopo
                    if filter_fn([validated]):
                        yield validated

    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[Dict[str, Any]]:
        # VTEX shipping calculation not implemented for now
        return None

