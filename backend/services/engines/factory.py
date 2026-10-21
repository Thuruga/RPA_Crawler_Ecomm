from typing import Optional, List
from services.engines.base_engine import BaseEngine
from services.engines.vtex_engine import VTEXEngine
from services.engines.shopify_engine import ShopifyEngine
from services.engines.mercado_livre_engine import MercadoLivreEngine
from services.engines.netshoes_engine import NetshoesEngine
from services.engines.amazon_engine import AmazonEngine
from services.brand_service import brand_service
from services.engines.brand_key_utils import normalize_brand_key
from services.engines.result_sort import sort_brand_products
from services.engines.resilient_marketplace import ResilientMarketplaceEngine
from core.models import BrandSearchResult

class EngineFactory:
    """
    Fábrica para instanciar a Engine correta baseada na marca.
    """

    @staticmethod
    def get_engine(brand_key: str) -> BaseEngine:
        """
        Resolve a engine para a marca. 
        """
        # Trata as marcas virtuais de marketplace que vêm do frontend
        brand_key_lower = normalize_brand_key(brand_key)
        if brand_key_lower == "mercadolivre":
            return ResilientMarketplaceEngine(MercadoLivreEngine(), "mercado_livre")
        elif brand_key_lower == "netshoes":
            return ResilientMarketplaceEngine(NetshoesEngine(), "netshoes")
        elif brand_key_lower == "amazon":
            return ResilientMarketplaceEngine(AmazonEngine(), "amazon")

        brand_data = brand_service.get_brand(brand_key.lower())
        
        # Lê o campo 'engine' do JSON (pode vir como dict ou objeto Pydantic)
        engine_type = "vtex"
        if brand_data:
            if isinstance(brand_data, dict):
                engine_type = brand_data.get("engine", "vtex")
            else:
                # O Pydantic model pode não ter o campo 'engine' ainda, mas o brand_service 
                # lê o JSON bruto que já contém a chave.
                engine_type = getattr(brand_data, "engine", "vtex")

        # Lacoste canonicamente utiliza Algolia como motor oficial de catálogo e PDP
        if brand_key_lower == "lacoste":
            engine_type = "algolia"

        if engine_type == "shopify":
            return ShopifyEngine(brand_key)

        # D-09 (Phase 31 — SFCC now live): SFCCEngine replaces the former NotImplementedError
        # guard for engine_type="sfcc".  Import is lazy (inside get_engine) to preserve
        # circular-import safety — same pattern as the existing engine branches above.
        if engine_type == "sfcc":
            from services.engines.sfcc_engine import SFCCEngine  # noqa: PLC0415
            return SFCCEngine(brand_key)

        # D-09 (Phase 32 — Wake now live): WakeEngine replaces the former NotImplementedError
        # guard for engine_type="wake".  Import is lazy (inside get_engine) to preserve
        # circular-import safety — same pattern as SFCCEngine above (L48-50).
        if engine_type == "wake":
            from services.engines.wake_engine import WakeEngine  # noqa: PLC0415
            return WakeEngine(brand_key)

        if engine_type in ("mercadolivre", "mercado_livre"):
            return ResilientMarketplaceEngine(MercadoLivreEngine(), "mercado_livre")

        if engine_type == "netshoes":
            return ResilientMarketplaceEngine(NetshoesEngine(), "netshoes")

        if engine_type == "amazon":
            return ResilientMarketplaceEngine(AmazonEngine(), "amazon")

        if engine_type == "zara":
            from services.engines.zara_engine import ZaraEngine  # noqa: PLC0415
            return ZaraEngine(brand_key)

        if engine_type == "algolia":
            from services.engines.algolia_engine import AlgoliaEngine  # noqa: PLC0415
            brand_data = brand_service.get_brand(brand_key.lower()) or {}
            # Transform Pydantic model to dict if needed
            brand_dict = brand_data.model_dump() if hasattr(brand_data, "model_dump") else brand_data
            if not isinstance(brand_dict, dict):
                brand_dict = {"id": brand_key, "domain": f"www.{brand_key}.com.br"}
            return AlgoliaEngine(brand_dict)

        if engine_type == "dafiti":
            from services.engines.dafiti_engine import DafitiEngine  # noqa: PLC0415
            return DafitiEngine(brand_key)

        if engine_type == "privalia":
            from services.engines.privalia_engine import PrivaliaEngine  # noqa: PLC0415
            return PrivaliaEngine(brand_key)

        if engine_type == "linx":
            from services.engines.linx_engine import LinxEngine  # noqa: PLC0415
            if not brand_data:
                raise ValueError(
                    f"Marca '{brand_key}' com engine=linx nao encontrada no banco. "
                    "Cadastre a marca com domain e engine='linx' antes de usar."
                )
            domain = (
                brand_data.get("domain") if isinstance(brand_data, dict)
                else getattr(brand_data, "domain", None)
            )
            if not domain:
                raise ValueError(
                    f"Marca '{brand_key}' com engine=linx nao tem 'domain' configurado no banco."
                )
            cfg = (
                brand_data.get("configuration") if isinstance(brand_data, dict)
                else getattr(brand_data, "configuration", {})
            ) or {}
            domain_aliases = cfg.get("domain_aliases") or []
            junior_domain = cfg.get("junior_domain") or next((a for a in domain_aliases if "junior" in a.lower()), None)
            return LinxEngine(brand_key=brand_key, base_domain=domain, junior_domain=junior_domain)

        return VTEXEngine(brand_key)

    async def search_all_brands(
        self,
        query: str,
        brands: Optional[List[str]] = None,
        max_per_brand: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> List[BrandSearchResult]:
        """
        Busca `query` em todas as marcas em PARALELO usando engines. `None`
        solicita exaustão segura da paginação disponível em cada provedor.
        """
        import asyncio
        from core.models import BrandSearchResult
        
        if not query or not query.strip():
            return []

        if brands:
            target_brands = brands
        else:
            # Single source of truth: list_brands(active_only=True) includes the 3
            # marketplace brand_keys (mercado_livre, netshoes, amazon) now that they
            # live in brands.json as real entries (Plan 04 / D-10).
            # Deactivating a marketplace via PATCH /brands/{key}/active automatically
            # excludes it from the next search — no hardcoded bypass (T-40-06).
            target_brands = [b.brand_key for b in brand_service.list_brands(active_only=True)]

        target_brands = [b.lower() for b in target_brands]

        async def _search_one(brand_key: str) -> BrandSearchResult:
            try:
                engine = self.get_engine(brand_key)
                return await engine.search(
                    query=query.strip(),
                    max_results=max_per_brand,
                    sort=sort,
                    zipcode=zipcode,
                    include_shipping=include_shipping
                )
            except Exception as e:
                # Retorna um resultado vazio com erro para não quebrar o gather
                return BrandSearchResult(brand_key=brand_key, brand_name=brand_key, error=str(e))

        tasks = [_search_one(brand_key) for brand_key in target_brands]
        results = await asyncio.gather(*tasks)

        # Ordenação client-side garantida para TODAS as marcas no ponto único de
        # fan-in. price_asc/price_desc são computáveis a partir dos dados que todo
        # engine retorna, então os aplicamos aqui uniformemente — vários engines
        # (Shopify/Wake/SFCC/ML) não implementam sort nativo. relevance/recent/
        # top_selling preservam a ordem nativa do engine (VTEX/Amazon/Netshoes via
        # parâmetro nativo), pois dependem de score/data/vendas que a busca não traz.
        for result in results:
            if getattr(result, "products", None):
                result.products = sort_brand_products(result.products, sort)

        return list(results)

engine_factory = EngineFactory()
