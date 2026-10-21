"""Controlled, opt-in audit of the three marketplace integrations.

The module is intentionally hermetic on import.  Network-facing dependencies are
loaded only by ``run_live`` and the CLI defaults to a dry-run that only prints the
validated matrix.  The pure classifier and orchestration helpers are also used by
the offline test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import random
from decimal import Decimal, InvalidOperation
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.phase10_acceptance import (
    FreightGateResult,
    FreightMatrixRow,
    SearchAcceptanceEvidence,
    StrictSearchGateResult,
    sanitize_phase10_evidence,
    validate_freight_gate_results,
    validate_phase10_report_payload,
    validate_search_acceptance_evidence,
    validate_strict_final_products,
)

DEFAULT_MANIFEST = BACKEND_DIR / "data" / "audits" / "marketplaces" / "acceptance_manifest.json"
DEFAULT_OUTPUT = BACKEND_DIR / "data" / "audits" / "marketplaces"
PHASE10_COMPANION_JSON = "phase10_live_report.json"
PHASE10_COMPANION_MARKDOWN = "phase10_live_report.md"

MARKETPLACE_DOMAINS: dict[str, tuple[str, ...]] = {
    "amazon": ("amazon.com.br",),
    "mercado_livre": ("mercadolivre.com.br", "mercadolibre.com"),
    "netshoes": ("netshoes.com.br",),
}
ALLOWED_PROFILES = ("current", "coherent_http")
ALLOWED_JOURNEYS = ("search", "pdp", "shipping")
JOB_EVIDENCE_TYPES = ("search", "cross", "category_scan", "price_check")
SEARCH_JOB_EVIDENCE_TYPES = {"search", "cross"}
JOB_SUCCESS_STATUSES = {
    "search": {"completed"},
    "cross": {"completed"},
    "category_scan": {"completed"},
    "price_check": {"completed", "ok"},
}
JOB_HONEST_FAILURE_STATUSES = {
    "category_scan": {"failed", "interrupted"},
    "price_check": {"blocked", "error", "failed", "interrupted", "stale"},
}
BLOCKED_HTTP = {403, 429, 503}
CHALLENGE_RE = re.compile(
    r"(?:http\s*)?(?:403|429|503)\b|captcha|anubis|access\s+denied|validatecaptcha|verify\s+(?:you are|that you are)\s+human|"
    r"unusual\s+traffic|login\s+(?:wall|required)|fa[cç]a\s+login\s+para\s+continuar",
    re.IGNORECASE,
)
SECRET_KEY_RE = re.compile(
    r"authorization|cookie|set-cookie|api[-_]?key|token|secret|password|passwd|proxy|html|body",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(api[-_]?key|token|secret|password|passwd|cookie|authorization)=([^\s&;]+)"
)
HTML_RE = re.compile(r"<\s*(?:!doctype|html|head|body|script|form)\b", re.IGNORECASE)


class AuditConfigurationError(ValueError):
    """The operator supplied an unsafe or inconsistent audit configuration."""


class CallBudgetExceeded(RuntimeError):
    """The manifest's external-call ceiling was reached."""


@dataclass(frozen=True)
class BrowserFingerprint:
    name: str
    impersonation: str
    user_agent: str
    sec_ch_ua: str
    platform: str

    @property
    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": self.platform,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }


FINGERPRINTS: tuple[BrowserFingerprint, ...] = (
    BrowserFingerprint(
        name="chrome120-windows",
        impersonation="chrome120",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        sec_ch_ua='"Chromium";v="120", "Google Chrome";v="120", "Not-A.Brand";v="99"',
        platform='"Windows"',
    ),
)


@dataclass
class RawObservation:
    payload: Any = None
    http_status: int | None = None
    body_sample: str = ""
    error: str | BaseException | None = None
    cache_state: str = "fresh"
    external_calls: int = 1
    tier: str = "http"
    unsupported: bool = False
    timed_out: bool = False
    legitimate_empty: bool = False
    fallback_used: bool = False


@dataclass
class Classification:
    status: str
    reason: str
    valid_items: int = 0
    selected_url: str | None = None


@dataclass
class Attempt:
    marketplace: str
    profile: str
    journey: str
    tier: str
    status: str
    reason: str
    http_status: int | None = None
    challenge_detected: bool = False
    valid_items: int = 0
    duration_ms: int = 0
    external_calls: int = 0
    fallback_used: bool = False
    cache_state: str = "fresh"
    selected_origin: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class JourneyResult:
    marketplace: str
    profile: str
    journey: str
    status: str
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class MarketplaceVerdict:
    marketplace: str
    profile: str
    status: str
    successful_journeys: list[str]
    failed_journeys: dict[str, str]
    external_calls: int


@dataclass
class AuditReport:
    schema_version: int
    audit_id: str
    generated_at: str
    live: bool
    fingerprint: str | None
    manifest: dict[str, Any]
    commands: list[str]
    results: list[JourneyResult]
    search_gates: list[StrictSearchGateResult]
    freight_gate: FreightGateResult
    evidence: dict[str, Any]
    verdicts: list[MarketplaceVerdict]
    requirement_verdicts: dict[str, str]
    total_external_calls: int

    def to_dict(self) -> dict[str, Any]:
        payload = sanitize_phase10_evidence(sanitize_value(asdict(self)))
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
        payload["job_evidence"] = evidence.get("durable_jobs")
        payload["frontend_reconnect"] = evidence.get("frontend_reconnect")
        return payload


class AuditAdapter(Protocol):
    async def execute(
        self,
        journey: str,
        *,
        query: str,
        product_url: str | None,
        zipcode: str,
        max_results: int,
        timeout_seconds: float,
    ) -> RawObservation: ...


def sanitize_url(value: str) -> str:
    """Remove credentials, query strings and fragments from a URL."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED URL]"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return value
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def sanitize_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")
    if HTML_RE.search(text):
        return "[HTML OMITTED]"
    text = BEARER_RE.sub("[REDACTED CREDENTIAL]", text)
    text = KEY_VALUE_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = re.sub(
        r"https?://[^\s<>'\"]+",
        lambda match: sanitize_url(match.group(0).rstrip(".,);")),
        text,
    )
    return text[:limit]


def sanitize_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            cleaned[name] = "[REDACTED]" if SECRET_KEY_RE.search(name) else sanitize_value(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, BaseException):
        return sanitize_text(f"{type(value).__name__}: {value}")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if is_dataclass(value):
        return asdict(value)
    return {}


def _positive_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return amount.is_finite() and amount > 0

def _field(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = data.get(name)
        if value is not None:
            return value
    return None


def _valid_https_url(url: Any, expected_domains: Sequence[str]) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == domain or host.endswith(f".{domain}") for domain in expected_domains
    )


def _challenge_detected(observation: RawObservation) -> bool:
    candidates = [observation.body_sample]
    if observation.error:
        candidates.append(str(observation.error))
    payload = _as_mapping(observation.payload)
    candidates.extend(str(payload.get(key, "")) for key in ("error", "message", "title"))
    return any(CHALLENGE_RE.search(candidate or "") for candidate in candidates)


def _classify_search(payload: Any, expected_domains: Sequence[str]) -> Classification:
    data = _as_mapping(payload)
    raw_products = data.get("products", payload if isinstance(payload, list) else [])
    products = raw_products if isinstance(raw_products, list) else []
    valid: list[dict[str, Any]] = []
    for item in products:
        product = _as_mapping(item)
        name = _field(product, "product_name", "name", "title", "titulo", "raw_title")
        url = _field(product, "url", "product_url")
        price = _field(product, "price_full", "price", "preco", "price_discount")
        if str(name or "").strip() and _valid_https_url(url, expected_domains) and _positive_number(price):
            valid.append(product)
    if not valid:
        return Classification("parser_failure", "no valid search product", 0)
    selected = str(_field(valid[0], "url", "product_url"))
    return Classification("fresh_success", "valid fresh search payload", len(valid), selected)


def _classify_pdp(payload: Any, expected_domains: Sequence[str]) -> Classification:
    del expected_domains
    data = _as_mapping(payload)
    identity = _field(data, "product_code", "id", "item_id", "product_name", "name", "raw_title", "title")
    price = _field(data, "price_full", "price", "preco", "price_discount")
    if str(identity or "").strip() and _positive_number(price):
        return Classification("fresh_success", "valid fresh PDP payload", 1)
    return Classification("parser_failure", "PDP identity or positive price missing", 0)


def _classify_shipping(payload: Any, expected_domains: Sequence[str]) -> Classification:
    del expected_domains
    data = _as_mapping(payload)
    explicit_free = data.get("is_free_shipping") is True or data.get("free") is True
    status = str(_field(data, "status", "shipping_status") or "").strip().casefold()
    available = status in {"available", "disponível", "disponivel"}
    price = _field(data, "shipping_price", "price", "cost")
    if explicit_free:
        return Classification("fresh_success", "explicit free shipping", 1)
    if available and _positive_number(price):
        return Classification("fresh_success", "available shipping with positive price", 1)
    return Classification("parser_failure", "shipping is neither explicit free nor available with price", 0)


def classify_observation(
    journey: str,
    observation: RawObservation,
    expected_domains: Sequence[str],
) -> Classification:
    """Classify one observation without performing I/O."""

    if journey not in ALLOWED_JOURNEYS:
        raise AuditConfigurationError(f"unknown journey: {journey}")
    if observation.unsupported:
        return Classification("unsupported", "journey unsupported")
    if observation.timed_out or isinstance(observation.error, (TimeoutError, asyncio.TimeoutError)):
        return Classification("timeout", "request timed out")
    if observation.cache_state == "stale":
        return Classification("stale", "cached stale value is not live evidence")
    challenge = _challenge_detected(observation)
    if observation.http_status in BLOCKED_HTTP or challenge:
        reason = f"HTTP {observation.http_status}" if observation.http_status in BLOCKED_HTTP else "challenge detected"
        return Classification("blocked", reason)
    if observation.error is not None:
        return Classification("network_error", sanitize_text(observation.error))
    if observation.external_calls < 1:
        return Classification("not_fresh", "no external call in this execution")
    if observation.legitimate_empty:
        return Classification("legitimate_empty", "source explicitly reported an empty result")
    classifier = {
        "search": _classify_search,
        "pdp": _classify_pdp,
        "shipping": _classify_shipping,
    }[journey]
    return classifier(observation.payload, expected_domains)


class ExternalCallBudget:
    """Atomic global/per-domain call budget with concurrency fixed at one."""

    def __init__(self, maximum: int, per_domain: int | Mapping[str, int]) -> None:
        if maximum < 0:
            raise AuditConfigurationError("maximum external calls cannot be negative")
        self.maximum = maximum
        self.per_domain = per_domain
        self.total = 0
        self.domain_totals: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._single_flight = asyncio.Semaphore(1)

    def _domain_limit(self, domain: str) -> int:
        if isinstance(self.per_domain, Mapping):
            return int(self.per_domain.get(domain, 0))
        return int(self.per_domain)

    async def reserve(self, domain: str, calls: int = 1) -> None:
        if calls < 1:
            raise AuditConfigurationError("reserved calls must be positive")
        async with self._lock:
            domain_count = self.domain_totals.get(domain, 0)
            if self.total + calls > self.maximum:
                raise CallBudgetExceeded("global external-call budget exhausted")
            if domain_count + calls > self._domain_limit(domain):
                raise CallBudgetExceeded(f"external-call budget exhausted for {domain}")
            self.total += calls
            self.domain_totals[domain] = domain_count + calls

    async def __aenter__(self) -> "ExternalCallBudget":
        await self._single_flight.acquire()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self._single_flight.release()


class DomainThrottle:
    def __init__(
        self,
        minimum_seconds: float,
        maximum_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if minimum_seconds < 0 or maximum_seconds < minimum_seconds:
            raise AuditConfigurationError("invalid cooldown interval")
        self.minimum_seconds = minimum_seconds
        self.maximum_seconds = maximum_seconds
        self.clock = clock
        self.sleeper = sleeper
        self.jitter = jitter
        self._next_allowed: dict[str, float] = {}

    async def wait(self, domain: str) -> None:
        now = self.clock()
        delay = max(0.0, self._next_allowed.get(domain, now) - now)
        if delay:
            await self.sleeper(delay)
        self._next_allowed[domain] = self.clock() + self.jitter(
            self.minimum_seconds, self.maximum_seconds
        )

def select_journey_product_url(
    marketplace: str,
    profile: str,
    journey: str,
    product_urls: Mapping[tuple[str, str], str],
    manifest: Mapping[str, Any]
) -> tuple[str | None, str]:
    """
    Returns the URL to use for the journey, and the source string.
    If search provided a URL, use it ('search').
    Otherwise, if the manifest has a seed URL for this journey, use it ('seed').
    Otherwise, return None, 'none'.
    """
    prior = product_urls.get((marketplace, profile))
    if prior:
        return prior, "search"
        
    seed_urls = manifest.get("journey_seed_urls", {})
    marketplace_seeds = seed_urls.get(marketplace, {})
    seed = marketplace_seeds.get(journey)
    
    if seed:
        return seed, "seed"
        
    return None, "none"

def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "marketplaces", "query", "zipcode", "max_results", "timeout_seconds",
        "cooldown_seconds", "max_external_calls", "max_calls_per_marketplace",
        "search_queries"
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise AuditConfigurationError(f"manifest missing: {', '.join(missing)}")
    marketplaces = payload["marketplaces"]
    if sorted(marketplaces) != sorted(MARKETPLACE_DOMAINS):
        raise AuditConfigurationError("manifest marketplaces must be the closed supported set")
    if payload["zipcode"] != "01310100":
        raise AuditConfigurationError("zipcode must be exactly 01310100")
    if int(payload["max_results"]) < 1 or int(payload["max_external_calls"]) < 1:
        raise AuditConfigurationError("manifest limits must be positive")
    if int(payload["max_results"]) > 10 or int(payload["max_external_calls"]) > 36 or int(payload.get("max_calls_per_marketplace", 0)) > 12:
        raise AuditConfigurationError("manifest limits exceed Phase 10 moderate budget")
    if float(payload.get("timeout_seconds", 0)) > 45:
        raise AuditConfigurationError("manifest limits exceed Phase 10 moderate budget")
    cooldown = payload["cooldown_seconds"]
    if float(cooldown["min"]) < 3.0 or float(cooldown["max"]) > 6.0 or float(cooldown["max"]) < float(cooldown["min"]):
        raise AuditConfigurationError("invalid manifest cooldown")
    if "generic_camiseta_masculina" not in payload["search_queries"] or "business_aramis_camisa_masculina" not in payload["search_queries"]:
        raise AuditConfigurationError("manifest search_queries missing required phases")
        
    seed_urls = payload.get("journey_seed_urls")
    if seed_urls is not None:
        from urllib.parse import urlsplit
        for mk, journeys in seed_urls.items():
            if mk not in marketplaces:
                raise AuditConfigurationError(f"seed URL marketplace {mk} not supported")
            for jny, url in journeys.items():
                if jny not in {"pdp", "shipping"}:
                    raise AuditConfigurationError(f"seed URL journey {jny} not supported")
                parsed = urlsplit(url)
                if parsed.scheme != "https":
                    raise AuditConfigurationError("seed URLs must be https")
                if parsed.query:
                    raise AuditConfigurationError("seed URLs must not contain query strings")
                host = parsed.hostname or ""
                allowed = MARKETPLACE_DOMAINS.get(mk, [])
                if not any(host == allowed_host or host.endswith(f".{allowed_host}") for allowed_host in allowed):
                    raise AuditConfigurationError(f"seed URL host {host} not allowed for marketplace {mk}")
                    
    return payload


def _contains_sensitive_job_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                return True
            if _contains_sensitive_job_material(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_job_material(item) for item in value)
    if not isinstance(value, str):
        return False
    return (
        HTML_RE.search(value) is not None
        or BEARER_RE.search(value) is not None
        or KEY_VALUE_SECRET_RE.search(value) is not None
        or re.search(r"https?://[^/\s]+:[^@\s]+@", value) is not None
    )


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuditConfigurationError(f"{label} must be an object")
    return value


def _require_fields(section: Mapping[str, Any], fields: Sequence[str], label: str) -> None:
    for field_name in fields:
        if field_name not in section or section[field_name] in {None, ""}:
            raise AuditConfigurationError(f"missing restart field: {label}.{field_name}")


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().casefold()


def _duplicate_count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AuditConfigurationError("duplicate_count must be an integer") from None


def validate_job_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Phase 10 durable job and restart/reconnect evidence."""

    evidence = _require_mapping(evidence, "job evidence")
    if _contains_sensitive_job_material(evidence):
        raise AuditConfigurationError("job evidence contains sensitive material")
    if evidence.get("schema_version") != 1:
        raise AuditConfigurationError("job evidence schema_version must be 1")
    if evidence.get("restart_job_kind") != "search":
        raise AuditConfigurationError("restart job kind must be search")
    _require_fields(
        evidence,
        ["backend_restart_command", "restart_started_at", "restart_completed_at"],
        "job evidence",
    )

    pre_restart = _require_mapping(evidence.get("pre_restart"), "pre_restart")
    post_restart = _require_mapping(evidence.get("post_restart"), "post_restart")
    duplicate_check = _require_mapping(evidence.get("duplicate_check"), "duplicate_check")
    _require_fields(pre_restart, ["job_id", "status", "observed_at"], "pre_restart")
    _require_fields(post_restart, ["job_id", "status", "observed_at"], "post_restart")
    _require_fields(duplicate_check, ["job_id", "duplicate_count", "disappeared"], "duplicate_check")
    if str(pre_restart["job_id"]) != str(post_restart["job_id"]):
        raise AuditConfigurationError("restart pre/post job id mismatch")
    if str(post_restart["job_id"]) != str(duplicate_check["job_id"]):
        raise AuditConfigurationError("restart duplicate check job id mismatch")
    if _normalized_status(post_restart["status"]) != "completed":
        raise AuditConfigurationError("search restart post status must complete")
    if _duplicate_count(duplicate_check["duplicate_count"]) != 0 or duplicate_check.get("disappeared") is not False:
        raise AuditConfigurationError("restart duplicate check failed")

    job_types = _require_mapping(evidence.get("job_types"), "job_types")
    missing = sorted(set(JOB_EVIDENCE_TYPES) - set(job_types))
    if missing:
        raise AuditConfigurationError(f"missing durable job evidence: {', '.join(missing)}")
    extra = sorted(set(job_types) - set(JOB_EVIDENCE_TYPES))
    if extra:
        raise AuditConfigurationError(f"unknown durable job evidence: {', '.join(extra)}")

    for kind in JOB_EVIDENCE_TYPES:
        snapshot = _require_mapping(job_types[kind], kind)
        _require_fields(snapshot, ["job_id", "status", "observed_after_restart", "duplicate_count", "disappeared"], kind)
        if snapshot.get("observed_after_restart") is not True:
            raise AuditConfigurationError(f"{kind} was not observed after restart")
        if snapshot.get("disappeared") is not False:
            raise AuditConfigurationError(f"{kind} disappeared after restart")
        if _duplicate_count(snapshot.get("duplicate_count")) != 0:
            raise AuditConfigurationError(f"{kind} has duplicate evidence")

        status = _normalized_status(snapshot.get("status"))
        if kind in SEARCH_JOB_EVIDENCE_TYPES and status != "completed":
            raise AuditConfigurationError(f"{kind} must complete")
        elif kind not in SEARCH_JOB_EVIDENCE_TYPES:
            if status in JOB_SUCCESS_STATUSES[kind]:
                continue
            if status in JOB_HONEST_FAILURE_STATUSES[kind] and str(snapshot.get("failure_reason") or "").strip():
                continue
            raise AuditConfigurationError(f"{kind} requires completed or honest terminal failure")

    frontend = _require_mapping(evidence.get("frontend_reconnect"), "frontend_reconnect")
    _require_fields(
        frontend,
        ["checklist_status", "observed_route", "reloaded", "job_still_visible", "duplicate_visible"],
        "frontend_reconnect",
    )
    return sanitize_phase10_evidence(dict(evidence))

def load_freight_acceptance_matrix(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise AuditConfigurationError("freight matrix schema version must be 1")
    integrations = payload.get("integrations", {})
    expected_integrations = {"vtex", "shopify", "wake", "mercado_livre", "amazon", "netshoes"}
    missing = sorted(expected_integrations - integrations.keys())
    if missing:
        raise AuditConfigurationError(f"freight matrix missing integrations: {', '.join(missing)}")
    for integration, data in integrations.items():
        url = data.get("product_url", "")
        if not url.startswith("https://"):
            raise AuditConfigurationError(f"freight matrix url for {integration} must be https")
        if "?" in url:
            raise AuditConfigurationError(f"freight matrix url for {integration} must not contain query strings")
    expected_ceps = ["01310100", "90010150", "70040010", "40020000", "69010001"]
    if payload.get("ceps") != expected_ceps:
        raise AuditConfigurationError("freight matrix ceps do not match expected Phase 10 regions")
    return payload


def _strict_product_candidates(payload: Any, marketplace: str) -> list[dict[str, Any]]:
    data = _as_mapping(payload)
    raw_products = data.get("products", payload if isinstance(payload, list) else [])
    products = raw_products if isinstance(raw_products, list) else []
    candidates: list[dict[str, Any]] = []
    for raw in products:
        product = _as_mapping(raw)
        if not product:
            continue
        candidate = dict(product)
        candidate.setdefault("marketplace", marketplace)
        candidates.append(sanitize_phase10_evidence(candidate))
    return candidates


def _manifest_query_spec(manifest: Mapping[str, Any], query_id: str) -> tuple[str, int, float | None]:
    search_queries = _require_mapping(manifest.get("search_queries"), "search_queries")
    raw_spec = search_queries.get(query_id, {})
    spec = raw_spec if isinstance(raw_spec, Mapping) else {"strict_products": raw_spec}
    required_count = int(spec.get("strict_products") or 0)
    min_score = spec.get("min_score")
    query = str(spec.get("query") or "")
    if not query:
        query = (
            str(manifest.get("query") or "")
            if query_id == "generic_camiseta_masculina"
            else query_id.replace("business_", "").replace("_", " ")
        )
    return query, required_count, float(min_score) if min_score is not None else None


def build_search_gate_results(
    manifest: Mapping[str, Any],
    strict_products: Sequence[Mapping[str, Any]],
) -> list[StrictSearchGateResult]:
    expected_origins = sorted({domain for domains in MARKETPLACE_DOMAINS.values() for domain in domains})
    gates: list[StrictSearchGateResult] = []
    for query_id in ("generic_camiseta_masculina", "business_aramis_camisa_masculina"):
        query, required_count, min_score = _manifest_query_spec(manifest, query_id)
        products = list(strict_products) if query_id == "generic_camiseta_masculina" else []
        gates.append(
            validate_strict_final_products(
                products,
                query_id=query_id,
                query=query,
                expected_origins=expected_origins,
                required_count=required_count,
                min_score=min_score,
            )
        )
    return gates


def build_freight_gate_results(matrix: Mapping[str, Any]) -> FreightGateResult:
    rows: list[FreightMatrixRow] = []
    integrations = _require_mapping(matrix.get("integrations"), "freight integrations")
    for integration, config in sorted(integrations.items()):
        product_url = str(_require_mapping(config, f"freight integration {integration}").get("product_url") or "")
        for zipcode in matrix.get("ceps", []):
            rows.append(
                FreightMatrixRow(
                    integration=str(integration),
                    state="not_reached",
                    product_url=product_url,
                    zipcode=str(zipcode),
                    shipping_price=None,
                    is_free_shipping=False,
                    evidence={
                        "source": "phase10_live_runner",
                        "reason": "freight_matrix_quote_not_executed",
                    },
                )
            )
    return validate_freight_gate_results(rows, expected_ceps=[str(zipcode) for zipcode in matrix.get("ceps", [])])
    
async def quote_freight_matrix(integration: str, config: dict, zipcode: str) -> dict:
    import sys
    from pathlib import Path
    SCRIPT_DIR = Path(__file__).resolve().parent
    BACKEND_DIR = SCRIPT_DIR.parent
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
        
    from services.brand_service import brand_service
    from services.shipping.resolver import resolve_shipping_provider
    from services.shipping.base import apply_shipping_calculation
    from services.engines.factory import EngineFactory
    
    product_url = config.get("product_url", "")
    state = "not_reached"
    shipping_price = None
    is_free_shipping = False
    evidence = {"source_endpoint": "audit_runner"}
    
    try:
        if integration == "vtex":
            from services.vtex_api_scraper import VtexApiClient
            brand_key = config.get("brand_key")
            sku_id = config.get("sku_id")
            seller_id = config.get("seller_id", "1")
            
            result = await VtexApiClient.calculate_for_brand(
                brand_key=brand_key.lower() if brand_key else "aramis",
                sku_id=sku_id,
                seller_id=seller_id,
                zipcode=zipcode
            )
            
            if result.get("error"):
                state = "error"
                evidence["error"] = result["error"]
            else:
                options = result.get("shipping_options", [])
                if options:
                    best = min(options, key=lambda x: getattr(x, "price", getattr(x, "shipping_price", float('inf'))))
                    price = float(getattr(best, "price", getattr(best, "shipping_price", 0)))
                    if price == 0:
                        is_free_shipping = True
                    shipping_price = str(price)
                    state = result.get("state", "available")
                else:
                    state = result.get("state", "unsupported")
                    evidence["reason"] = "no_options"
                    
        elif integration in ("shopify", "wake"):
            brand_key = config.get("brand_key")
            brand = brand_service.get_brand(brand_key)
            from services.shipping.base import is_url_allowed_for_brand
            if not is_url_allowed_for_brand(product_url, brand):
                raise ValueError("URL not allowed for brand")
            
            provider = resolve_shipping_provider(brand)
            from services.models import SearchProductResult
            product = SearchProductResult(
                brand=brand_key,
                product_name="Produto",
                url=product_url,
                price_full=None
            )
            raw = await provider.calculate(product, zipcode, brand)
            apply_shipping_calculation(product, raw)
            
            if raw.state in ("error", "temporary_failure"):
                state = raw.state
                evidence["error"] = raw.message
            else:
                price_float = product.shipping_price
                is_free = product.is_free_shipping
                if is_free:
                    state = "available"
                    is_free_shipping = True
                    shipping_price = "0.00"
                elif price_float is not None:
                    state = "available"
                    shipping_price = str(price_float)
                else:
                    state = raw.state or "unsupported"
                    
        elif integration in ("mercado_livre", "amazon", "netshoes"):
            engine_name = config.get("engine") or integration
            engine = EngineFactory.get_engine(engine_name)
            result = await engine.calculate_shipping_advanced(product_url, zipcode)
            if not result:
                state = "unsupported"
            elif result.get("error"):
                state = "error"
                evidence["error"] = result["error"]
            else:
                price_float = result.get("shipping_price")
                is_free = result.get("is_free_shipping", False)
                if is_free:
                    state = "available"
                    is_free_shipping = True
                    shipping_price = "0.00"
                elif price_float is not None:
                    state = "available"
                    shipping_price = str(price_float)
                else:
                    state = "unsupported"
    except Exception as e:
        state = "error"
        evidence["error"] = str(e)
        
    return {
        "integration": integration,
        "state": state,
        "product_url": product_url,
        "zipcode": zipcode,
        "shipping_price": shipping_price,
        "is_free_shipping": is_free_shipping,
        "evidence": evidence
    }

async def run_freight_matrix(matrix: Mapping[str, Any], quoter=quote_freight_matrix) -> list[FreightMatrixRow]:
    rows: list[FreightMatrixRow] = []
    integrations = _require_mapping(matrix.get("integrations"), "freight integrations")
    for integration, config in sorted(integrations.items()):
        for zipcode in matrix.get("ceps", []):
            res = await quoter(integration, config, str(zipcode))
            rows.append(
                FreightMatrixRow(
                    integration=res["integration"],
                    state=res["state"],
                    product_url=res["product_url"],
                    zipcode=res["zipcode"],
                    shipping_price=res["shipping_price"],
                    is_free_shipping=res["is_free_shipping"],
                    evidence=res["evidence"],
                )
            )
    return rows


def _selected_origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.hostname}" if parsed.scheme and parsed.hostname else None


async def _invoke_adapter(
    adapter: AuditAdapter,
    journey: str,
    *,
    manifest: Mapping[str, Any],
    product_url: str | None,
) -> RawObservation:
    return await adapter.execute(
        journey,
        query=str(manifest["query"]),
        product_url=product_url,
        zipcode=str(manifest["zipcode"]),
        max_results=int(manifest["max_results"]),
        timeout_seconds=float(manifest["timeout_seconds"]),
    )


async def run_matrix(
    manifest: Mapping[str, Any],
    adapters: Mapping[tuple[str, str], AuditAdapter],
    *,
    marketplaces: Sequence[str],
    profiles: Sequence[str],
    journeys: Sequence[str],
    budget: ExternalCallBudget,
    throttle: DomainThrottle,
    search_products: list[dict[str, Any]] | None = None,
) -> list[JourneyResult]:
    """Run a deterministic sequential matrix using real or fake adapters."""

    results: list[JourneyResult] = []
    product_urls: dict[tuple[str, str], str] = {}
    for marketplace in marketplaces:
        domain = MARKETPLACE_DOMAINS[marketplace][0]
        for profile in profiles:
            adapter = adapters[(profile, marketplace)]
            for journey in journeys:
                prior_url, url_source = select_journey_product_url(
                    marketplace, profile, journey, product_urls, manifest
                )
                if journey in {"pdp", "shipping"} and not prior_url:
                    results.append(JourneyResult(
                        marketplace, profile, journey, "not_reached",
                        [Attempt(
                            marketplace, profile, journey,
                            "http",
                            "not_reached", "fresh search did not provide a product URL",
                            external_calls=0,
                        )],
                    ))
                    continue

                started = time.monotonic()
                try:
                    async with budget:
                        await budget.reserve(domain)
                        await throttle.wait(domain)
                        observation = await _invoke_adapter(
                            adapter, journey, manifest=manifest, product_url=prior_url
                        )
                        if journey == "search" and search_products is not None:
                            search_products.extend(_strict_product_candidates(observation.payload, marketplace))
                except CallBudgetExceeded as exc:
                    attempt = Attempt(
                        marketplace, profile, journey, "none", "budget_exhausted",
                        sanitize_text(exc), external_calls=0,
                        fallback_used=False,
                    )
                    results.append(JourneyResult(marketplace, profile, journey, attempt.status, [attempt]))
                    continue
                except (TimeoutError, asyncio.TimeoutError) as exc:
                    observation = RawObservation(error=exc, timed_out=True)
                except Exception as exc:  # live engines must not abort the full matrix
                    observation = RawObservation(error=exc)

                classified = classify_observation(
                    journey, observation, MARKETPLACE_DOMAINS[marketplace]
                )
                attempt = Attempt(
                    marketplace=marketplace,
                    profile=profile,
                    journey=journey,
                    tier=observation.tier,
                    status=classified.status,
                    reason=sanitize_text(classified.reason),
                    http_status=observation.http_status,
                    challenge_detected=_challenge_detected(observation),
                    valid_items=classified.valid_items,
                    duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                    external_calls=max(1, observation.external_calls),
                    fallback_used=observation.fallback_used,
                    cache_state=observation.cache_state,
                    selected_origin=_selected_origin(classified.selected_url),
                    evidence={
                        "query": manifest.get("query"),
                        "product_url_source": url_source if journey in {"pdp", "shipping"} else None,
                        "seed_url_used": (url_source == "seed") if journey in {"pdp", "shipping"} else False,
                    },
                )
                results.append(JourneyResult(marketplace, profile, journey, classified.status, [attempt]))
                if journey == "search" and classified.status == "fresh_success" and classified.selected_url:
                    product_urls[(marketplace, profile)] = classified.selected_url
    return results


def derive_verdicts(results: Sequence[JourneyResult]) -> list[MarketplaceVerdict]:
    grouped: dict[tuple[str, str], dict[str, JourneyResult]] = {}
    for result in results:
        grouped.setdefault((result.marketplace, result.profile), {})[result.journey] = result
    verdicts: list[MarketplaceVerdict] = []
    for (marketplace, profile), rows in sorted(grouped.items()):
        successes = sorted(journey for journey, row in rows.items() if row.status == "fresh_success")
        failures = {journey: row.status for journey, row in sorted(rows.items()) if row.status != "fresh_success"}
        all_success = set(successes) == set(ALLOWED_JOURNEYS)
        verdicts.append(MarketplaceVerdict(
            marketplace=marketplace,
            profile=profile,
            status="circumvented_in_test_scope" if all_success else "not_circumvented",
            successful_journeys=successes,
            failed_journeys=failures,
            external_calls=sum(attempt.external_calls for row in rows.values() for attempt in row.attempts),
        ))
    return verdicts


def _build_phase10_evidence(
    job_evidence: Mapping[str, Any] | None = None,
    frontend_checklist: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    durable_jobs = validate_job_evidence(job_evidence) if job_evidence is not None else None
    if frontend_checklist is not None and _contains_sensitive_job_material(frontend_checklist):
        raise AuditConfigurationError("frontend checklist contains sensitive material")

    frontend_reconnect: Any = None
    if durable_jobs is not None:
        frontend_reconnect = dict(_require_mapping(durable_jobs.get("frontend_reconnect"), "frontend_reconnect"))
    if frontend_checklist is not None:
        merged = dict(frontend_reconnect or {})
        merged.update(sanitize_phase10_evidence(dict(frontend_checklist)))
        frontend_reconnect = merged

    return {
        "durable_jobs": durable_jobs,
        "frontend_reconnect": frontend_reconnect,
    }


def _frontend_reconnect_passed(frontend: Mapping[str, Any] | None) -> bool:
    if not frontend:
        return False
    return (
        frontend.get("checklist_status") == "passed"
        and frontend.get("reloaded") is True
        and frontend.get("job_still_visible") is True
        and frontend.get("duplicate_visible") is False
    )


def _frontend_reconnect_from_evidence(evidence: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not evidence:
        return None
    frontend = evidence.get("frontend_reconnect")
    if isinstance(frontend, Mapping):
        return frontend
    durable_jobs = evidence.get("durable_jobs")
    if isinstance(durable_jobs, Mapping) and isinstance(durable_jobs.get("frontend_reconnect"), Mapping):
        return durable_jobs["frontend_reconnect"]
    return None

def derive_phase10_verdicts(
    freight_gate: FreightGateResult,
    search_gates: list[StrictSearchGateResult],
    job_evidence: Mapping[str, Any] | None = None,
    marketplace_verdicts: Sequence[MarketplaceVerdict] | None = None,
) -> dict[str, str]:
    search_passed = bool(search_gates) and all(gate.passed for gate in search_gates)
    frontend = _frontend_reconnect_from_evidence(job_evidence)
    verify_passed = job_evidence is not None and _frontend_reconnect_passed(frontend)
    successful_marketplaces = {
        verdict.marketplace
        for verdict in (marketplace_verdicts or [])
        if verdict.status == "circumvented_in_test_scope"
    }
    mkt_passed = set(MARKETPLACE_DOMAINS).issubset(successful_marketplaces)
    return {
        "MKT-04": "passed" if mkt_passed else "failed",
        "SEARCH-01": "passed" if search_passed else "failed",
        "SHIP-01": "passed" if freight_gate.passed else "failed",
        "VERIFY-01": "passed" if verify_passed else "failed",
    }


def render_json(report: AuditReport) -> bytes:
    return (json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def render_markdown(report: AuditReport) -> bytes:
    payload = report.to_dict()
    lines = [
        "# Marketplace live audit",
        "",
        f"- Audit: `{payload.get('audit_id')}`",
        f"- Generated (UTC): `{payload.get('generated_at')}`",
        f"- Live: `{str(payload.get('live')).lower()}`",
        f"- External calls: `{payload.get('total_external_calls')}`",
        "",
        "## Marketplace Journeys",
        "",
        "| Marketplace | Profile | Journey | Tier | Status | Calls | Duration (ms) |",
        "|---|---|---|---|---|---:|---:|",
    ]
    for row in payload.get("results") or []:
        attempts = row.get("attempts") or []
        if not attempts:
            continue
        attempt = attempts[-1]
        lines.append(
            f"| {row.get('marketplace')} | {row.get('profile')} | {row.get('journey')} | {attempt.get('tier')} | "
            f"{row.get('status')} | {attempt.get('external_calls')} | {attempt.get('duration_ms')} |"
        )
    lines.extend(["", "## Search Gates", ""])
    for gate in payload.get("search_gates") or []:
        lines.append(
            f"- Query: `{gate.get('query')}` ({gate.get('query_id')}) - "
            f"**{'PASSED' if gate.get('passed') else 'FAILED'}** (Accepted: {len(gate.get('accepted') or [])})"
        )

    lines.extend(["", "## Freight Gate", ""])
    freight_gate = payload.get("freight_gate") or {}
    lines.append(f"**{'PASSED' if freight_gate.get('passed') else 'FAILED'}**")
    for row in freight_gate.get("rows") or []:
        lines.append(f"- {row.get('integration')} ({row.get('zipcode')}): {row.get('state')} - {row.get('shipping_price')}")

    lines.extend(["", "## Durable Job Evidence", ""])
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    durable_jobs = evidence.get("durable_jobs")
    if durable_jobs:
        lines.append(f"- Restart job: `{durable_jobs.get('restart_job_kind')}`")
        lines.append(f"- Backend restart command: `{durable_jobs.get('backend_restart_command')}`")
        pre_restart = durable_jobs.get("pre_restart") or {}
        post_restart = durable_jobs.get("post_restart") or {}
        lines.append(f"- Pre restart: `{pre_restart.get('job_id')}` / `{pre_restart.get('status')}`")
        lines.append(f"- Post restart: `{post_restart.get('job_id')}` / `{post_restart.get('status')}`")
        lines.extend([
            "",
            "| Job Type | Job ID | Status | Duplicate Count | Disappeared |",
            "|---|---|---|---:|---|",
        ])
        for kind, snapshot in (durable_jobs.get("job_types") or {}).items():
            lines.append(
                f"| {kind} | {snapshot.get('job_id')} | {snapshot.get('status')} | "
                f"{snapshot.get('duplicate_count')} | {snapshot.get('disappeared')} |"
            )
    else:
        lines.append("- Durable job evidence not provided.")

    lines.extend(["", "## Frontend Reconnect Evidence", ""])
    frontend_reconnect = evidence.get("frontend_reconnect")
    if frontend_reconnect:
        lines.append(f"- Checklist status: `{frontend_reconnect.get('checklist_status')}`")
        lines.append(f"- Observed route: `{frontend_reconnect.get('observed_route')}`")
        screenshot_path = frontend_reconnect.get("screenshot_path")
        if screenshot_path:
            lines.append(f"- Screenshot: `{screenshot_path}`")
    else:
        lines.append("- Frontend reconnect evidence not provided.")

    lines.extend(["", "## Verdicts", ""])
    for verdict in payload.get("verdicts") or []:
        lines.append(f"- `{verdict.get('marketplace')}/{verdict.get('profile')}`: **{verdict.get('status')}**")
    for req, status in (payload.get("requirement_verdicts") or {}).items():
        lines.append(f"- `{req}`: **{status}**")
    return ("\n".join(lines) + "\n").encode("utf-8")

class EngineAdapter:
    """Thin audit adapter around a production engine resolved by EngineFactory."""

    def __init__(
        self,
        engine: Any,
        *,
        tier: str = "http",
        fallback_used: bool = False,
        fingerprint: BrowserFingerprint | None = None,
    ) -> None:
        self.engine = getattr(engine, "_engine", engine)  # bypass resilience cache for live evidence
        self.tier = tier
        self.fallback_used = fallback_used
        self.fingerprint = fingerprint

    @contextmanager
    def _coherent_profile(self):
        """Temporarily align engine headers and SessionFactory selection."""
        if self.fingerprint is None:
            yield
            return
        original_headers = dict(getattr(self.engine, "headers", {}))
        coherent_headers = dict(original_headers)
        coherent_headers.update(self.fingerprint.headers)
        self.engine.headers = coherent_headers
        session_factory = None
        original_choice = None
        if self.engine.__class__.__name__ == "NetshoesEngine":
            from infrastructure.http import session_factory as session_factory_module
            session_factory = session_factory_module
            original_choice = session_factory.random.choice
            def coherent_choice(values):
                if values is session_factory._IMPERSONATIONS:
                    return self.fingerprint.impersonation
                if values is session_factory._USER_AGENTS:
                    return self.fingerprint.user_agent
                return original_choice(values)
            session_factory.random.choice = coherent_choice
        try:
            yield
        finally:
            self.engine.headers = original_headers
            if session_factory is not None and original_choice is not None:
                session_factory.random.choice = original_choice

    async def execute(self, journey: str, **kwargs: Any) -> RawObservation:
        product_url = kwargs["product_url"]
        try:
            with self._coherent_profile():
                if journey == "search":
                    payload = await self.engine.search(
                        kwargs["query"], max_results=kwargs["max_results"], include_shipping=False
                    )
                    error = getattr(payload, "error", None)
                elif journey == "pdp":
                    payload = await self.engine.get_pdp_product(product_url)
                    error = payload.get("error") if isinstance(payload, Mapping) else None
                else:
                    payload = await self.engine.calculate_shipping_advanced(product_url, kwargs["zipcode"])
                    error = payload.get("error") if isinstance(payload, Mapping) else None
            cache_state = payload.get("_marketplace_read_state", "fresh") if isinstance(payload, Mapping) else "fresh"
            return RawObservation(
                payload=payload, error=error, cache_state=cache_state,
                tier=self.tier, fallback_used=self.fallback_used,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            return RawObservation(error=exc, timed_out=True, tier=self.tier, fallback_used=self.fallback_used)
        except Exception as exc:
            status_match = re.search(r"\b(403|429|503)\b", str(exc))
            return RawObservation(
                error=exc,
                http_status=int(status_match.group(1)) if status_match else None,
                tier=self.tier,
                fallback_used=self.fallback_used,
            )


def choose_fingerprint(seed: int | None = None) -> BrowserFingerprint:
    """Choose once per run; the returned immutable profile is sticky."""

    return random.Random(seed).choice(FINGERPRINTS)


def build_live_adapters(
    marketplaces: Sequence[str],
    profiles: Sequence[str],
    fingerprint: BrowserFingerprint,
) -> dict[tuple[str, str], AuditAdapter]:
    """Resolve engines lazily.  Merely importing this module never resolves them."""

    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from services.engines.factory import EngineFactory

    adapters: dict[tuple[str, str], AuditAdapter] = {}
    for marketplace in marketplaces:
        engine = EngineFactory.get_engine(marketplace)
        for profile in profiles:
            adapters[(profile, marketplace)] = EngineAdapter(
                engine, tier=profile,
                fingerprint=fingerprint if profile == "coherent_http" else None,
            )
    return adapters


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _validate_selection(values: Sequence[str], allowed: Sequence[str], label: str) -> list[str]:
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise AuditConfigurationError(f"unknown {label}: {', '.join(unknown)}")
    return list(dict.fromkeys(values))


def build_report(
    manifest: Mapping[str, Any], results: list[JourneyResult], *, live: bool,
    fingerprint: BrowserFingerprint | None, budget: ExternalCallBudget,
    freight_results: FreightGateResult, search_results: list[StrictSearchGateResult],
    commands: list[str],
    job_evidence: Mapping[str, Any] | None = None,
    frontend_checklist: Mapping[str, Any] | None = None,
) -> AuditReport:
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    evidence = _build_phase10_evidence(job_evidence, frontend_checklist)
    marketplace_verdicts = derive_verdicts(results)
    return AuditReport(
        schema_version=1,
        audit_id=generated.replace(":", "").replace("+00:00", "Z"),
        generated_at=generated,
        live=live,
        fingerprint=fingerprint.name if fingerprint else None,
        manifest=sanitize_value(dict(manifest)),
        commands=commands,
        results=results,
        search_gates=search_results,
        freight_gate=freight_results,
        evidence=evidence,
        verdicts=marketplace_verdicts,
        requirement_verdicts=derive_phase10_verdicts(
            freight_results, search_results, evidence, marketplace_verdicts
        ),
        total_external_calls=budget.total,
    )


def build_phase10_closeout_report(
    manifest: Mapping[str, Any],
    results: list[JourneyResult],
    *,
    live: bool,
    fingerprint: BrowserFingerprint | None,
    budget: ExternalCallBudget,
    freight_results: FreightGateResult,
    search_results: list[StrictSearchGateResult],
    commands: list[str],
    job_evidence: Mapping[str, Any] | None = None,
    frontend_checklist: Mapping[str, Any] | None = None,
) -> AuditReport:
    return build_report(
        manifest,
        results,
        live=live,
        fingerprint=fingerprint,
        budget=budget,
        freight_results=freight_results,
        search_results=search_results,
        commands=commands,
        job_evidence=job_evidence,
        frontend_checklist=frontend_checklist,
    )


async def run_live(
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    freight_results: FreightGateResult,
    search_results: list[StrictSearchGateResult],
    job_evidence: Mapping[str, Any] | None = None,
    frontend_checklist: Mapping[str, Any] | None = None,
) -> AuditReport:
    fingerprint = choose_fingerprint(args.seed)
    budget_limit = min(int(args.max_external_calls), int(manifest["max_external_calls"]))
    budget = ExternalCallBudget(budget_limit, int(manifest["max_calls_per_marketplace"]))
    cooldown = manifest["cooldown_seconds"]
    throttle = DomainThrottle(float(cooldown["min"]), float(cooldown["max"]))
    adapters = build_live_adapters(args.marketplaces, args.profiles, fingerprint)
    strict_products: list[dict[str, Any]] = []
    results = await run_matrix(
        manifest, adapters, marketplaces=args.marketplaces, profiles=args.profiles,
        journeys=args.journeys, budget=budget, throttle=throttle,
        search_products=strict_products,
    )
    if not search_results:
        search_results = build_search_gate_results(manifest, strict_products)
    command_parts = [
        "python scripts/audit_marketplaces_live.py",
        "--live",
        "--profiles", ",".join(args.profiles),
        "--marketplaces", ",".join(args.marketplaces),
        "--journeys", ",".join(args.journeys),
        "--manifest", str(args.manifest),
        "--freight-matrix", str(args.freight_matrix),
        "--max-external-calls", str(args.max_external_calls),
        "--output", str(args.output),
    ]
    if args.phase_report_output:
        command_parts.extend(["--phase-report-output", str(args.phase_report_output)])
    commands = [" ".join(command_parts)]
    if args.job_evidence:
        commands[0] += f" --job-evidence {args.job_evidence}"
    if args.frontend_checklist:
        commands[0] += f" --frontend-checklist {args.frontend_checklist}"
    return build_phase10_closeout_report(
        manifest,
        results,
        live=True,
        fingerprint=fingerprint,
        budget=budget,
        freight_results=freight_results,
        search_results=search_results,
        commands=commands,
        job_evidence=job_evidence,
        frontend_checklist=frontend_checklist,
    )


def render_payload_json(payload: Mapping[str, Any]) -> bytes:
    sanitized = sanitize_phase10_evidence(sanitize_value(dict(payload)))
    return (json.dumps(sanitized, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def render_payload_markdown(payload: Mapping[str, Any]) -> bytes:
    payload = sanitize_phase10_evidence(sanitize_value(dict(payload)))
    lines = [
        "# Marketplace live audit",
        "",
        f"- Audit ID: {payload.get('audit_id')}",
        f"- Generated at: {payload.get('generated_at')}",
        f"- Live mode: {payload.get('live')}",
        f"- Fingerprint: {payload.get('fingerprint')}",
        f"- External calls: {payload.get('total_external_calls')}",
        "",
        "## Commands",
        "",
    ]
    for command in payload.get("commands") or []:
        lines.append(f"- {command}")

    lines.extend(["", "## Requirement Verdicts", ""])
    for req, status in (payload.get("requirement_verdicts") or {}).items():
        lines.append(f"- {req}: {status}")

    lines.extend(["", "## Search Gate", ""])
    for gate in payload.get("search_gates") or []:
        lines.append(
            f"- {gate.get('query_id')}: {'PASSED' if gate.get('passed') else 'FAILED'} "
            f"({len(gate.get('accepted') or [])}/{gate.get('required_count')})"
        )

    lines.extend(["", "## Freight Gate", ""])
    freight_gate = payload.get("freight_gate") or {}
    lines.append(f"{'PASSED' if freight_gate.get('passed') else 'FAILED'}")
    for row in freight_gate.get("rows") or []:
        lines.append(f"- {row.get('integration')} ({row.get('zipcode')}): {row.get('state')} - {row.get('shipping_price')}")

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    durable_jobs = payload.get("job_evidence") or evidence.get("durable_jobs")
    lines.extend(["", "## Durable Job Evidence", ""])
    if durable_jobs:
        lines.append(f"- Restart job: {durable_jobs.get('restart_job_kind')}")
        lines.append(f"- Backend restart command: {durable_jobs.get('backend_restart_command')}")
        lines.extend([
            "",
            "| Job Type | Job ID | Status | Duplicate Count | Disappeared |",
            "|---|---|---|---:|---|",
        ])
        for kind, snapshot in (durable_jobs.get("job_types") or {}).items():
            lines.append(
                f"| {kind} | {snapshot.get('job_id')} | {snapshot.get('status')} | "
                f"{snapshot.get('duplicate_count')} | {snapshot.get('disappeared')} |"
            )
    else:
        lines.append("- Durable job evidence not provided.")

    frontend = payload.get("frontend_reconnect") or evidence.get("frontend_reconnect")
    lines.extend(["", "## Frontend Reconnect Evidence", ""])
    if frontend:
        lines.append(f"- Checklist status: {frontend.get('checklist_status')}")
        lines.append(f"- Observed route: {frontend.get('observed_route')}")
        screenshot_path = frontend.get("screenshot_path")
        if screenshot_path:
            lines.append(f"- Screenshot: {screenshot_path}")
    else:
        lines.append("- Frontend reconnect evidence not provided.")

    sources = payload.get("evidence_sources") or {}
    if sources:
        lines.extend(["", "## Evidence Sources", ""])
        for name, source in sources.items():
            lines.append(f"- {name}: {source}")

    lines.extend(["", "## Verdicts", ""])
    for verdict in payload.get("verdicts") or []:
        lines.append(f"- {verdict.get('marketplace')}/{verdict.get('profile')}: {verdict.get('status')}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_final_report_payload(
    payload: Mapping[str, Any],
    output: Path,
    phase_report_output: Path | None = None,
) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / PHASE10_COMPANION_JSON
    markdown_path = output / PHASE10_COMPANION_MARKDOWN
    sanitized_payload = sanitize_phase10_evidence(sanitize_value(dict(payload)))
    json_bytes = (json.dumps(sanitized_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    md_bytes = render_payload_markdown(sanitized_payload)
    json_path.write_bytes(json_bytes)
    markdown_path.write_bytes(md_bytes)

    if phase_report_output:
        phase_report_output.mkdir(parents=True, exist_ok=True)
        (phase_report_output / "10-LIVE-REPORT.json").write_bytes(json_bytes)
        (phase_report_output / "10-LIVE-REPORT.md").write_bytes(md_bytes)
    return json_path, markdown_path

def write_report(report: AuditReport, output: Path, phase_report_output: Path | None = None) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=True)
    stamp = report.audit_id
    json_path = output / f"marketplace_audit_{stamp}.json"
    markdown_path = output / f"marketplace_audit_{stamp}.md"
    companion_json_path = output / PHASE10_COMPANION_JSON
    companion_markdown_path = output / PHASE10_COMPANION_MARKDOWN

    json_bytes = render_json(report)
    md_bytes = render_markdown(report)

    json_path.write_bytes(json_bytes)
    markdown_path.write_bytes(md_bytes)
    companion_json_path.write_bytes(json_bytes)
    companion_markdown_path.write_bytes(md_bytes)

    if phase_report_output:
        phase_report_output.mkdir(parents=True, exist_ok=True)
        (phase_report_output / "10-LIVE-REPORT.json").write_bytes(json_bytes)
        (phase_report_output / "10-LIVE-REPORT.md").write_bytes(md_bytes)

    return json_path, markdown_path


def _latest_timestamped_report(output: Path) -> Path:
    candidates = sorted(output.glob("marketplace_audit_*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise AuditConfigurationError("no marketplace audit report found")
    return candidates[-1]


def verify_latest(output: Path) -> tuple[Path, Path]:
    companion_path = output / PHASE10_COMPANION_JSON
    json_path = companion_path if companion_path.is_file() else _latest_timestamped_report(output)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    forbidden = re.compile(r"cookie|authorization|set-cookie|<html|proxy.*://[^/\s]+@", re.IGNORECASE)
    if forbidden.search(json.dumps(payload, ensure_ascii=False)):
        raise AuditConfigurationError("latest report contains forbidden sensitive material")
    if payload.get("schema_version") != 1 or not isinstance(payload.get("verdicts"), list):
        raise AuditConfigurationError("latest report has an invalid schema")

    val = validate_phase10_report_payload(payload)
    if not val.passed:
        raise AuditConfigurationError(f"latest report is unsanitized or invalid for Phase 10: {val.rejections}")

    job_evidence = payload.get("job_evidence")
    if job_evidence is None and isinstance(payload.get("evidence"), Mapping):
        job_evidence = payload["evidence"].get("durable_jobs")
    if json_path.name == PHASE10_COMPANION_JSON:
        if job_evidence is None:
            raise AuditConfigurationError("missing durable job evidence")
        validate_job_evidence(_require_mapping(job_evidence, "job_evidence"))

    markdown_path = output / PHASE10_COMPANION_MARKDOWN if json_path.name == PHASE10_COMPANION_JSON else json_path.with_suffix(".md")
    if not markdown_path.is_file():
        raise AuditConfigurationError("latest report is missing Markdown companion")
    return json_path, markdown_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly permit external calls")
    parser.add_argument("--verify-latest", action="store_true")
    parser.add_argument("--merge-latest", action="store_true")
    parser.add_argument("--profiles", type=_csv, default=list(ALLOWED_PROFILES))
    parser.add_argument("--marketplaces", type=_csv, default=list(MARKETPLACE_DOMAINS))
    parser.add_argument("--journeys", type=_csv, default=list(ALLOWED_JOURNEYS))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--freight-matrix", type=Path, default=None)
    parser.add_argument("--job-evidence", type=Path, default=None)
    parser.add_argument("--frontend-checklist", type=Path, default=None)
    parser.add_argument("--search-evidence", type=Path, default=None)
    parser.add_argument("--max-external-calls", type=int, default=24)
    parser.add_argument("--phase-report-output", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    args.profiles = _validate_selection(args.profiles, ALLOWED_PROFILES, "profile")
    args.marketplaces = _validate_selection(args.marketplaces, tuple(MARKETPLACE_DOMAINS), "marketplace")
    args.journeys = _validate_selection(args.journeys, ALLOWED_JOURNEYS, "journey")
    if args.max_external_calls < 1:
        parser.error("--max-external-calls must be positive")
    if args.merge_latest and args.live:
        parser.error("--merge-latest cannot be combined with --live")
    if args.merge_latest:
        missing = []
        if args.freight_matrix is None:
            missing.append("--freight-matrix")
        if args.phase_report_output is None:
            missing.append("--phase-report-output")
        if args.job_evidence is None:
            missing.append("--job-evidence")
        if args.frontend_checklist is None:
            missing.append("--frontend-checklist")
        if args.search_evidence is None:
            missing.append("--search-evidence")
        if missing:
            parser.error("--merge-latest requires " + ", ".join(missing))
    return args


def _load_job_evidence_file(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _require_mapping(payload, "job evidence file")


def _load_frontend_checklist_file(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _require_mapping(payload, "frontend checklist file")
    return {
        "checklist_path": str(path),
        "checklist_text": path.read_text(encoding="utf-8"),
    }


def load_search_acceptance_evidence(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence = _require_mapping(payload, "search evidence file")
    if _contains_sensitive_job_material(evidence):
        raise AuditConfigurationError("search evidence contains sensitive material")
    return evidence


def merge_search_acceptance_evidence(payload: dict[str, Any], evidence: Mapping[str, Any] | None) -> None:
    if not evidence:
        return
    manifest = payload.get("manifest") or {}
    marketplaces = manifest.get("marketplaces") or list(MARKETPLACE_DOMAINS)
    expected_origins = []
    for m in marketplaces:
        if m in MARKETPLACE_DOMAINS:
            expected_origins.extend(MARKETPLACE_DOMAINS[m])
    
    validated = validate_search_acceptance_evidence(evidence, expected_origins)
    payload["search_acceptance"] = validated.sanitized_payload
    payload["search_acceptance"]["passed"] = validated.passed
    payload["search_acceptance"]["accepted_count"] = validated.accepted_count

def _merge_latest_command(args: argparse.Namespace) -> str:
    command_parts = [
        "python scripts/audit_marketplaces_live.py",
        "--merge-latest",
        "--manifest", str(args.manifest),
        "--freight-matrix", str(args.freight_matrix),
        "--output", str(args.output),
        "--phase-report-output", str(args.phase_report_output),
        "--job-evidence", str(args.job_evidence),
        "--frontend-checklist", str(args.frontend_checklist),
        "--search-evidence", str(args.search_evidence),
    ]
    return " ".join(command_parts)


def merge_latest_report(args: argparse.Namespace, manifest: Mapping[str, Any]) -> tuple[Path, Path]:
    if args.freight_matrix is None:
        raise AuditConfigurationError("--merge-latest requires --freight-matrix")
    load_freight_acceptance_matrix(args.freight_matrix)
    source_json = _latest_timestamped_report(args.output)
    source_payload = json.loads(source_json.read_text(encoding="utf-8"))
    if source_payload.get("schema_version") != 1 or not isinstance(source_payload.get("verdicts"), list):
        raise AuditConfigurationError("latest report has an invalid schema")

    job_evidence = _load_job_evidence_file(args.job_evidence)
    frontend_checklist = _load_frontend_checklist_file(args.frontend_checklist)
    search_evidence = load_search_acceptance_evidence(args.search_evidence) if args.search_evidence else None
    evidence = _build_phase10_evidence(job_evidence, frontend_checklist)
    payload = sanitize_phase10_evidence(sanitize_value(dict(source_payload)))
    
    merge_search_acceptance_evidence(payload, search_evidence)
    
    payload["manifest"] = sanitize_phase10_evidence(sanitize_value(dict(manifest)))
    payload["generated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload["evidence"] = evidence
    payload["job_evidence"] = evidence.get("durable_jobs")
    payload["frontend_reconnect"] = evidence.get("frontend_reconnect")
    payload["evidence_sources"] = {
        "marketplace_audit": str(source_json),
        "job_evidence": str(args.job_evidence),
        "frontend_checklist": str(args.frontend_checklist),
    }
    if args.search_evidence:
        payload["evidence_sources"]["search_evidence"] = str(args.search_evidence)

    commands = list(payload.get("commands") or [])
    commands.append(_merge_latest_command(args))
    payload["commands"] = commands

    requirement_verdicts = dict(payload.get("requirement_verdicts") or {})
    requirement_verdicts["VERIFY-01"] = (
        "passed" if payload["job_evidence"] and _frontend_reconnect_passed(payload.get("frontend_reconnect")) else "failed"
    )
    if "search_acceptance" in payload:
        requirement_verdicts["SEARCH-01"] = "passed" if payload["search_acceptance"].get("passed") else "failed"
    payload["requirement_verdicts"] = requirement_verdicts

    val = validate_phase10_report_payload(payload)
    if not val.passed:
        raise AuditConfigurationError(f"merged report is unsanitized or invalid for Phase 10: {val.rejections}")
    validate_job_evidence(_require_mapping(payload["job_evidence"], "job_evidence"))
    return write_final_report_payload(payload, args.output, args.phase_report_output)


def dry_run_payload(args: argparse.Namespace, manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "network_enabled": False,
        "marketplaces": args.marketplaces,
        "profiles": args.profiles,
        "journeys": args.journeys,
        "matrix_cells": len(args.marketplaces) * len(args.profiles) * len(args.journeys),
        "max_external_calls": min(args.max_external_calls, int(manifest["max_external_calls"])),
        "concurrency": 1,
        "attempts_executed": 0,
        "job_evidence": str(args.job_evidence) if args.job_evidence else None,
        "frontend_checklist": str(args.frontend_checklist) if args.frontend_checklist else None,
    }


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.verify_latest:
        json_path, markdown_path = verify_latest(args.output)
        print(json.dumps({"verified": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
        return 0
    manifest = load_manifest(args.manifest)
    if args.merge_latest:
        json_path, markdown_path = merge_latest_report(args, manifest)
        print(json.dumps({"json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
        return 0
    if not args.live:
        print(json.dumps(dry_run_payload(args, manifest), ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    job_evidence = _load_job_evidence_file(args.job_evidence)
    frontend_checklist = _load_frontend_checklist_file(args.frontend_checklist)
    freight_matrix = (
        load_freight_acceptance_matrix(args.freight_matrix)
        if args.freight_matrix
        else {"integrations": {}, "ceps": []}
    )
    if args.freight_matrix:
        freight_rows = await run_freight_matrix(freight_matrix)
        freight_results = validate_freight_gate_results(freight_rows, expected_ceps=[str(z) for z in freight_matrix.get("ceps", [])])
    else:
        freight_results = build_freight_gate_results(freight_matrix)
    report = await run_live(
        args,
        manifest,
        freight_results,
        [],
        job_evidence,
        frontend_checklist,
    )
    paths = write_report(report, args.output, args.phase_report_output)
    print(json.dumps({"json": str(paths[0]), "markdown": str(paths[1])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
