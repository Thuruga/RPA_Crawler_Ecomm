import logging
import asyncio
from typing import Optional
from config import settings

logger = logging.getLogger("BrowserManager")


def _to_playwright_proxy(proxy_url: Optional[str]) -> Optional[dict]:
    """Converte ``http://[user:senha@]host:porta`` no dict de proxy do Playwright.

    Retorna ``None`` quando ``proxy_url`` é vazio (modo direto — dev/redes limpas).
    Usado para dar à Lacoste/SFCC um egress de IP limpo sem afetar as demais marcas
    (o proxy é resolvido por marca em ``brand.proxy_url`` — D-09/D-10).
    """
    if not proxy_url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    proxy: dict = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


class BrowserManager:
    """
    Gerenciador Playwright com otimizações para ambientes com pouca RAM.

    Flags aplicadas para reduzir consumo (fonte da verdade: CHROMIUM_ARGS abaixo):
      --disable-gpu            : Sem renderização GPU (desnecessário em headless)
      --disable-extensions     : Sem extensões
      --disable-dev-shm-usage  : Usa /tmp ao invés de /dev/shm (evita crash em containers)
      --no-sandbox             : Necessário em containers Linux sem root
      --disable-background-networking / --disable-sync / --mute-audio / --no-first-run

    NOTA: --single-process e --js-flags=--max-old-space-size=128 foram REMOVIDOS de propósito —
    quebravam o tratamento de redirects JS (ex: proof-of-work do Anubis no Mercado Livre). Logo o
    Chromium aqui é multiprocesso e sem limite de heap V8; em máquinas com pouca RAM
    evite muitas chamadas Playwright concorrentes.

    O browser é fechado após cada uso (não singleton persistente) para liberar RAM
    quando o servidor entrar em idle. Isso causa um delay de ~2s na próxima chamada
    mas evita que o Chromium fique consumindo memória indefinidamente.
    """

    _instance = None
    _browser = None
    _playwright = None
    _lock: asyncio.Lock = None

    CHROMIUM_ARGS = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--mute-audio",
        "--no-first-run",
        "--disable-blink-features=AutomationControlled",
    ]

    @classmethod
    async def _get_lock(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def get_browser(cls):
        if not settings.PLAYWRIGHT_ENABLED:
            raise RuntimeError(
                "Playwright desabilitado via PLAYWRIGHT_ENABLED=false. "
                "Use curl_cffi ou aiohttp como alternativa."
            )

        lock = await cls._get_lock()
        async with lock:
            # Recreate browser if it doesn't exist or if it disconnected/crashed
            if cls._browser is None or not cls._browser.is_connected():
                if cls._playwright is None:
                    from playwright.async_api import async_playwright
                    cls._playwright = await async_playwright().start()
                
                cls._browser = await cls._playwright.chromium.launch(
                    headless=True,
                    args=cls.CHROMIUM_ARGS,
                )
                logger.info("[PLAYWRIGHT] Browser Chromium iniciado (modo low-memory).")
        return cls._browser

    @classmethod
    async def fetch_html(
        cls,
        url: str,
        wait_selector: Optional[str] = None,
        timeout: int = 30000,
        wait_until: str = "domcontentloaded",
        extra_sleep: float = 1.0,
        proxy: Optional[str] = None,
    ) -> str:
        """
        Abre o navegador, navega até a URL e retorna o HTML.
        Utiliza sync_playwright em uma thread separada para evitar problemas de
        NotImplementedError com o SelectorEventLoop do Uvicorn no Windows.
        """
        if not settings.PLAYWRIGHT_ENABLED:
            raise RuntimeError(
                "Playwright desabilitado via PLAYWRIGHT_ENABLED=false. "
                "Use curl_cffi ou aiohttp como alternativa."
            )

        def _sync_fetch():
            from playwright.sync_api import sync_playwright
            import time
            
            with sync_playwright() as p:
                launch_kwargs = {"headless": True, "args": cls.CHROMIUM_ARGS}
                pw_proxy = _to_playwright_proxy(proxy)
                if pw_proxy:
                    launch_kwargs["proxy"] = pw_proxy
                    logger.info("[PLAYWRIGHT] egress via proxy: %s", pw_proxy["server"])
                browser = p.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                    java_script_enabled=True,
                    viewport={"width": 1366, "height": 768},
                    locale="pt-BR",
                    timezone_id="America/Sao_Paulo",
                    extra_http_headers={
                        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                        "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"Windows"',
                    },
                )
                page = context.new_page()

                # Mask headless fingerprint & Akamai sensor telemetry
                page.add_init_script("""
                    // 1. Remove navigator.webdriver flag
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    
                    // 2. Realistic plugin array
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' }
                        ]
                    });

                    // 3. Languages
                    Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US', 'en'] });

                    // 4. Chrome runtime & csi
                    window.chrome = {
                        runtime: {},
                        loadTimes: function() {},
                        csi: function() {},
                        app: {}
                    };

                    // 5. Fake notification permission query
                    const originalQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
                    if (originalQuery) {
                        window.navigator.permissions.query = (parameters) => (
                            parameters.name === 'notifications' ?
                                Promise.resolve({ state: Notification.permission }) :
                                originalQuery(parameters)
                        );
                    }
                """)

                try:
                    logger.info(f"[PLAYWRIGHT] Navegando para {url} (wait_until={wait_until})")
                    page.goto(url, wait_until=wait_until, timeout=timeout)

                    if wait_selector:
                        try:
                            page.wait_for_selector(wait_selector, timeout=min(timeout, 12000))
                        except Exception as sel_err:
                            logger.info(f"[PLAYWRIGHT] wait_selector '{wait_selector}' timeout em {url}: {sel_err}")

                    if extra_sleep > 0:
                        time.sleep(extra_sleep)

                    for attempt in range(3):
                        try:
                            content = page.content()
                            return content
                        except Exception as inner_e:
                            if "navigating" in str(inner_e).lower() and attempt < 2:
                                logger.warning(f"[PLAYWRIGHT] Página {url} navegando, aguardando para capturar HTML... ({attempt+1}/3)")
                                time.sleep(2.0)
                            else:
                                raise inner_e
                    
                    return page.content()
                except Exception as e:
                    logger.error(f"[PLAYWRIGHT] Erro ao buscar {url}: {e}")
                    raise
                finally:
                    page.close()
                    context.close()
                    browser.close()

        # Roda na thread pool para não bloquear o loop principal e garantir
        # que um novo event loop compatível (Proactor) seja criado para o Playwright
        return await asyncio.to_thread(_sync_fetch)

    @classmethod
    async def close(cls):
        """No-op, já que agora o browser é fechado a cada requisição."""
        pass


browser_manager = BrowserManager()
