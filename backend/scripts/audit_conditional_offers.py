"""Bounded live coverage audit for conditional offers on active own-brand sites.

The audit deliberately separates cheap product discovery from full PDP enrichment.
It never accepts caller-provided URLs, credentials, cookies, coupon guesses, or
checkout payloads.  Final publication is a recoverable three-file transaction;
``conditional_offers_latest.json`` is replaced last and acts as the commit marker.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Sequence
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, model_validator


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api.routes_promotions import promotion_orchestrator
from core.browser_manager import BrowserManager
from core.models import ConditionalOfferResult
from core.session_manager import SessionManager
from services.brand_service import brand_service
from services.engines.factory import engine_factory
from services.promotions.registry import resolve_brand


LOGGER = logging.getLogger("conditional-offer-audit")
MARKETPLACE_KEYS = frozenset({"mercado_livre", "netshoes", "amazon"})
ACCEPTED_STATUSES = frozenset(
    {"found", "none_detected", "unsupported", "blocked", "requires_auth"}
)
MAX_GLOBAL_CONCURRENCY = 2
MAX_PRODUCTS_PER_BRAND = 3
PER_BRAND_TIMEOUT_SECONDS = 120.0
MIN_DOMAIN_THROTTLE_SECONDS = 1.0
SLOT_QUERIES = (
    ("ordinary", "camisa"),
    ("offers_section", "ofertas"),
    ("another_category", "calca"),
)

_SECRET = re.compile(
    r"(?i)\b(authorization|api[-_]?key|apikey|cookie|set-cookie|password|secret|"
    r"session|token|access[-_]?token|email|cpf)\b\s*[:=]\s*(?:bearer\s+)?[^\s&]+"
)
_QUERY_SECRET = re.compile(
    r"(?i)(token|key|secret|session|cookie|email|cpf|authorization|coupon|cupom)="
)
_MAX_DIAGNOSTIC = 300


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def sanitize_text(value: Any) -> str:
    text = _SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", str(value or ""))
    return text[:_MAX_DIAGNOSTIC]


def sanitize_url(value: str) -> str:
    """Retain public origin/path evidence, never query, fragment, userinfo or PII."""
    parsed = urlsplit(str(value or ""))
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme or "https", f"{host}{port}", parsed.path or "/", "", ""))


class AuditSample(BaseModel):
    slot: str
    selection_method: str
    url: str | None = None
    url_hash: str | None = None
    status: str
    offers: list[dict[str, Any]] = Field(default_factory=list)
    sources_attempted: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    limitation: str | None = None
    limitation_type: Literal[
        "candidate_unavailable",
        "candidate_blocked",
        "capability_unsupported",
        "brand_timeout",
    ] | None = None
    executed: bool = False
    functional_covered: bool = False
    duration_ms: int = 0

    @model_validator(mode="after")
    def validate_terminal_sample(self):
        if self.status not in {*ACCEPTED_STATUSES, "error"}:
            raise ValueError(f"unknown audit sample status: {self.status}")
        if not self.url and not self.limitation:
            raise ValueError("a sample without URL requires an explicit limitation")
        if self.limitation_type and not self.limitation:
            raise ValueError("a typed limitation requires explanatory text")
        if not self.url and not self.limitation_type:
            raise ValueError("a pre-probe limitation requires a typed reason")
        self.executed = bool(self.sources_attempted)
        # D-08 (GAP-01, Fase 03 Plano 13): uma fonte executada só conta como
        # cobertura FUNCIONAL se nenhum diagnóstico marcou contexto comercial
        # insuficiente (`insufficient_context:`) — chamada sem contexto para
        # avaliar a regra nunca é diagnóstico real, mesmo que a fonte tenha
        # sido de fato invocada.
        self.functional_covered = self.executed and not any(
            "insufficient_context:" in str(item) for item in self.diagnostics
        )
        return self


class BrandAuditReport(BaseModel):
    brand_key: str
    brand_name: str | None = None
    engine: str | None = None
    domain: str | None = None
    status: str
    samples: list[AuditSample] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    duration_ms: int = 0
    executed_samples: int = 0
    functional_samples: int = 0
    functionally_diagnosed: bool = False

    @model_validator(mode="after")
    def derive_functional_coverage(self):
        self.executed_samples = sum(sample.executed for sample in self.samples)
        self.functional_samples = sum(sample.functional_covered for sample in self.samples)
        self.functionally_diagnosed = self.functional_samples > 0
        return self


class AuditReport(BaseModel):
    schema_version: str = "1.0"
    captured_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    snapshot_hash: str | None = None
    expected_brands: list[str]
    observed_brands: list[str] = Field(default_factory=list)
    exclusions: dict[str, str] = Field(default_factory=dict)
    totals: dict[str, int] = Field(default_factory=dict)
    brands: list[BrandAuditReport] = Field(default_factory=list)
    limits: dict[str, Any] = Field(
        default_factory=lambda: {
            "max_products_per_brand": MAX_PRODUCTS_PER_BRAND,
            "global_concurrency": MAX_GLOBAL_CONCURRENCY,
            "per_domain_concurrency": 1,
            "domain_throttle_seconds": MIN_DOMAIN_THROTTLE_SECONDS,
            "brand_timeout_seconds": PER_BRAND_TIMEOUT_SECONDS,
        }
    )

    @model_validator(mode="after")
    def derive_metadata(self):
        self.expected_brands = sorted(dict.fromkeys(self.expected_brands))
        self.observed_brands = sorted(
            dict.fromkeys(row.brand_key for row in self.brands)
        )
        counts = Counter(row.status for row in self.brands)
        self.totals = {
            "brands": len(self.brands),
            "samples": sum(len(row.samples) for row in self.brands),
            "nominal_brands": len(self.observed_brands),
            "executed_samples": sum(row.executed_samples for row in self.brands),
            "functional_samples": sum(row.functional_samples for row in self.brands),
            "functional_brands": sum(row.functionally_diagnosed for row in self.brands),
            "limited_samples": sum(
                sample.limitation_type is not None
                for row in self.brands
                for sample in row.samples
            ),
            **{key: counts.get(key, 0) for key in sorted(ACCEPTED_STATUSES | {"error"})},
        }
        return self


def snapshot_active_own_brands(
    brands: Iterable[Any] | None = None,
) -> tuple[list[Any], dict[str, str]]:
    """Snapshot the dynamic active universe and explicitly exclude D-07 only."""
    source = list(brands) if brands is not None else brand_service.list_brands(active_only=True)
    active = [brand for brand in source if bool(_field(brand, "is_active", True))]
    own: list[Any] = []
    excluded: dict[str, str] = {}
    for brand in active:
        key = str(_field(brand, "brand_key") or "").casefold().strip()
        if key in MARKETPLACE_KEYS:
            excluded[key] = "marketplace excluded by D-07"
        else:
            own.append(brand)
    own.sort(key=lambda item: str(_field(item, "brand_key") or ""))
    return own, excluded


def _sample_url(url: str, slot: str, method: str) -> AuditSample:
    safe = sanitize_url(url)
    return AuditSample(
        slot=slot,
        selection_method=method,
        url=safe,
        url_hash=hashlib.sha256(safe.encode("utf-8")).hexdigest(),
        status="none_detected",
    )


def build_brand_report(
    brand: Any,
    *,
    candidates: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> BrandAuditReport:
    """Small deterministic constructor used by the CLI and its offline contract."""
    seen: set[str] = set()
    samples: list[AuditSample] = []
    for index, url in enumerate(candidates):
        safe = sanitize_url(url)
        if not safe or safe in seen or len(samples) >= MAX_PRODUCTS_PER_BRAND:
            continue
        seen.add(safe)
        slot = SLOT_QUERIES[min(index, len(SLOT_QUERIES) - 1)][0]
        samples.append(_sample_url(safe, slot, "engine_search"))
    explicit = [sanitize_text(item) for item in limitations if item]
    if not samples and not explicit:
        explicit = ["no distinct product candidate was available"]
    return BrandAuditReport(
        brand_key=str(_field(brand, "brand_key") or "").casefold(),
        brand_name=_field(brand, "brand_name"),
        engine=_field(brand, "engine"),
        domain=_field(brand, "domain"),
        status="none_detected" if samples else "unsupported",
        samples=samples,
        limitations=explicit,
    )


def exit_code(report: AuditReport) -> int:
    expected = set(report.expected_brands)
    observed = [row.brand_key for row in report.brands]
    if expected != set(observed) or len(observed) != len(set(observed)):
        return 1
    for brand in report.brands:
        if brand.status not in ACCEPTED_STATUSES:
            return 1
        if not brand.samples and not brand.limitations:
            return 1
        if len(brand.samples) > MAX_PRODUCTS_PER_BRAND:
            return 1
        urls = [sample.url for sample in brand.samples if sample.url]
        if len(urls) != len(set(urls)):
            return 1
        if any(sample.status not in ACCEPTED_STATUSES for sample in brand.samples):
            return 1
        if any(not sample.url and not sample.limitation for sample in brand.samples):
            return 1
        for sample in brand.samples:
            if sample.status in {"found", "none_detected"} and not sample.sources_attempted:
                return 1
            if sample.url and not sample.sources_attempted:
                return 1
            if not sample.url and not sample.limitation_type:
                return 1
            if sample.status in {"blocked", "requires_auth", "unsupported"}:
                if not sample.sources_attempted and not sample.limitation_type:
                    return 1
        if any(sample.url for sample in brand.samples) and not brand.functionally_diagnosed:
            return 1
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    if _SECRET.search(serialized) or _QUERY_SECRET.search(serialized):
        return 1
    return 0


class DomainThrottle:
    def __init__(self, minimum_seconds: float = MIN_DOMAIN_THROTTLE_SECONDS):
        self.minimum_seconds = max(MIN_DOMAIN_THROTTLE_SECONDS, minimum_seconds)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_started: dict[str, float] = {}

    async def run(self, domain: str, callback: Callable[[], Any]) -> Any:
        lock = self._locks.setdefault(domain.casefold(), asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last_started.get(domain.casefold(), 0.0)
            if elapsed < self.minimum_seconds:
                await asyncio.sleep(self.minimum_seconds - elapsed)
            self._last_started[domain.casefold()] = time.monotonic()
            value = callback()
            if hasattr(value, "__await__"):
                return await value
            return value


def _aggregate_brand_status(samples: Sequence[AuditSample]) -> str:
    # "unsupported" samples reaching this point come from _discover_candidate
    # (limitation_type="candidate_unavailable"): the slot simply had no distinct
    # product this run, not proof the engine lacks promotion capability. Any
    # status below represents a slot that actually executed a probe, so those
    # take precedence -- "unsupported" is only the aggregate outcome when every
    # slot failed to get a candidate at all.
    statuses = [sample.status for sample in samples]
    for status in ("error", "found", "blocked", "requires_auth", "none_detected"):
        if status in statuses:
            return status
    if "unsupported" in statuses:
        return "unsupported"
    return "error"


async def _discover_candidate(
    engine: Any,
    *,
    query: str,
    slot: str,
    domain: str,
    throttle: DomainThrottle,
    seen: set[str],
) -> AuditSample:
    started = time.monotonic()
    try:
        result = await throttle.run(
            domain,
            lambda: engine.search(query=query, max_results=5),
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        return AuditSample(
            slot=slot,
            selection_method=f"engine_search:{query}",
            status="blocked",
            limitation="candidate search timed out",
            limitation_type="candidate_blocked",
            diagnostics=[sanitize_text(type(exc).__name__)],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except (OSError, ConnectionError) as exc:
        return AuditSample(
            slot=slot,
            selection_method=f"engine_search:{query}",
            status="blocked",
            limitation="storefront could not be reached",
            limitation_type="candidate_blocked",
            diagnostics=[sanitize_text(type(exc).__name__)],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        return AuditSample(
            slot=slot,
            selection_method=f"engine_search:{query}",
            status="error",
            limitation="internal candidate discovery failure",
            diagnostics=[sanitize_text(f"{type(exc).__name__}: {exc}")],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    error = sanitize_text(_field(result, "error", ""))
    for product in _field(result, "products", []) or []:
        raw_url = str(_field(product, "url") or "")
        if not raw_url:
            continue
        safe = sanitize_url(raw_url)
        if safe in seen:
            continue
        parsed = urlsplit(safe)
        if not parsed.hostname or parsed.hostname.casefold() != domain.casefold():
            if not parsed.hostname or not parsed.hostname.casefold().endswith(f".{domain.casefold()}"):
                continue
        seen.add(safe)
        sample = _sample_url(safe, slot, f"engine_search:{query}")
        sample.duration_ms = int((time.monotonic() - started) * 1000)
        return sample
    return AuditSample(
        slot=slot,
        selection_method=f"engine_search:{query}",
        status="blocked" if error else "unsupported",
        limitation=(
            f"candidate search failed: {error}" if error else "no distinct candidate returned for this slot"
        ),
        diagnostics=[error] if error else [],
        limitation_type="candidate_blocked" if error else "candidate_unavailable",
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _offer_payload(result: ConditionalOfferResult) -> list[dict[str, Any]]:
    return [offer.model_dump(mode="json", exclude_none=True) for offer in result.offers]


async def _enrich_sample(
    brand: Any,
    sample: AuditSample,
    *,
    throttle: DomainThrottle,
) -> AuditSample:
    if not sample.url:
        return sample
    started = time.monotonic()
    brand_key = str(_field(brand, "brand_key") or "").casefold()
    domain = str(_field(brand, "domain") or "").casefold()
    try:
        result = await throttle.run(
            domain,
            lambda: promotion_orchestrator(
                brand=brand,
                brand_key=brand_key,
                url=sample.url,
                trigger="product_opened",
                force=True,
                level="full",
            ),
        )
        result = ConditionalOfferResult.model_validate(result)
        sample.status = result.status.value
        sample.offers = _offer_payload(result)
        sample.sources_attempted = [sanitize_text(item) for item in result.sources_attempted]
        sample.diagnostics = [sanitize_text(item) for item in result.diagnostics]
    except (asyncio.TimeoutError, TimeoutError):
        sample.status = "blocked"
        sample.diagnostics = ["PDP conditional-offer probe timed out"]
    except (OSError, ConnectionError) as exc:
        sample.status = "blocked"
        sample.diagnostics = [sanitize_text(type(exc).__name__)]
    except Exception as exc:
        sample.status = "error"
        sample.diagnostics = [sanitize_text(f"{type(exc).__name__}: {exc}")]
    sample.duration_ms += int((time.monotonic() - started) * 1000)
    return sample


async def audit_brand(
    brand: Any,
    *,
    throttle: DomainThrottle,
    max_products: int = MAX_PRODUCTS_PER_BRAND,
) -> BrandAuditReport:
    started = time.monotonic()
    key = str(_field(brand, "brand_key") or "").casefold()
    engine_name = str(_field(brand, "engine") or "").casefold()
    domain = str(_field(brand, "domain") or "").casefold()
    resolved = resolve_brand(brand)
    if resolved.status != "ready":
        limitation = sanitize_text("; ".join(resolved.diagnostics) or "no proven promotion source")
        samples = [
            AuditSample(
                slot=slot,
                selection_method="capability_registry",
                status="unsupported",
                limitation=limitation,
                limitation_type="capability_unsupported",
            )
            for slot, _query in SLOT_QUERIES[:max_products]
        ]
        return BrandAuditReport(
            brand_key=key,
            brand_name=_field(brand, "brand_name"),
            engine=engine_name,
            domain=domain,
            status="unsupported",
            samples=samples,
            limitations=[limitation],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        engine = engine_factory.get_engine(key)
    except Exception as exc:
        return BrandAuditReport(
            brand_key=key,
            brand_name=_field(brand, "brand_name"),
            engine=engine_name,
            domain=domain,
            status="error",
            limitations=[sanitize_text(f"engine resolution failed: {type(exc).__name__}: {exc}")],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    seen: set[str] = set()
    samples: list[AuditSample] = []
    for slot, query in SLOT_QUERIES[:max_products]:
        sample = await _discover_candidate(
            engine,
            query=query,
            slot=slot,
            domain=domain,
            throttle=throttle,
            seen=seen,
        )
        samples.append(sample)

    for index, sample in enumerate(samples):
        if sample.url:
            samples[index] = await _enrich_sample(brand, sample, throttle=throttle)

    limitations = [
        f"{sample.slot}: {sample.limitation}" for sample in samples if sample.limitation
    ]
    return BrandAuditReport(
        brand_key=key,
        brand_name=_field(brand, "brand_name"),
        engine=engine_name,
        domain=domain,
        status=_aggregate_brand_status(samples),
        samples=samples,
        limitations=limitations,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def audit_universe(
    brands: Sequence[Any],
    *,
    max_products: int = MAX_PRODUCTS_PER_BRAND,
    timeout_seconds: float = PER_BRAND_TIMEOUT_SECONDS,
    concurrency: int = MAX_GLOBAL_CONCURRENCY,
    throttle_seconds: float = MIN_DOMAIN_THROTTLE_SECONDS,
) -> list[BrandAuditReport]:
    semaphore = asyncio.Semaphore(min(MAX_GLOBAL_CONCURRENCY, max(1, concurrency)))
    throttle = DomainThrottle(max(MIN_DOMAIN_THROTTLE_SECONDS, throttle_seconds))

    async def guarded(brand: Any) -> BrandAuditReport:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    audit_brand(brand, throttle=throttle, max_products=max_products),
                    timeout=timeout_seconds,
                )
            except (asyncio.TimeoutError, TimeoutError):
                return BrandAuditReport(
                    brand_key=str(_field(brand, "brand_key") or "").casefold(),
                    brand_name=_field(brand, "brand_name"),
                    engine=_field(brand, "engine"),
                    domain=_field(brand, "domain"),
                    status="blocked",
                    samples=[
                        AuditSample(
                            slot=slot,
                            selection_method="brand_timeout",
                            status="blocked",
                            limitation=f"brand audit exceeded {timeout_seconds:g}s",
                            limitation_type="brand_timeout",
                        )
                        for slot, _query in SLOT_QUERIES[:max_products]
                    ],
                    limitations=[f"brand audit exceeded {timeout_seconds:g}s"],
                    duration_ms=int(timeout_seconds * 1000),
                )
            except Exception as exc:
                return BrandAuditReport(
                    brand_key=str(_field(brand, "brand_key") or "").casefold(),
                    brand_name=_field(brand, "brand_name"),
                    engine=_field(brand, "engine"),
                    domain=_field(brand, "domain"),
                    status="error",
                    limitations=[sanitize_text(f"internal audit failure: {type(exc).__name__}: {exc}")],
                )

    return list(await asyncio.gather(*(guarded(brand) for brand in brands)))


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def finalize_report(report: AuditReport) -> dict[str, Any]:
    report = AuditReport.model_validate(report.model_dump(mode="json"))
    base = report.model_dump(mode="json")
    base.pop("snapshot_hash", None)
    report.snapshot_hash = hashlib.sha256(canonical_json(base).encode("utf-8")).hexdigest()
    return report.model_dump(mode="json")


def render_json(payload: dict[str, Any]) -> bytes:
    return (canonical_json(payload) + "\n").encode("utf-8")


def render_markdown(payload: dict[str, Any]) -> bytes:
    metadata = {
        key: payload[key]
        for key in (
            "snapshot_hash",
            "captured_at",
            "expected_brands",
            "observed_brands",
            "totals",
        )
    }
    lines = [
        "# Phase 03 — Auditoria de cobertura de ofertas condicionais",
        "",
        f"<!-- audit-metadata: {json.dumps(metadata, sort_keys=True, ensure_ascii=False)} -->",
        "",
        f"- Capturada em: `{payload['captured_at']}`",
        f"- Snapshot SHA-256: `{payload['snapshot_hash']}`",
        f"- Cobertura: **{len(payload['observed_brands'])}/{len(payload['expected_brands'])}** marcas próprias ativas",
        f"- Cobertura nominal: **{payload['totals']['nominal_brands']}/{len(payload['expected_brands'])}** marcas",
        f"- Cobertura funcional: **{payload['totals']['functional_brands']}/{len(payload['expected_brands'])}** marcas; **{payload['totals']['functional_samples']}** amostras com fonte executada",
        "- Evidência substituída: o snapshot do 03-07 provava cobertura nominal, mas não funcional; este snapshot pós-fix exige fonte executada ou limitação pre-probe tipada.",
        f"- Gate: **{'PASS' if exit_code(AuditReport.model_validate(payload)) == 0 else 'FAIL'}**",
        "",
        "## Exclusões D-07",
        "",
    ]
    for key, reason in sorted(payload.get("exclusions", {}).items()):
        lines.append(f"- `{key}` — {reason}")
    lines.extend(
        [
            "",
            "## Cobertura",
            "",
            "| Marca | Engine | Status | Cobertura nominal | Cobertura funcional | Amostras | Limitações |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for brand in payload["brands"]:
        limitations = "; ".join(brand.get("limitations") or []) or "—"
        lines.append(
            f"| `{brand['brand_key']}` | {brand.get('engine') or '—'} | "
            f"`{brand['status']}` | sim | "
            f"{'sim' if brand.get('functionally_diagnosed') else 'não (limitação pre-probe)'} | "
            f"{len(brand.get('samples') or [])} | {limitations} |"
        )
    lines.extend(["", "## Amostras", ""])
    for brand in payload["brands"]:
        lines.extend([f"### {brand['brand_key']}", ""])
        for sample in brand.get("samples") or []:
            evidence = sample.get("url") or sample.get("limitation") or "—"
            lines.append(
                f"- `{sample['slot']}` — `{sample['status']}` — {evidence} "
                f"({len(sample.get('offers') or [])} oferta(s)); fontes: "
                f"{', '.join(sample.get('sources_attempted') or []) or 'nenhuma (pre-probe)'}"
            )
        lines.append("")
    lines.extend(
        [
            "## Verificação D-01..D-18",
            "",
            "- D-01..D-05: contratos e parsers offline da fixture Phase 03.",
            "- D-06..D-09: universo dinâmico, exclusões e matriz desta auditoria.",
            "- D-10..D-14: seleção fast e enriquecimento full/confirm bounded por PDP.",
            "- D-15..D-18: build, UAT visual aprovada e contratos de monitor/histórico.",
            "",
            "Ausência legítima (`none_detected`), fonte não suportada, bloqueio e autenticação são diagnósticos honestos; omissão ou `error` interno falham o gate.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def _fsync_file(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        # Windows does not support fsync on a directory handle.  os.replace is
        # still atomic on the same volume; file fsync remains mandatory.
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temporary(destination: Path, content: bytes, label: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.{label}.",
        suffix=".tmp",
    )
    temporary = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            _fsync_file(handle)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _backup_destination(destination: Path) -> tuple[Path | None, os.stat_result | None]:
    if not destination.exists():
        return None, None
    metadata = destination.stat()
    backup = _write_temporary(destination, destination.read_bytes(), "backup")
    os.chmod(backup, stat.S_IMODE(metadata.st_mode))
    os.utime(backup, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    return backup, metadata


def _replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def publish_transaction(
    *,
    destinations: Sequence[tuple[Path, bytes]],
) -> None:
    """Publish snapshot, Markdown and latest with full rollback on any failure."""
    resolved = [destination.resolve() for destination, _content in destinations]
    if len(resolved) != len(set(resolved)):
        counts = Counter(resolved)
        duplicates = sorted(str(path) for path, count in counts.items() if count > 1)
        raise ValueError(
            "publish_transaction destinations must be pairwise distinct paths; "
            f"duplicate(s): {duplicates}"
        )
    staged: dict[Path, Path] = {}
    backups: dict[Path, tuple[Path | None, os.stat_result | None]] = {}
    existed = {destination: destination.exists() for destination, _content in destinations}
    replaced: list[Path] = []
    try:
        for destination, content in destinations:
            staged[destination] = _write_temporary(destination, content, "staged")
        for destination, _content in destinations:
            backups[destination] = _backup_destination(destination)
        for destination, _content in destinations:
            _replace(staged[destination], destination)
            replaced.append(destination)
        for parent in dict.fromkeys(destination.parent for destination, _ in destinations):
            _fsync_directory(parent)
    except Exception:
        for destination, _content in reversed(destinations):
            backup, metadata = backups.get(destination, (None, None))
            try:
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
                    if metadata is not None:
                        os.chmod(destination, stat.S_IMODE(metadata.st_mode))
                        os.utime(destination, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                elif not existed[destination]:
                    destination.unlink(missing_ok=True)
            except Exception as rollback_error:
                LOGGER.critical("rollback failed for %s: %s", destination, rollback_error)
        raise
    finally:
        for path in [*staged.values(), *(item[0] for item in backups.values())]:
            if path is not None:
                path.unlink(missing_ok=True)
    for parent in dict.fromkeys(destination.parent for destination, _ in destinations):
        _fsync_directory(parent)


def _timestamp_name(captured_at: str) -> str:
    parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_candidate(payload: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"conditional_offers_{_timestamp_name(payload['captured_at'])}.json"
    if path.exists():
        raise FileExistsError(f"candidate snapshot collision: {path}")
    temporary = _write_temporary(path, render_json(payload), "candidate")
    _replace(temporary, path)
    _fsync_directory(path.parent)
    return path


def publish_final(
    payload: dict[str, Any],
    output_dir: Path,
    markdown_path: Path,
    *,
    canonical_bytes: bytes | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = output_dir / f"conditional_offers_{_timestamp_name(payload['captured_at'])}.json"
    latest = output_dir / "conditional_offers_latest.json"
    if snapshot.exists():
        raise FileExistsError(f"dated snapshot collision: {snapshot}")
    # Render every byte before any destination or temporary is mutated.
    snapshot_bytes = canonical_bytes if canonical_bytes is not None else render_json(payload)
    markdown_bytes = render_markdown(payload)
    latest_bytes = snapshot_bytes
    publish_transaction(
        destinations=(
            (snapshot, snapshot_bytes),
            (markdown_path, markdown_bytes),
            (latest, latest_bytes),  # commit marker: always last
        )
    )
    return snapshot


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/audits"))
    parser.add_argument("--brand", action="append", default=[])
    parser.add_argument("--max-products", type=int, default=MAX_PRODUCTS_PER_BRAND)
    parser.add_argument("--timeout", type=float, default=PER_BRAND_TIMEOUT_SECONDS)
    parser.add_argument("--concurrency", type=int, default=MAX_GLOBAL_CONCURRENCY)
    parser.add_argument("--throttle", type=float, default=MIN_DOMAIN_THROTTLE_SECONDS)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--candidate", action="store_true")
    modes.add_argument("--promote-candidate", type=Path)
    modes.add_argument("--verify-latest", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    if args.max_products < 1 or args.max_products > MAX_PRODUCTS_PER_BRAND:
        parser.error("--max-products must be between 1 and 3")
    if args.concurrency < 1 or args.concurrency > MAX_GLOBAL_CONCURRENCY:
        parser.error("--concurrency must be between 1 and 2")
    if args.throttle < MIN_DOMAIN_THROTTLE_SECONDS:
        parser.error("--throttle must be at least 1 second")
    if args.timeout <= 0 or args.timeout > PER_BRAND_TIMEOUT_SECONDS:
        parser.error("--timeout must be greater than zero and at most 120 seconds")
    if (args.promote_candidate or args.verify_latest) and args.markdown is None:
        parser.error("offline promote/verify modes require --markdown")
    if args.candidate and args.markdown is not None:
        parser.error("--candidate cannot write Markdown")
    return args


def _select_brands(own: Sequence[Any], requested: Sequence[str]) -> list[Any]:
    if not requested:
        return list(own)
    normalized = [item.casefold().strip() for item in requested]
    by_key = {str(_field(brand, "brand_key") or "").casefold(): brand for brand in own}
    unknown = sorted(set(normalized) - set(by_key))
    if unknown:
        raise ValueError(f"unknown or excluded --brand value(s): {', '.join(unknown)}")
    return [by_key[key] for key in dict.fromkeys(normalized)]


def _active_universe_keys() -> list[str]:
    own, _exclusions = snapshot_active_own_brands()
    return sorted(str(_field(brand, "brand_key") or "").casefold() for brand in own)


def _validated_artifact(
    path: Path,
    *,
    expected_parent: Path,
) -> tuple[bytes, dict[str, Any], AuditReport]:
    resolved = path.resolve(strict=True)
    if resolved.parent != expected_parent.resolve() or not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("audit artifact must be a regular file in the expected directory")
    raw = resolved.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    report = AuditReport.model_validate(payload)
    normalized = report.model_dump(mode="json")
    if payload != normalized or raw != render_json(normalized):
        raise ValueError("audit artifact is not canonical AuditReport JSON")
    claimed_hash = normalized.get("snapshot_hash")
    base = dict(normalized)
    base.pop("snapshot_hash", None)
    actual_hash = hashlib.sha256(canonical_json(base).encode("utf-8")).hexdigest()
    if claimed_hash != actual_hash:
        raise ValueError("snapshot_hash mismatch")
    expected_name = f"conditional_offers_{_timestamp_name(report.captured_at)}.json"
    if resolved.name != expected_name and resolved.name != "conditional_offers_latest.json":
        raise ValueError("artifact filename does not match captured_at")
    if report.expected_brands != _active_universe_keys():
        raise ValueError("active own-brand universe drift")
    if exit_code(report) != 0:
        raise ValueError("audit functional gates failed")
    return raw, normalized, report


def promote_candidate(
    candidate_path: Path,
    *,
    output_dir: Path,
    markdown_path: Path,
) -> tuple[int, Path, AuditReport]:
    candidate_dir = output_dir.resolve() / "candidates"
    raw, payload, report = _validated_artifact(
        candidate_path,
        expected_parent=candidate_dir,
    )
    snapshot = publish_final(
        payload,
        output_dir,
        markdown_path,
        canonical_bytes=raw,
    )
    return 0, snapshot, report


def verify_latest(
    latest_path: Path,
    *,
    markdown_path: Path,
) -> tuple[int, Path, AuditReport]:
    output_dir = latest_path.resolve(strict=True).parent
    latest_bytes, payload, report = _validated_artifact(
        latest_path,
        expected_parent=output_dir,
    )
    timestamp = _timestamp_name(report.captured_at)
    snapshot = output_dir / f"conditional_offers_{timestamp}.json"
    candidate = output_dir / "candidates" / f"conditional_offers_{timestamp}.json"
    if not snapshot.is_file() or snapshot.read_bytes() != latest_bytes:
        raise ValueError("dated snapshot is missing or differs from latest")
    if not candidate.is_file() or candidate.read_bytes() != latest_bytes:
        raise ValueError("approved candidate is missing or differs from latest")
    if not markdown_path.is_file() or markdown_path.read_bytes() != render_markdown(payload):
        raise ValueError("Markdown evidence differs from canonical report")
    return 0, latest_path, report


async def run_live(args: argparse.Namespace) -> tuple[int, Path | None, AuditReport]:
    own, exclusions = snapshot_active_own_brands()
    selected = _select_brands(own, args.brand)
    expected = [str(_field(brand, "brand_key") or "").casefold() for brand in selected]
    artifact: Path | None = None
    try:
        rows = await audit_universe(
            selected,
            max_products=args.max_products,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
            throttle_seconds=args.throttle,
        )
        report = AuditReport(
            expected_brands=expected,
            exclusions=exclusions,
            brands=rows,
        )
        payload = finalize_report(report)
        report = AuditReport.model_validate(payload)
        if args.candidate:
            artifact = write_candidate(payload, args.output)
        return exit_code(report), artifact, report
    finally:
        await SessionManager.close_session()
        await BrowserManager.close()


async def run(args: argparse.Namespace) -> tuple[int, Path | None, AuditReport]:
    return await run_live(args)


async def dispatch(argv: Sequence[str] | None = None) -> tuple[int, Path | None, AuditReport]:
    args = _parse_args(argv)
    if args.promote_candidate is not None:
        return promote_candidate(
            args.promote_candidate,
            output_dir=args.output,
            markdown_path=args.markdown,
        )
    if args.verify_latest is not None:
        return verify_latest(args.verify_latest, markdown_path=args.markdown)
    return await run(args)


async def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        code, artifact, report = await dispatch(argv)
    except Exception as exc:
        LOGGER.error("AUDIT_FAILED=%s", sanitize_text(f"{type(exc).__name__}: {exc}"))
        return 2
    if artifact is not None:
        print(f"REPORT_FILE={artifact.resolve()}")
    print(
        json.dumps(
            {
                "expected_brands": report.expected_brands,
                "observed_brands": report.observed_brands,
                "totals": report.totals,
                "snapshot_hash": report.snapshot_hash,
                "exit_code": code,
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
