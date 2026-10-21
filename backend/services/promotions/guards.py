"""SSRF guards for declarative conditional-promotion probes."""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable
from urllib.parse import urljoin, urlparse


_SENSITIVE_QUERY_PARTS = {
    "access_token", "api_key", "apikey", "auth", "authorization", "cookie",
    "credential", "jwt", "password", "secret", "session", "signature", "token",
}


def _normalize_host(value: str) -> str:
    return value.casefold().rstrip(".")


def _host_allowed(host: str, allowed_host: str | Iterable[str]) -> bool:
    host = _normalize_host(host)
    if isinstance(allowed_host, (list, tuple, set, frozenset)):
        return any(_host_allowed(host, h) for h in allowed_host)
    allowed = _normalize_host(str(allowed_host or ""))
    if not allowed:
        return False
    if host == allowed or host.endswith("." + allowed):
        return True
    if allowed.startswith("www.") and host == allowed[4:]:
        return True
    return False


def _public_addresses(host: str, *, resolve_dns: bool) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve_dns:
            return []
    try:
        addresses = {
            ipaddress.ip_address(entry[4][0])
            for entry in socket.getaddrinfo(host, None)
        }
    except (OSError, ValueError) as exc:
        raise ValueError("promotion target host could not be resolved") from exc
    if not addresses:
        raise ValueError("promotion target host did not resolve")
    return list(addresses)


def validate_probe_path(path: str) -> str:
    """Accept only a secret-free absolute path on the persisted brand origin."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("promotion probe path must start with one slash")
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError("promotion probe path must be relative to the brand origin")
    if not parsed.path.startswith("/") or "\\" in parsed.path:
        raise ValueError("invalid promotion probe path")
    query_names = {
        pair.partition("=")[0].casefold()
        for pair in parsed.query.split("&")
        if pair
    }
    if any(
        sensitive == name or sensitive in name
        for name in query_names
        for sensitive in _SENSITIVE_QUERY_PARTS
    ):
        raise ValueError("promotion probe path cannot contain sensitive query parameters")
    return path


def validate_promotion_target(
    target: str,
    *,
    allowed_host: str | Iterable[str],
    resolve_dns: bool = True,
) -> str:
    """Validate scheme, authority, brand ownership and every resolved IP."""
    try:
        parsed = urlparse(target)
        host = _normalize_host(parsed.hostname or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid promotion target") from exc
    if parsed.scheme != "https":
        raise ValueError("promotion probes require HTTPS")
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError("promotion target cannot contain userinfo")
    if not _host_allowed(host, allowed_host):
        raise ValueError("promotion target host is outside the persisted brand origin")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("local promotion targets are forbidden")
    addresses = _public_addresses(host, resolve_dns=resolve_dns)
    if any(not address.is_global for address in addresses):
        raise ValueError("promotion target resolved to a non-public address")
    return target


def validate_redirect_chain(
    urls: Iterable[str],
    *,
    allowed_host: str,
    resolve_dns: bool = True,
) -> list[str]:
    validated = [
        validate_promotion_target(url, allowed_host=allowed_host, resolve_dns=resolve_dns)
        for url in urls
    ]
    if not validated:
        raise ValueError("redirect chain is empty")
    return validated


def build_probe_endpoint(
    *,
    brand_domain: str,
    path: str,
    resolve_dns: bool = True,
) -> str:
    host = _normalize_host(
        brand_domain.replace("https://", "").replace("http://", "").strip("/")
    )
    safe_path = validate_probe_path(path)
    target = urljoin(f"https://{host}/", safe_path.lstrip("/"))
    return validate_promotion_target(target, allowed_host=host, resolve_dns=resolve_dns)

