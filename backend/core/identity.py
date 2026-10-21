import random
import logging
from typing import Optional
from config import settings

logger = logging.getLogger("IdentityManager")

class IdentityManager:
    """Gerencia a identidade do scraper (User-Agent e Proxy) para evitar bloqueios."""

    @staticmethod
    def get_random_user_agent() -> str:
        return random.choice(settings.USER_AGENTS)

    @staticmethod
    def get_proxy() -> Optional[str]:
        """
        Retorna um proxy seguindo a hierarquia de prioridade:
        1. BrightData (se configurado)
        2. ScraperAPI (se configurado - via URL de proxy)
        3. Lista estática (rotação aleatória)
        """
        # Prioridade 1: BrightData
        if settings.BRIGHTDATA_PROXY_URL:
            return settings.BRIGHTDATA_PROXY_URL
        
        # Prioridade 2: ScraperAPI (usando o endpoint de proxy)
        if settings.SCRAPERAPI_KEY:
            return f"http://scraperapi:{settings.SCRAPERAPI_KEY}@proxy-server.scraperapi.com:8001"

        # Prioridade 3: Lista estática
        if settings.ENABLE_PROXY and settings.PROXY_LIST:
            return random.choice(settings.PROXY_LIST)
            
        return None
