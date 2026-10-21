"""Pure validators for the Phase 10 operational acceptance gates.

The functions in this module intentionally perform no network access, no file
I/O, and no marketplace engine resolution. They validate evidence produced by
later live steps before that evidence can satisfy SEARCH-01, SHIP-01, or
VERIFY-01.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from services.relevance_gates import compute_min_score_cutoff


GENERIC_SEARCH_QUERY_ID = "generic_camiseta_masculina"
SUPPORTED_FREIGHT_INTEGRATIONS = {
    "amazon",
    "mercado_livre",
    "netshoes",
    "shopify",
    "vtex",
    "wake",
}
SUCCESS_FREIGHT_STATES = {"available", "free"}
DEGRADED_STATES = {
    "blocked",
    "stale",
    "not_fresh",
    "temporary_failure",
    "unsupported",
    "parser_failure",
    "not_reached",
}

PHASE10_EXPECTED_FREIGHT_CEPS = (
    "01310100",
    "90010150",
    "70040010",
    "40020000",
    "69010001",
)
FREIGHT_INTEGRATION_ALLOWED_HOSTS = {
    "amazon": ("amazon.com.br",),
    "mercado_livre": ("mercadolivre.com.br",),
    "netshoes": ("netshoes.com.br",),
    "shopify": ("ricardoalmeida.com.br",),
    "vtex": ("aramis.com.br",),
    "wake": ("richards.com.br",),
}
SENSITIVE_KEY_RE = re.compile(
    r"authorization|cookie|set-cookie|api[-_]?key|token|secret|password|passwd|proxy|headers?|html|body",
    re.IGNORECASE,
)
HTML_RE = re.compile(r"<\s*(?:!doctype|html|head|body|script|form)\b", re.IGNORECASE)
BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(api[-_]?key|token|secret|password|passwd|cookie|authorization)=([^\s&;]+)"
)
CEP_RE = re.compile(r"\b\d{5}-?\d{3}\b")
URL_RE = re.compile(r"https?://[^\s<>'\"]+")


@dataclass(frozen=True)
class StrictProductEvidence:
    marketplace: str | None
    title: str
    normalized_url: str
    price: Decimal | None
    score: Decimal | None
    sku: str | None = None
    native_code: str | None = None
    reason: str = "accepted"


@dataclass(frozen=True)
class StrictSearchGateResult:
    query_id: str
    query: str
    passed: bool
    counts_toward_threshold: bool
    required_count: int
    accepted: list[StrictProductEvidence] = field(default_factory=list)
    rejected: list[StrictProductEvidence] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)


@dataclass(frozen=True)
class SearchAcceptanceEvidence:
    query_id: str
    passed: bool
    counts_toward_threshold: bool
    accepted_count: int
    accepted_products: list[StrictProductEvidence] = field(default_factory=list)
    rejected_products: list[StrictProductEvidence] = field(default_factory=list)
    sanitized_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FreightMatrixRow:
    integration: str
    state: str
    product_url: str
    zipcode: str
    shipping_price: Any = None
    is_free_shipping: bool = False
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FreightMatrixRejection:
    integration: str
    state: str
    reason: str
    product_url: str = ""
    zipcode: str = ""


@dataclass(frozen=True)
class FreightGateResult:
    passed: bool
    rows: list[FreightMatrixRow] = field(default_factory=list)
    rejections: list[FreightMatrixRejection] = field(default_factory=list)


@dataclass(frozen=True)
class Phase10ReportPayload:
    passed: bool
    sanitized_payload: dict[str, Any]
    rejections: list[str] = field(default_factory=list)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    return {}


def _field(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _normalize_url(value: Any) -> tuple[str, str, str | None]:
    if not isinstance(value, str) or not value.strip():
        return "", "missing_product_url", None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return "", "invalid_url", None
    if parsed.scheme != "https":
        return "", "invalid_url_scheme", parsed.hostname.lower() if parsed.hostname else None
    if not parsed.hostname:
        return "", "invalid_url", None
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    normalized = urlunsplit((parsed.scheme, f"{host}{port}", parsed.path or "/", "", ""))
    return normalized, "", host


def _expected_origin(host: str | None, expected_origins: Sequence[str]) -> bool:
    if not host:
        return False
    normalized = {origin.lower() for origin in expected_origins}
    return any(host == origin or host.endswith(f".{origin}") for origin in normalized)


def _string_key(value: Any) -> str | None:
    text = str(value or "").strip().casefold()
    return text or None


def _rejection(reason: str, product: Mapping[str, Any], *, normalized_url: str = "") -> StrictProductEvidence:
    return StrictProductEvidence(
        marketplace=_string_key(_field(product, "marketplace", "plataforma", "origin")),
        title=str(_field(product, "title", "product_name", "name", "titulo", "raw_title") or ""),
        normalized_url=normalized_url,
        price=_decimal_or_none(_field(product, "price", "price_full", "preco", "price_discount")),
        score=_decimal_or_none(_field(product, "final_match_score", "match_score", "score")),
        sku=_string_key(_field(product, "sku", "seller_sku", "product_sku")),
        native_code=_string_key(_field(product, "native_code", "product_code", "item_id", "id")),
        reason=reason,
    )


def validate_strict_final_products(
    products: Sequence[Any],
    *,
    query_id: str,
    query: str,
    expected_origins: Sequence[str],
    required_count: int = 10,
    min_score: float | Decimal | None = None,
    has_vision: bool = False,
) -> StrictSearchGateResult:
    """Validate the strict final-product SEARCH-01 acceptance contract."""

    cutoff = Decimal(str(compute_min_score_cutoff(float(min_score or 0), has_vision)))
    accepted: list[StrictProductEvidence] = []
    rejected: list[StrictProductEvidence] = []
    seen_urls: set[str] = set()
    seen_skus: set[str] = set()
    seen_native_codes: set[str] = set()

    for raw in products:
        product = _as_mapping(raw)
        title = str(_field(product, "title", "product_name", "name", "titulo", "raw_title") or "").strip()
        normalized_url, url_reason, host = _normalize_url(_field(product, "url", "product_url"))
        price = _decimal_or_none(_field(product, "price", "price_full", "preco", "price_discount"))
        score = _decimal_or_none(_field(product, "final_match_score", "match_score", "score"))
        sku = _string_key(_field(product, "sku", "seller_sku", "product_sku"))
        native_code = _string_key(_field(product, "native_code", "product_code", "item_id", "id"))
        marketplace = _string_key(_field(product, "marketplace", "plataforma", "origin"))

        if not title:
            rejected.append(_rejection("empty_title", product, normalized_url=normalized_url))
            continue
        if url_reason:
            rejected.append(_rejection(url_reason, product, normalized_url=normalized_url))
            continue
        if not _expected_origin(host, expected_origins):
            rejected.append(_rejection("unexpected_origin", product, normalized_url=normalized_url))
            continue
        if price is None:
            rejected.append(_rejection("missing_price", product, normalized_url=normalized_url))
            continue
        if price <= 0:
            rejected.append(_rejection("non_positive_price", product, normalized_url=normalized_url))
            continue
        if score is None or score < cutoff:
            rejected.append(_rejection("score_below_cutoff", product, normalized_url=normalized_url))
            continue
        if normalized_url in seen_urls:
            rejected.append(_rejection("duplicate_url", product, normalized_url=normalized_url))
            continue
        if sku and sku in seen_skus:
            rejected.append(_rejection("duplicate_sku", product, normalized_url=normalized_url))
            continue
        if native_code and native_code in seen_native_codes:
            rejected.append(_rejection("duplicate_native_code", product, normalized_url=normalized_url))
            continue

        seen_urls.add(normalized_url)
        if sku:
            seen_skus.add(sku)
        if native_code:
            seen_native_codes.add(native_code)
        accepted.append(
            StrictProductEvidence(
                marketplace=marketplace,
                title=title,
                normalized_url=normalized_url,
                price=price,
                score=score,
                sku=sku,
                native_code=native_code,
            )
        )

    counts_toward_threshold = query_id == GENERIC_SEARCH_QUERY_ID
    if not counts_toward_threshold and accepted:
        rejected = [
            StrictProductEvidence(
                marketplace=None,
                title=query,
                normalized_url="",
                price=None,
                score=None,
                reason="business_query_not_counted_for_search_threshold",
            )
        ]
    elif len(accepted) < required_count:
        rejected.append(
            StrictProductEvidence(
                marketplace=None,
                title=query,
                normalized_url="",
                price=None,
                score=None,
                reason="accepted_count_below_required",
            )
        )

    return StrictSearchGateResult(
        query_id=query_id,
        query=query,
        passed=counts_toward_threshold and len(accepted) >= required_count,
        counts_toward_threshold=counts_toward_threshold,
        required_count=required_count,
        accepted=accepted,
        rejected=rejected,
    )


def validate_search_acceptance_evidence(
    payload: Mapping[str, Any],
    expected_origins: Sequence[str]
) -> SearchAcceptanceEvidence:
    """Validate SearchAcceptanceEvidence containing final products."""
    
    sanitized = sanitize_phase10_evidence(dict(payload))
    query_id = str(sanitized.get("query_id") or "")
    query = str(sanitized.get("query") or "")
    results = sanitized.get("results")
    if not isinstance(results, Sequence):
        results = []
    
    strict_gate = validate_strict_final_products(
        results,
        query_id=query_id,
        query=query,
        expected_origins=expected_origins,
        required_count=10,
        min_score=None
    )
    
    return SearchAcceptanceEvidence(
        query_id=query_id,
        passed=strict_gate.passed,
        counts_toward_threshold=strict_gate.counts_toward_threshold,
        accepted_count=strict_gate.accepted_count,
        accepted_products=strict_gate.accepted,
        rejected_products=strict_gate.rejected,
        sanitized_payload=sanitized
    )


def _freight_rejection(row: FreightMatrixRow, reason: str) -> FreightMatrixRejection:
    return FreightMatrixRejection(
        integration=row.integration,
        state=row.state,
        reason=reason,
        product_url=row.product_url,
        zipcode=row.zipcode,
    )


def _valid_zipcode(value: str) -> bool:
    return bool(re.fullmatch(r"\d{5}-?\d{3}", str(value or "")))


def validate_freight_gate_results(
    rows: Sequence[FreightMatrixRow],
    *,
    expected_ceps: Sequence[str] = PHASE10_EXPECTED_FREIGHT_CEPS,
    allowed_hosts_by_integration: Mapping[str, Sequence[str]] = FREIGHT_INTEGRATION_ALLOWED_HOSTS,
) -> FreightGateResult:
    """Validate SHIP-01 freight evidence for the declared supported integrations."""

    rejections: list[FreightMatrixRejection] = []
    observed_integrations: set[str] = set()
    observed_regions: set[tuple[str, str]] = set()
    expected_zipcodes = {str(zipcode).replace("-", "") for zipcode in expected_ceps}

    for row in rows:
        integration = row.integration.strip().casefold()
        state = row.state.strip().casefold()
        observed_integrations.add(integration)
        price = _decimal_or_none(row.shipping_price)
        explicit_free = row.is_free_shipping is True or state == "free"
        zipcode_valid = _valid_zipcode(row.zipcode)
        normalized_zipcode = str(row.zipcode or "").replace("-", "")

        if integration not in SUPPORTED_FREIGHT_INTEGRATIONS:
            rejections.append(_freight_rejection(row, "unknown_integration"))
        elif zipcode_valid:
            observed_regions.add((integration, normalized_zipcode))

        if state == "blocked":
            rejections.append(_freight_rejection(row, "blocked_state"))
        elif state == "stale":
            rejections.append(_freight_rejection(row, "stale_state"))
        elif state == "temporary_failure":
            rejections.append(_freight_rejection(row, "temporary_failure_state"))
        elif state == "unsupported" and integration in SUPPORTED_FREIGHT_INTEGRATIONS:
            rejections.append(_freight_rejection(row, "unsupported_supported_integration"))
        elif state not in SUCCESS_FREIGHT_STATES:
            rejections.append(_freight_rejection(row, f"{state or 'missing'}_state"))

        if not zipcode_valid:
            rejections.append(_freight_rejection(row, "missing_zipcode"))
        normalized_url, url_reason, host = _normalize_url(row.product_url)
        if url_reason:
            rejections.append(_freight_rejection(row, "missing_product_url" if not row.product_url else url_reason))
        elif not normalized_url:
            rejections.append(_freight_rejection(row, "missing_product_url"))
        else:
            allowed_hosts = allowed_hosts_by_integration.get(integration, ())
            if allowed_hosts and not _expected_origin(host, allowed_hosts):
                rejections.append(_freight_rejection(row, "unexpected_origin"))
        if not row.evidence:
            rejections.append(_freight_rejection(row, "missing_integration_evidence"))

        if explicit_free:
            if price is None:
                rejections.append(_freight_rejection(row, "invalid_money"))
            elif price != 0:
                rejections.append(_freight_rejection(row, "invalid_free_money"))
            continue
        if price is None:
            rejections.append(_freight_rejection(row, "invalid_money"))
        elif price == 0:
            rejections.append(_freight_rejection(row, "false_zero_shipping"))
        elif price < 0:
            rejections.append(_freight_rejection(row, "invalid_money"))

    missing = sorted(SUPPORTED_FREIGHT_INTEGRATIONS - observed_integrations)
    for integration in missing:
        rejections.append(
            FreightMatrixRejection(
                integration=integration,
                state="missing",
                reason="missing_integration_evidence",
            )
        )

    for integration in sorted(SUPPORTED_FREIGHT_INTEGRATIONS):
        for zipcode in sorted(expected_zipcodes):
            if (integration, zipcode) not in observed_regions:
                rejections.append(
                    FreightMatrixRejection(
                        integration=integration,
                        state="missing",
                        reason="missing_region_evidence",
                        zipcode=zipcode,
                    )
                )

    return FreightGateResult(passed=not rejections, rows=list(rows), rejections=rejections)

def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED URL]"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return value
    host = parsed.hostname.lower()
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def _sanitize_text(value: Any) -> str:
    text = str(value or "")
    if HTML_RE.search(text):
        return "[HTML OMITTED]"
    text = BEARER_RE.sub("[REDACTED CREDENTIAL]", text)
    text = KEY_VALUE_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = URL_RE.sub(lambda match: _sanitize_url(match.group(0).rstrip(".,);")), text)
    text = CEP_RE.sub("[REDACTED CEP]", text)
    return text


def sanitize_phase10_evidence(value: Any) -> Any:
    """Return a JSON-ready value without sensitive Phase 10 evidence material."""

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if SENSITIVE_KEY_RE.search(name):
                continue
            cleaned[name] = sanitize_phase10_evidence(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [sanitize_phase10_evidence(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def _url_has_query_or_is_malformed(value: str) -> bool:
    for match in URL_RE.finditer(value):
        try:
            if urlsplit(match.group(0)).query:
                return True
        except ValueError:
            return True
    return False

def _contains_unsafe_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                return True
            if _contains_unsafe_material(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_unsafe_material(item) for item in value)
    if not isinstance(value, str):
        return False
    return (
        HTML_RE.search(value) is not None
        or BEARER_RE.search(value) is not None
        or KEY_VALUE_SECRET_RE.search(value) is not None
        or CEP_RE.search(value) is not None
        or _url_has_query_or_is_malformed(value)
        or re.search(r"https?://[^/\s]+:[^@\s]+@", value) is not None
    )


def _contains_query_fragment_or_unsafe_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_query_fragment_or_unsafe_material(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_query_fragment_or_unsafe_material(item) for item in value)
    if not isinstance(value, str):
        return False
    return "?" in value or "#" in value or _contains_unsafe_material(value)

def validate_phase10_report_payload(payload: Mapping[str, Any]) -> Phase10ReportPayload:
    """Validate a sanitized, reproducible Phase 10 closeout payload."""

    sanitized = sanitize_phase10_evidence(dict(payload))
    rejections: list[str] = []

    if payload.get("schema_version") != 1:
        rejections.append("invalid_schema_version")
    if not str(payload.get("generated_at") or "").strip():
        rejections.append("missing_generated_at")

    manifest = str(payload.get("manifest") or "")
    if not manifest:
        rejections.append("missing_manifest_reference")
    elif "?" in manifest or "#" in manifest or _contains_unsafe_material(manifest):
        rejections.append("unsafe_manifest_reference")

    commands = payload.get("commands")
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)) or not commands:
        rejections.append("missing_reproducible_commands")
    elif any(not isinstance(command, str) or not command.strip() for command in commands):
        rejections.append("missing_reproducible_commands")
    elif _contains_query_fragment_or_unsafe_material(commands):
        rejections.append("unsafe_commands_material")

    evidence_sources = payload.get("evidence_sources")
    if evidence_sources is not None and _contains_query_fragment_or_unsafe_material(evidence_sources):
        rejections.append("unsafe_evidence_sources_material")

    results = payload.get("results")
    if results is not None and (_contains_unsafe_material(results) or _contains_unsafe_material(sanitized.get("results"))):
        rejections.append("unsafe_results_material")

    freight_gate = payload.get("freight_gate")
    if freight_gate is not None and (_contains_unsafe_material(freight_gate) or _contains_unsafe_material(sanitized.get("freight_gate"))):
        rejections.append("unsafe_freight_gate_material")
    if "evidence" not in payload:
        rejections.append("missing_evidence")
    elif _contains_unsafe_material(payload["evidence"]):
        rejections.append("unsafe_evidence_material")
    elif _contains_unsafe_material(sanitized.get("evidence")):
        rejections.append("unsafe_evidence_material")

    return Phase10ReportPayload(
        passed=not rejections,
        sanitized_payload=sanitized,
        rejections=rejections,
    )


__all__ = [
    "FreightGateResult",
    "FreightMatrixRejection",
    "FreightMatrixRow",
    "Phase10ReportPayload",
    "SearchAcceptanceEvidence",
    "StrictProductEvidence",
    "StrictSearchGateResult",
    "sanitize_phase10_evidence",
    "validate_freight_gate_results",
    "validate_phase10_report_payload",
    "validate_search_acceptance_evidence",
    "validate_strict_final_products",
]
