"""
RetryPolicy — Retry assíncrono com backoff exponencial e jitter.

Características:
  - Configurable via settings (MAX_RETRIES, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS)
  - Jitter aleatório para evitar thundering herd entre engines concorrentes
  - Non-retryable: 404 (estrutura mudou), 403 permanente, 400 (input inválido)
  - Retryable: 429 (rate limit), 503, 502, timeout, erros de rede
  - Logging estruturado de cada tentativa para observabilidade
"""

import asyncio
import logging
import random
from typing import Callable, Awaitable, TypeVar, Any, Optional

logger = logging.getLogger("RetryPolicy")

T = TypeVar("T")

# HTTP status codes que NÃO devem ser retentados
NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 410, 422}

# HTTP status codes que DEVEM ser retentados
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 520, 521, 522, 524}


class RetryExhausted(Exception):
    """Levantado quando todas as tentativas de retry foram esgotadas."""

    def __init__(self, engine: str, attempts: int, last_error: str):
        self.engine = engine
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"[{engine}] Todas as {attempts} tentativas esgotadas. Último erro: {last_error}"
        )


class HTTPStatusError(Exception):
    """Wrapper para erros HTTP com status code."""

    def __init__(self, status_code: int, url: str = ""):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} em '{url}'")


def _calculate_backoff(
    attempt: int,
    base: float,
    cap: float,
    jitter_factor: float = 0.3,
) -> float:
    """
    Full-jitter backoff: min(cap, base * 2^attempt) ± jitter.
    Evita thundering herd quando múltiplos engines retentam ao mesmo tempo.
    """
    exponential = min(cap, base * (2 ** attempt))
    jitter = exponential * jitter_factor * random.random()
    return exponential + jitter


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    engine_name: str,
    max_attempts: Optional[int] = None,
    base_seconds: Optional[float] = None,
    max_seconds: Optional[float] = None,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> T:
    """
    Executa `fn` com retry automático usando backoff exponencial com jitter.

    Args:
        fn: Coroutine factory a ser executada (sem argumentos).
        engine_name: Nome do engine para logging.
        max_attempts: Máximo de tentativas (default: settings.RETRY_MAX_ATTEMPTS).
        base_seconds: Base do backoff em segundos (default: settings.RETRY_BASE_SECONDS).
        max_seconds: Cap máximo do backoff (default: settings.RETRY_MAX_SECONDS).
        on_retry: Callback opcional chamado em cada retry(attempt, exception).

    Returns:
        O resultado de `fn` em caso de sucesso.

    Raises:
        RetryExhausted: Quando todas as tentativas falham.
        Exception: Quando o erro não é retentável (ex: 404).
    """
    from config import settings

    _max = max_attempts or settings.RETRY_MAX_ATTEMPTS
    _base = base_seconds or settings.RETRY_BASE_SECONDS
    _cap = max_seconds or settings.RETRY_MAX_SECONDS

    last_exc: Optional[Exception] = None

    for attempt in range(_max):
        try:
            result = await fn()
            if attempt > 0:
                logger.info(
                    f"[{engine_name}] Sucesso na tentativa {attempt + 1}/{_max}."
                )
            return result

        except HTTPStatusError as e:
            # Erros permanentes — não retentar
            if e.status_code in NON_RETRYABLE_STATUSES:
                logger.warning(
                    f"[{engine_name}] HTTP {e.status_code} — não retentável. Abortando."
                )
                raise

            # Erros temporários — retentar
            last_exc = e
            wait = _calculate_backoff(attempt, _base, _cap)
            logger.warning(
                f"[{engine_name}] HTTP {e.status_code} (tentativa {attempt + 1}/{_max}). "
                f"Aguardando {wait:.1f}s antes de retentar..."
            )

        except (asyncio.TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            wait = _calculate_backoff(attempt, _base, _cap)
            logger.warning(
                f"[{engine_name}] Erro de rede '{type(e).__name__}' "
                f"(tentativa {attempt + 1}/{_max}). Aguardando {wait:.1f}s..."
            )

        except Exception as e:
            # Erro inesperado — não retentar para não mascarar bugs
            logger.error(
                f"[{engine_name}] Erro inesperado '{type(e).__name__}: {e}'. Não retentando."
            )
            raise

        if on_retry:
            try:
                on_retry(attempt + 1, last_exc)
            except Exception as e:
                logger.debug(f"[{engine_name}] Callback on_retry falhou: {e}")

        if attempt < _max - 1:
            await asyncio.sleep(wait)

    raise RetryExhausted(
        engine=engine_name,
        attempts=_max,
        last_error=str(last_exc),
    )
