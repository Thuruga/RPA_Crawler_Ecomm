"""
SessionFactory — Fábrica centralizada de sessões HTTP anti-bot.

Responsabilidades:
  - Construir sessões curl_cffi com impersonation de browser real
  - Injetar proxy do ProxyManager (BrightData / ScraperAPI / local / direto)
  - Rotacionar User-Agent de forma aleatória mas consistente
  - Adicionar headers anti-fingerprint completos (Sec-CH-UA, etc.)
  - Reformatar URLs para ScraperAPI quando configurado

Uso:
    async with SessionFactory.create(engine_name="Mercado Livre") as session:
        response = await session.get(url, headers=headers)
"""

import logging
import random
import urllib.parse
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from curl_cffi.requests import AsyncSession

logger = logging.getLogger("SessionFactory")

# Impersonations disponíveis — rotacionados para dificultar fingerprinting
_IMPERSONATIONS = [
    "chrome124",
    "chrome120",
    "chrome119",
    "chrome110",
    "safari17_0",
    "safari15_5",
]

# Pool de User-Agent strings realistas (sincronizados com os impersonations)
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Headers base que simulam um navegador real navegando em HTTPS
_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


def _build_scraperapi_url(original_url: str, api_key: str, render: bool = False) -> str:
    """
    Reformata uma URL para roteá-la via ScraperAPI.
    ScraperAPI atua como proxy inteligente com CAPTCHA solving integrado.
    """
    params = {
        "api_key": api_key,
        "url": original_url,
        "country_code": "br",
        "device_type": "desktop",
    }
    if render:
        params["render"] = "true"
    return "http://api.scraperapi.com/?" + urllib.parse.urlencode(params)


@asynccontextmanager
async def create_session(
    engine_name: str = "engine",
    extra_headers: Optional[dict] = None,
    timeout: Optional[int] = None,
) -> AsyncGenerator[tuple[AsyncSession, Optional[str]], None]:
    """
    Context manager que produz uma sessão HTTP configurada e o proxy ativo.

    Yields:
        (session, active_proxy_url) — proxy_url é None em modo direto.

    Uso:
        async with create_session("MercadoLivre") as (session, proxy):
            response = await session.get(url)
            if response.status_code != 200:
                proxy_manager.report_failure(proxy)
    """
    from config import settings
    from infrastructure.http.proxy_manager import get_proxy_manager

    proxy_manager = get_proxy_manager()
    active_proxy = proxy_manager.get_proxy()

    # Seleciona impersonation e User-Agent aleatórios
    impersonation = random.choice(_IMPERSONATIONS)
    user_agent = random.choice(_USER_AGENTS)

    headers = dict(_BASE_HEADERS)
    headers["User-Agent"] = user_agent

    # Sec-CH-UA sincronizado com User-Agent
    if "Chrome/124" in user_agent:
        headers["Sec-CH-UA"] = '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"Windows"'
    elif "Chrome/120" in user_agent:
        headers["Sec-CH-UA"] = '"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"'
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"Windows"'

    if extra_headers:
        headers.update(extra_headers)

    request_timeout = timeout or settings.REQUEST_TIMEOUT_SECONDS

    session_kwargs = {
        "impersonate": impersonation,
        "timeout": request_timeout,
    }

    # Injeta proxy (exceto ScraperAPI que reformata a URL)
    if active_proxy and settings.SCRAPERAPI_KEY:
        # ScraperAPI mode: sem proxy na sessão, URL reformatada na chamada
        pass
    elif active_proxy:
        session_kwargs["proxies"] = {
            "http": active_proxy,
            "https": active_proxy,
        }

    mode = proxy_manager.get_status()["mode"]
    logger.debug(
        f"[SessionFactory] [{engine_name}] Sessão: impersonation={impersonation} "
        f"mode={mode} proxy={'sim' if active_proxy else 'não'}"
    )

    async with AsyncSession(**session_kwargs) as session:
        yield session, active_proxy


def rewrite_url_for_proxy(url: str, proxy_type: str) -> str:
    """
    Reescreve a URL original para passar pelo gateway de proxy quando necessário.
    Atualmente apenas ScraperAPI requer reformatação de URL.
    """
    from config import settings

    if proxy_type == "scraperapi" and settings.SCRAPERAPI_KEY:
        return _build_scraperapi_url(url, settings.SCRAPERAPI_KEY)
    return url
