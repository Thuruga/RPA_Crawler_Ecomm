"""
Reusable async HTTP client with TLS fingerprint impersonation.

Provides a centralized, hardened HTTP fetcher that bypasses common WAF
protections (Akamai, Cloudflare, PerimeterX) by impersonating real browser
TLS fingerprints via curl_cffi.  Used by engines that previously relied on
Playwright solely for anti-bot evasion, not for JavaScript rendering.

Design decisions:
  - curl_cffi AsyncSession is the primary transport (sub-second latency,
    native TLS impersonation, zero GPU/RAM overhead).
  - BrowserManager (Playwright) is the explicit last-resort fallback,
    gated by a caller-controlled flag.
  - User-Agent rotation uses the project-wide DEFAULT_USER_AGENTS pool.
  - Per-brand proxy support (brand.proxy_url) is respected.
  - Structured logging at every decision point for operational visibility.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from curl_cffi.requests import AsyncSession

from config import DEFAULT_USER_AGENTS, settings

logger = logging.getLogger("HttpClient")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Headers that closely mimic a real Chrome 120 session on Windows.
# Sec-Ch-Ua* headers are required by Akamai/Cloudflare to pass sensor checks.
_CHROME_HEADERS: Dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Ch-Ua": (
        '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"'
    ),
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

# TLS impersonation target — curl_cffi's identifier for Chrome 120 on Windows.
_IMPERSONATE_TARGET = "chrome120"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------

class FetchStrategy(str, Enum):
    """Which transport successfully delivered the response."""
    CURL_CFFI = "curl_cffi"
    PLAYWRIGHT = "playwright"


@dataclass(frozen=True)
class FetchResult:
    """Immutable result of an HTTP fetch attempt."""
    html: str
    status_code: int
    strategy: FetchStrategy
    url: str
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code == 200 and bool(self.html)


@dataclass
class FetchOptions:
    """Caller-controlled knobs for a fetch request."""

    # Timeout for the curl_cffi attempt (seconds).
    timeout_seconds: float = 20.0

    # Extra headers to merge (overrides defaults).
    extra_headers: Dict[str, str] = field(default_factory=dict)

    # Per-brand proxy URL (http://user:pass@host:port).  None = direct.
    proxy_url: Optional[str] = None

    # When True, a failed curl_cffi attempt will fall back to Playwright.
    allow_playwright_fallback: bool = True

    # Playwright-specific settings (only used when falling back).
    playwright_timeout_ms: int = 45000
    playwright_wait_until: str = "domcontentloaded"
    playwright_extra_sleep: float = 2.0
    playwright_wait_selector: Optional[str] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class StealthHttpClient:
    """Async HTTP client with TLS impersonation and graceful degradation.

    Usage::

        result = await StealthHttpClient.fetch("https://www.zara.com/br/pt/...")
        if result.ok:
            html = result.html
    """

    @classmethod
    async def fetch(
        cls,
        url: str,
        options: Optional[FetchOptions] = None,
    ) -> FetchResult:
        """Fetch ``url`` using the hardened transport chain.

        Chain:
          1. curl_cffi with Chrome TLS impersonation (fast, ~200ms).
          2. Playwright via BrowserManager (slow, ~5-15s) — only if
             ``options.allow_playwright_fallback`` is True.
        """
        opts = options or FetchOptions()
        result = await cls._fetch_curl_cffi(url, opts)
        if result.ok:
            return result

        # curl_cffi failed — log and decide whether to escalate.
        logger.warning(
            "[HttpClient] curl_cffi failed for %s (status=%s, error=%s). "
            "allow_playwright_fallback=%s",
            url, result.status_code, result.error, opts.allow_playwright_fallback,
        )

        if not opts.allow_playwright_fallback:
            return result

        if not settings.PLAYWRIGHT_ENABLED:
            logger.warning(
                "[HttpClient] Playwright fallback disabled via PLAYWRIGHT_ENABLED=false "
                "for %s — returning curl_cffi result as-is.",
                url,
            )
            return result

        return await cls._fetch_playwright(url, opts)

    # ---- curl_cffi transport ------------------------------------------------

    @classmethod
    async def _fetch_curl_cffi(
        cls,
        url: str,
        opts: FetchOptions,
    ) -> FetchResult:
        """Primary transport: curl_cffi with Chrome 120 TLS impersonation."""
        import time

        headers = {**_CHROME_HEADERS}
        headers["User-Agent"] = random.choice(DEFAULT_USER_AGENTS)
        if opts.extra_headers:
            headers.update(opts.extra_headers)

        start = time.monotonic()
        try:
            async with AsyncSession(
                impersonate=_IMPERSONATE_TARGET,
                timeout=opts.timeout_seconds,
            ) as session:
                # curl_cffi proxy support: pass as a dict with the protocol key.
                proxies = (
                    {"https": opts.proxy_url, "http": opts.proxy_url}
                    if opts.proxy_url
                    else None
                )
                response = await session.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                )
                elapsed = (time.monotonic() - start) * 1000

                logger.info(
                    "[HttpClient] curl_cffi %s → %s (%dms, %d bytes)",
                    url, response.status_code, int(elapsed), len(response.text or ""),
                )

                return FetchResult(
                    html=response.text or "",
                    status_code=response.status_code,
                    strategy=FetchStrategy.CURL_CFFI,
                    url=url,
                    elapsed_ms=elapsed,
                )

        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "[HttpClient] curl_cffi exception for %s after %dms: %s",
                url, int(elapsed), exc,
            )
            return FetchResult(
                html="",
                status_code=0,
                strategy=FetchStrategy.CURL_CFFI,
                url=url,
                elapsed_ms=elapsed,
                error=str(exc),
            )

    # ---- Playwright transport (last resort) ---------------------------------

    @classmethod
    async def _fetch_playwright(
        cls,
        url: str,
        opts: FetchOptions,
    ) -> FetchResult:
        """Fallback transport: full headless Chromium via BrowserManager."""
        import time

        start = time.monotonic()
        try:
            from core.browser_manager import BrowserManager

            html = await BrowserManager.fetch_html(
                url,
                wait_selector=opts.playwright_wait_selector,
                timeout=opts.playwright_timeout_ms,
                wait_until=opts.playwright_wait_until,
                extra_sleep=opts.playwright_extra_sleep,
                proxy=opts.proxy_url,
            )
            elapsed = (time.monotonic() - start) * 1000

            logger.info(
                "[HttpClient] Playwright %s → 200 (%dms, %d bytes)",
                url, int(elapsed), len(html or ""),
            )

            return FetchResult(
                html=html or "",
                status_code=200 if html else 0,
                strategy=FetchStrategy.PLAYWRIGHT,
                url=url,
                elapsed_ms=elapsed,
            )

        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - start) * 1000
            logger.error(
                "[HttpClient] Playwright exception for %s after %dms: %s",
                url, int(elapsed), exc,
            )
            return FetchResult(
                html="",
                status_code=0,
                strategy=FetchStrategy.PLAYWRIGHT,
                url=url,
                elapsed_ms=elapsed,
                error=str(exc),
            )
