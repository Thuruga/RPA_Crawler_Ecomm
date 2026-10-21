from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Callable
import asyncio
import logging

logger = logging.getLogger(__name__)

class BaseEngine(ABC):
    """
    Interface abstrata para motores de e-commerce.
    Define o contrato para orquestração de scraping e inteligência.
    """

    # Whether a None return from calculate_shipping() (Tier 2) means the engine
    # genuinely attempted and was blocked (anti-bot) vs. Tier 2 being an
    # unimplemented stub for this engine. Callers (e.g. cross_marketplace_service)
    # use this to avoid labeling an unimplemented engine as "blocked".
    SHIPPING_TIER2_BLOCKS_ON_NONE: bool = True

    # SR-01: Raw-fetch multiplier — all engines request at least this many raw
    # candidates *before* running filter_mens_fashion() and validate_single(),
    # so that Quality Gates (gender filter, Pydantic validation) don't silently
    # shrink the final result set below `max_results`.
    #
    # Value rationale: empirically, fewer than 5 % of products from branded
    # menswear stores are dropped by filter_mens_fashion (false positives are
    # very rare in single-gender catalogs). A 2× multiplier therefore yields
    # a comfortable safety margin without inflating latency (2 × max_results
    # is still a single API page for VTEX/Wake, and two pages for Shopify).
    # Override per-engine if the engine's filter is known to be more aggressive.
    SEARCH_RAW_FETCH_MULTIPLIER: int = 2

    @classmethod
    def _resolve_max_raw_fetch(cls, max_results: Optional[int]) -> Optional[int]:
        """Return the raw candidate count to fetch before Quality-Gate filtering.

        SR-01 (search result count guarantee): every engine should request
        ``max_results * SEARCH_RAW_FETCH_MULTIPLIER`` raw items from the
        upstream API so that ``filter_mens_fashion`` and ``validate_single``
        have room to discard poor-quality / off-category candidates without
        the final result set falling below ``max_results``.

        Callers must still truncate to ``max_results`` *after* filtering.
        """
        if max_results is None:
            return None
        try:
            safe = max(1, int(max_results))
        except (TypeError, ValueError):
            safe = 10
        return safe * cls.SEARCH_RAW_FETCH_MULTIPLIER


    @abstractmethod
    async def run_bulk_scrape(
        self,
        category_url: str,
        log_callback: Optional[Callable] = None,
        cancel_event: Optional[asyncio.Event] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ):
        """
        Executa uma varredura completa em uma categoria.
        Deve renderizar (yield) produtos um a um.
        """
        pass

    @abstractmethod
    async def discover_categories(self) -> List[Dict[str, Any]]:
        """
        Descobre a árvore de categorias real do motor.
        """
        pass

    @abstractmethod
    async def get_catalog(self) -> List[Dict[str, Any]]:
        """
        Retorna o catálogo de categorias formatado para o frontend.
        """
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: Optional[int] = 10,
        sort: Optional[str] = None,
        zipcode: Optional[str] = None,
        include_shipping: bool = False
    ) -> Any:
        """
        Executa uma busca por termo na plataforma. `None` significa percorrer
        todas as páginas disponíveis com término seguro.
        """
        pass

    @abstractmethod
    async def get_product_details(self, product_url: str) -> Optional[Dict[str, Any]]:
        """
        Extrai detalhes de um único produto.

        IMPORTANTE: este método existe para o fluxo de enriquecimento de seller/preço
        do cross-marketplace. Os engines de marketplace (ML/Amazon/Netshoes) retornam
        aqui APENAS `{"seller": ...}` (ou `+price`), NÃO o produto completo. Para obter
        um produto completo compatível com `RawProductBronze` (price monitor), use
        `get_pdp_product`.
        """
        pass

    async def get_pdp_product(self, product_url: str) -> Optional[Dict[str, Any]]:
        """
        Extrai o produto COMPLETO de uma PDP (Product Detail Page), no formato
        compatível com `RawProductBronze` (campos url/brand/raw_title/
        raw_description/price_full/image_url/stock_availability/...).

        Usado pelo monitor de preço (`PriceMonitorService._monitor_loop`).

        Default: delega para `get_product_details`. Engines que JÁ retornam o
        produto completo lá (VTEX/Shopify/SFCC/Wake/Zara via `validate_single`)
        funcionam sem alteração. Engines de marketplace (ML/Amazon/Netshoes),
        cujo `get_product_details` retorna apenas `{"seller": ...}`,
        SOBRESCREVEM este método para parsear o produto completo da PDP.
        """
        return await self.get_product_details(product_url)

    @abstractmethod
    async def calculate_shipping(self, product: Any, zipcode: str) -> Optional[Dict[str, Any]]:
        """
        Calcula o frete para o produto e CEP fornecido.
        Retorna um dicionário com is_free_shipping e shipping_price, ou None em caso de falha.
        """
        pass

    async def calculate_shipping_advanced(self, url: str, zipcode: str) -> Optional[Dict[str, Any]]:
        """
        Calcula o frete usando métodos avançados (Playwright) na página do produto.
        Pode ser sobreposto pelas classes filhas (Netshoes, Amazon).
        """
        raise NotImplementedError()

    async def category_has_products(self, candidate: Any) -> Any:
        """
        Probe de conteúdo (engine-specific) para o pipeline compartilhado de
        descoberta de categorias (`services.category_discovery`).

        Sem default permissivo: NÃO retorna `True` por padrão. Engines
        catalog-capable (VTEX/Shopify/Wake, e futuramente SFCC/Zara) DEVEM
        sobrescrever este método com uma checagem real (count/search limit 1,
        GraphQL hotsite, etc.) que classifica o resultado como
        `has_products`/`empty`/`blocked` (ver `category_discovery.CategoryProbeResult`).
        Engines search-only (Amazon/Mercado Livre/Netshoes) não participam
        deste pipeline (aplicabilidade N/A) e portanto não precisam
        sobrescrever — mas se chamado, o comportamento é falhar explicitamente
        em vez de aprovar silenciosamente uma categoria sem evidência (D-04).
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement category_has_products; "
            "engines participating in category discovery must implement an "
            "explicit content probe (no permissive default)."
        )

    @abstractmethod
    def get_engine_name(self) -> str:
        """Retorna o nome amigável do motor (ex: 'VTEX')."""
        pass

    def emit_log(self, callback: Optional[Callable], message: Any, type: str = "info", **kwargs):
        """
        Padroniza a emissão de logs para o frontend.
        Garante que a mensagem seja sempre um dicionário estruturado.
        """
        if not callback:
            return
            
        if isinstance(message, dict):
            # Se já for um dict, apenas garante que os campos extras sejam mesclados
            payload = message.copy()
            if kwargs:
                payload.update(kwargs)
            callback(payload)
        else:
            # Se for string ou outro tipo, encapsula no formato padrão
            payload = {"type": type, "message": str(message)}
            if kwargs:
                payload.update(kwargs)
            callback(payload)

    def validate_and_filter(
        self, 
        products: List[Any], 
        log_callback: Optional[Callable] = None
    ) -> List[Dict[str, Any]]:
        """
        Aplica os Quality Gates nos produtos extraídos.
        Filtra itens inválidos e emite logs de aviso.
        """
        from core.models import RawProductBronze
        from pydantic import ValidationError

        valid_products = []
        rejected_count = 0
        
        for p in products:
            try:
                # Garante que os validadores do Pydantic rodem
                if isinstance(p, dict):
                    validated = RawProductBronze.model_validate(p)
                else:
                    # Se já for um objeto, converte para dict e valida para garantir que os validators rodem
                    validated = RawProductBronze.model_validate(p if isinstance(p, dict) else p.model_dump())
                
                valid_products.append(validated.model_dump())
            except (ValidationError, ValueError) as e:
                rejected_count += 1
                title = "Produto Desconhecido"
                if isinstance(p, dict):
                    title = p.get('raw_title') or p.get('product_name') or "Sem título"
                else:
                    title = getattr(p, 'raw_title', None) or getattr(p, 'product_name', 'Sem título')
                
                # Simplifica a mensagem de erro do Pydantic
                reason = str(e)
                if hasattr(e, 'errors') and callable(getattr(e, 'errors', None)):
                    try:
                        errs = e.errors()
                        if errs:
                            reason = errs[0].get('msg', 'Erro desconhecido')
                    except Exception as ex:
                        logger.debug("Não foi possível extrair detalhes do erro de validação: %s", ex)
                
                self.emit_log(
                    log_callback, 
                    f"⚠️ [Quality Gate] '{title}' descartado. Motivo: {reason}",
                    type="brand_warning" # Usando warning para não assustar o usuário, mas informar o descarte
                )
        
        if rejected_count > 0:
            self.emit_log(log_callback, f"📊 Quality Gate finalizado: {rejected_count} itens descartados por baixa qualidade.")
            
        return valid_products

    def validate_single(self, product: Any, log_callback: Optional[Callable] = None) -> Optional[Dict[str, Any]]:
        """Valida um único produto."""
        results = self.validate_and_filter([product], log_callback)
        return results[0] if results else None

    @staticmethod
    def _strip_accents(text: str) -> str:
        import unicodedata
        return "".join(
            c for c in unicodedata.normalize("NFD", text)
            if unicodedata.category(c) != "Mn"
        )

    @staticmethod
    def _extract_product_field(product: Any, *field_names: str) -> Any:
        for f in field_names:
            if isinstance(product, dict):
                v = product.get(f)
            else:
                v = getattr(product, f, None)
            if v is not None:
                return v
        return None

    @staticmethod
    def filter_mens_fashion(products: List[Any]) -> List[Any]:
        """
        Filtra os produtos para remover itens fora da categoria de moda/acessórios masculinos.
        Valida gênero explícito, categorias, URLs, SKUs femininos e termos bloqueados no título.
        """
        import re

        female_genders = {"feminino", "feminina", "female", "woman", "mulher", "girl", "menina"}

        female_blocklist = [
            r"feminina", r"femininas", r"feminino", r"femininos",
            r"mulher", r"mulheres", r"menina", r"meninas", r"garota", r"garotas",
            r"saia", r"saias", r"vestido", r"vestidos", r"blusa", r"blusas",
            r"sutia", r"sutias", r"calcinha", r"calcinhas",
            r"maio", r"maios", r"biquini", r"biquinis",
            r"pantalona", r"pantalonas", r"pantacourt",
            r"cropped", r"croppeds",
            r"macaquinho", r"macaquinhos",
            r"camisola", r"camisolas", r"babydoll", r"lingerie", r"lingeries",
            r"sapatilha", r"sapatilhas", r"scarpin", r"scarpins", r"tamanco", r"tamancos",
            r"anabela", r"rasteirinha", r"rasteirinhas",
            r"corpete", r"espartilho", r"gestante", r"gestantes", r"maternidade"
        ]
        automotive_blocklist = [
            r"carro", r"pneu", r"automotivo", r"moto", r"roda", r"macaneta", r"parachoque", r"retrovisor", r"farol",
            r"escapamento", r"capa de banco", r"tapete", r"aro",
            r"volkswagen", r"fiat", r"chevrolet", r"ford", r"hyundai", r"toyota", r"honda",
            # "Flex" é uma característica comum de calças masculinas; sem
            # contexto automotivo ele não pode desqualificar uma peça válida.
            r"msi", r"1\.0", r"1\.6", r"tsi", r"comfortline", r"highline", r"trendline"
        ]

        patterns = [re.compile(r'\b' + bw + r'\b', re.IGNORECASE) for bw in (female_blocklist + automotive_blocklist)]

        female_cat_keywords = [
            "feminina", "feminino", "mulher", "menina", "garota",
            "lingerie", "moda intima", "calcados femininos", "calcado feminino"
        ]

        female_url_parts = [
            "/feminino", "/feminina", "/mulher", "/moda-feminina", "/menina", "/lingerie"
        ]

        filtered = []
        for p in products:
            # 1. Gênero explícito
            gender = str(BaseEngine._extract_product_field(p, "gender", "genero") or "").strip().lower()
            if gender in female_genders:
                continue

            # 2. SKU / Product Code (ex: Dafiti APF/ACF/FAF)
            sku = str(BaseEngine._extract_product_field(p, "sku", "product_code", "data_sku") or "").upper()
            if any(fem_sku in sku for fem_sku in ("APF", "ACF", "FAF")):
                continue

            # 3. Categorias
            categories_raw = BaseEngine._extract_product_field(p, "categories", "category", "categoria", "categorias")
            if categories_raw:
                if isinstance(categories_raw, (list, tuple)):
                    cat_str = " ".join(str(c) for c in categories_raw).lower()
                else:
                    cat_str = str(categories_raw).lower()
                cat_norm = BaseEngine._strip_accents(cat_str)
                if any(term in cat_norm for term in female_cat_keywords):
                    continue

            # 4. URL / Link
            url = str(BaseEngine._extract_product_field(p, "url", "link") or "").lower()
            if url:
                url_norm = BaseEngine._strip_accents(url)
                if any(part in url_norm for part in female_url_parts):
                    continue

            # 5. Título
            title = BaseEngine._extract_product_field(p, "titulo", "product_name", "raw_title", "title") or ""
            title_norm = BaseEngine._strip_accents(str(title).lower())

            if any(pattern.search(title_norm) for pattern in patterns):
                continue

            filtered.append(p)

        return filtered

    @staticmethod
    def filter_boys_fashion(products: List[Any]) -> List[Any]:
        """
        Filtra produtos do segmento infantil para aceitar estritamente itens masculinos e unissex (D-08, D-09).
        Descarta qualquer item com termos explicitamente femininos.
        """
        import re

        female_blocklist = [
            r"menina", r"meninas", r"garota", r"garotas", r"infantil feminina", r"infantil feminino",
            r"feminina", r"feminino", r"femininas", r"femininos",
            r"vestido", r"vestidos", r"saia", r"saias", r"blusa", r"blusas",
            r"tiara", r"tiaras", r"laco", r"lacos",
            r"biquini", r"biquinis", r"maio", r"maios", r"sapatilha", r"sapatilhas",
            r"boneca", r"bonecas", r"bailarina", r"princesa", r"princesas",
            r"pantalona", r"pantalonas", r"pantacourt", r"cropped", r"croppeds",
            r"macaquinho", r"macaquinhos", r"camisola", r"camisolas", r"babydoll",
            r"lingerie", r"sutia", r"calcinha"
        ]
        patterns = [re.compile(r'\b' + bw + r'\b', re.IGNORECASE) for bw in female_blocklist]

        female_genders = {"feminino", "feminina", "female", "girl", "menina"}
        female_cat_keywords = ["feminina", "feminino", "menina", "garota", "lingerie"]
        female_url_parts = ["/feminino", "/feminina", "/menina", "/garota"]

        filtered = []
        for p in products:
            gender = str(BaseEngine._extract_product_field(p, "gender", "genero") or "").strip().lower()
            if gender in female_genders:
                continue

            sku = str(BaseEngine._extract_product_field(p, "sku", "product_code", "data_sku") or "").upper()
            if any(fem_sku in sku for fem_sku in ("APF", "ACF", "FAF")):
                continue

            categories_raw = BaseEngine._extract_product_field(p, "categories", "category", "categoria", "categorias")
            if categories_raw:
                if isinstance(categories_raw, (list, tuple)):
                    cat_str = " ".join(str(c) for c in categories_raw).lower()
                else:
                    cat_str = str(categories_raw).lower()
                cat_norm = BaseEngine._strip_accents(cat_str)
                if any(term in cat_norm for term in female_cat_keywords):
                    continue

            url = str(BaseEngine._extract_product_field(p, "url", "link") or "").lower()
            if url:
                url_norm = BaseEngine._strip_accents(url)
                if any(part in url_norm for part in female_url_parts):
                    continue

            title = BaseEngine._extract_product_field(p, "titulo", "product_name", "raw_title", "title") or ""
            title_norm = BaseEngine._strip_accents(str(title).lower())
            if not any(pattern.search(title_norm) for pattern in patterns):
                filtered.append(p)

        return filtered
