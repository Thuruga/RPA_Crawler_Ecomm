"""
ProxyManager — Gestão Production-Ready de proxies para extração.

Suporte a:
  - Lista local de proxies (PROXY_LIST no .env)
  - BrightData Residential Proxy (BRIGHTDATA_PROXY_URL)
  - ScraperAPI (SCRAPERAPI_KEY) — reformata a URL para usar o gateway
  - Round-robin com blacklist temporária de proxies falhos
  - Sem proxy configurado → retorna None → usa IP local (modo dev)
"""

import logging
import random
import time
from threading import Lock
from typing import Optional

logger = logging.getLogger("ProxyManager")


class ProxyManager:
    """
    Singleton thread-safe para rotação de proxies.

    Hierarquia de prioridade:
      1. BrightData  (enterprise grade, IP residencial)
      2. ScraperAPI  (gateway inteligente com built-in CAPTCHA solving)
      3. Lista local (PROXY_LIST — proxies próprios/datacenter)
      4. Direto      (sem proxy — apenas para dev/testes)
    """

    def __init__(self, settings):
        self._settings = settings
        self._lock = Lock()

        # Proxies locais e estado de saúde
        self._proxy_pool: list[str] = list(settings.PROXY_LIST)
        self._blacklist: dict[str, float] = {}  # proxy_url -> timestamp_expiry
        self._failure_counts: dict[str, int] = {}
        self._round_robin_index: int = 0

        if self._proxy_pool:
            logger.info(f"[ProxyManager] Pool local iniciado com {len(self._proxy_pool)} proxies.")
        if settings.BRIGHTDATA_PROXY_URL:
            logger.info("[ProxyManager] BrightData configurado.")
        if settings.SCRAPERAPI_KEY:
            logger.info("[ProxyManager] ScraperAPI configurado.")

    @property
    def is_configured(self) -> bool:
        """True se qualquer fonte de proxy estiver disponível."""
        return bool(
            self._settings.BRIGHTDATA_PROXY_URL
            or self._settings.SCRAPERAPI_KEY
            or self._proxy_pool
        )

    def get_proxy(self) -> Optional[str]:
        """
        Retorna o próximo proxy disponível ou None se modo direto.
        Thread-safe.
        """
        with self._lock:
            # Prioridade 1: BrightData
            if self._settings.BRIGHTDATA_PROXY_URL:
                return self._settings.BRIGHTDATA_PROXY_URL

            # Prioridade 2: ScraperAPI — retorna None pois a URL é reformatada no SessionFactory
            if self._settings.SCRAPERAPI_KEY:
                return None  # SessionFactory lida com a reformatação

            # Prioridade 3: Pool local com round-robin
            if self._proxy_pool:
                return self._get_local_proxy()

            # Prioridade 4: Sem proxy (modo dev)
            return None

    def _get_local_proxy(self) -> Optional[str]:
        """Round-robin com skip de proxies blacklistados. NÃO é thread-safe (chamado dentro do lock)."""
        now = time.time()
        available = [
            p for p in self._proxy_pool
            if self._blacklist.get(p, 0) < now
        ]

        if not available:
            # Todos blacklistados — reseta a mais antiga e tenta
            if self._proxy_pool:
                oldest = min(self._blacklist.items(), key=lambda x: x[1])[0]
                del self._blacklist[oldest]
                logger.warning(f"[ProxyManager] Todos proxies blacklistados. Resetando '{oldest}'.")
                available = [oldest]
            else:
                return None

        proxy = available[self._round_robin_index % len(available)]
        self._round_robin_index += 1
        return proxy

    def report_failure(self, proxy_url: str) -> None:
        """
        Registra falha em um proxy.
        Após PROXY_FAILURE_THRESHOLD falhas, o proxy é blacklistado
        por PROXY_BLACKLIST_SECONDS.
        """
        if not proxy_url or proxy_url not in self._proxy_pool:
            return

        with self._lock:
            self._failure_counts[proxy_url] = self._failure_counts.get(proxy_url, 0) + 1
            threshold = getattr(self._settings, "PROXY_FAILURE_THRESHOLD", 3)
            blacklist_seconds = getattr(self._settings, "PROXY_BLACKLIST_SECONDS", 300)

            if self._failure_counts[proxy_url] >= threshold:
                expiry = time.time() + blacklist_seconds
                self._blacklist[proxy_url] = expiry
                self._failure_counts[proxy_url] = 0
                logger.warning(
                    f"[ProxyManager] Proxy '{proxy_url}' blacklistado por "
                    f"{blacklist_seconds}s após {threshold} falhas."
                )

    def report_success(self, proxy_url: str) -> None:
        """Reseta o contador de falhas de um proxy após sucesso."""
        if proxy_url and proxy_url in self._failure_counts:
            with self._lock:
                self._failure_counts.pop(proxy_url, None)

    def get_status(self) -> dict:
        """Retorna status atual do pool para o endpoint /health."""
        now = time.time()
        with self._lock:
            return {
                "mode": self._detect_mode(),
                "pool_size": len(self._proxy_pool),
                "blacklisted": sum(1 for exp in self._blacklist.values() if exp > now),
                "available": sum(1 for p in self._proxy_pool if self._blacklist.get(p, 0) < now),
            }

    def _detect_mode(self) -> str:
        if self._settings.BRIGHTDATA_PROXY_URL:
            return "brightdata"
        if self._settings.SCRAPERAPI_KEY:
            return "scraperapi"
        if self._proxy_pool:
            return "local_pool"
        return "direct"


# Instanciado lazy — ver session_factory.py
_instance: Optional[ProxyManager] = None


def get_proxy_manager() -> ProxyManager:
    """Singleton factory. Importa settings aqui para evitar import circular."""
    global _instance
    if _instance is None:
        from config import settings
        _instance = ProxyManager(settings)
    return _instance
