"""Módulo de autenticação da API OmniPulse.

Fornece autenticação para o portfólio com suporte a:
- Modo aberto de desenvolvimento local (AUTH_ENABLED=False)
- Autenticação via cabeçalho X-API-Key ou Bearer Token
"""

import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from config import settings

logger = logging.getLogger(__name__)


class AuthError(HTTPException):
    """Exceção dedicada de autenticação com formato {error, message}."""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.error = error
        resp_headers = dict(headers or {})
        if status_code == status.HTTP_401_UNAUTHORIZED and "WWW-Authenticate" not in resp_headers:
            resp_headers["WWW-Authenticate"] = "Bearer"
        super().__init__(status_code=status_code, detail=message, headers=resp_headers)


def register_auth_error_handler(app: Any) -> None:
    """Registra o manipulador de AuthError na aplicação FastAPI."""

    async def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            content={"error": exc.error, "message": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    app.add_exception_handler(AuthError, _auth_error_handler)


async def verify_connect_or_internal_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> Dict[str, Any]:
    """Dependência de autenticação: valida API Key ou permite acesso livre local."""
    if not settings.AUTH_ENABLED:
        return {"auth_type": "dev_bypass", "subject": "portfolio_user"}

    # 1. Validação de X-API-Key
    if x_api_key:
        key_bytes = x_api_key.encode("utf-8")
        expected_bytes = (settings.INTERNAL_API_KEY or "").encode("utf-8")
        if expected_bytes and secrets.compare_digest(key_bytes, expected_bytes):
            return {"auth_type": "internal_api_key"}

    # 2. Validação de Bearer Token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        expected = settings.INTERNAL_API_KEY or ""
        if expected and secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return {"auth_type": "bearer_token", "subject": "api_user"}

    raise AuthError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        error="UNAUTHORIZED",
        message="Credenciais de autenticação ausentes ou inválidas.",
    )


async def verify_ws_connect_or_key(
    token: Optional[str] = None,
    api_key: Optional[str] = None,
) -> bool:
    """Validação para conexões WebSocket."""
    if not settings.AUTH_ENABLED:
        return True
    expected = (settings.INTERNAL_API_KEY or "").encode("utf-8")
    if not expected:
        return False
    if api_key and secrets.compare_digest(api_key.encode("utf-8"), expected):
        return True
    if token and secrets.compare_digest(token.encode("utf-8"), expected):
        return True
    return False


def verify_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Função legada de compatibilidade."""
    if not settings.AUTH_ENABLED:
        return
    if not x_api_key or x_api_key != settings.INTERNAL_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing API Key",
        )


def verify_ws_api_key(api_key: Optional[str]) -> bool:
    """Função legada de compatibilidade para WebSockets."""
    if not settings.AUTH_ENABLED:
        return True
    return bool(api_key and api_key == settings.INTERNAL_API_KEY)
