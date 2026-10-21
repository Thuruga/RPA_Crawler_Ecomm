from abc import ABC, abstractmethod
from typing import Optional, List, Any
from core.models import RawProductBronze, BrandSearchResult

class BaseScraper(ABC):
    """
    Interface abstrata para motores de scraping.
    Permite que a aplicação suporte múltiplas plataformas (VTEX, Shopify, Magento, etc).
    """

    @abstractmethod
    async def get_product_by_url(self, product_url: str) -> Optional[RawProductBronze]:
        """Extrai dados de um único produto via URL."""
        pass

    @abstractmethod
    async def scrape_category_paged(
        self,
        category_url: str,
        log_callback=None,
        cancel_event=None,
        chunk_size=50
    ):
        """Varre uma categoria completa paginando os resultados e dando yield em cada produto."""
        pass

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 10,
        sort: Optional[str] = None
    ) -> BrandSearchResult:
        """Executa uma busca por termo na plataforma."""
        pass
