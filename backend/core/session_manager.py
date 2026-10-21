import asyncio
import logging
from typing import Optional
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

logger = logging.getLogger("SessionManager")

class SessionManager:
    """
    Gerenciador de sessões HTTP para reaproveitamento de conexões.
    """
    _session: Optional[AsyncSession] = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_session(cls) -> AsyncSession:
        """Retorna uma sessão compartilhada, criando-a se necessário."""
        if cls._session is None or getattr(cls._session, "closed", False):
            async with cls._lock:
                if cls._session is None or getattr(cls._session, "closed", False):
                    # The desktop development environment can export a dead local
                    # proxy (for example 127.0.0.1:9). Category collection calls
                    # must not inherit it implicitly: a transport failure was being
                    # indistinguishable from an invalid Wake hotsite and caused a
                    # valid Richards collection to be rejected.
                    #
                    # curl_cffi 0.15 records ``trust_env`` but does not apply it
                    # while configuring libcurl.  Set CURLOPT_PROXY explicitly
                    # to prevent libcurl itself from reading HTTP(S)_PROXY.  A
                    # blank proxy means direct transport; callers which require
                    # a brand-specific proxy still pass it on their request.
                    cls._session = AsyncSession(
                        impersonate="chrome120",
                        timeout=30,
                        trust_env=False,
                        curl_options={CurlOpt.PROXY: ""},
                    )
                    logger.info("[SESSION] Nova sessão compartilhada curl_cffi criada.")
        return cls._session

    @classmethod
    async def close_session(cls):
        """Fecha a sessão compartilhada."""
        if cls._session:
            async with cls._lock:
                if cls._session:
                    await cls._session.close()
                    cls._session = None
                    logger.info("[SESSION] Sessão compartilhada fechada.")

session_manager = SessionManager()
