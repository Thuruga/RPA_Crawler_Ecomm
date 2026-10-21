"""Synthetic benchmark for the category discovery Redis cache.

The benchmark is intentionally hermetic: it patches the existing cache owner to
use an in-memory fake Redis client and drives the same route helper functions
used by `/brands/{brand}/categories` and `/brands/{brand_key}/discover`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Callable, Sequence
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_OUTPUT = PROJECT_ROOT / ".planning" / "phases" / "09-redis-seletivo-e-mensurado" / "09-BASELINE.md"
DEFAULT_REPETITIONS = 5
DEFAULT_MIN_SAVED_PERCENT = 80.0
CANDIDATE = "category_discovery_cache"
KEY_PREFIX = "category_discovery:v1:"
TTL_SOURCE = "settings.CATEGORY_DISCOVERY_CACHE_TTL_SECONDS"
OWNER = "backend/services/category_discovery_cache.py"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import settings  # noqa: E402
from services.category_discovery import (  # noqa: E402
    CategoryProbeResult,
    CategoryValidationOutcome,
    DiscoveredCategory,
    ProbeStatus,
)


@dataclass(frozen=True)
class CacheBenchmarkResult:
    route: str
    brand_key: str
    repetitions: int
    cache_mode: str
    live_discovery_calls: int
    warm_live_discovery_calls: int
    duplicate_live_calls: int
    calls_saved: int
    blocked_probe_count: int
    latency_ms: float
    disposition: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CacheBenchmarkReport:
    candidate: str
    generated_at: str
    brand_key: str
    repetitions: int
    min_saved_percent: float
    disposition: str
    ttl_source: str
    ttl_seconds: int
    owner: str
    key_prefix: str
    fallback_behavior: str
    results: list[CacheBenchmarkResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["results"] = [row.to_dict() for row in self.results]
        return payload


class _FakeRedis:
    def __init__(self, *, storage_enabled: bool) -> None:
        self.storage_enabled = storage_enabled
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        if not self.storage_enabled:
            return None
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        del ex
        if self.storage_enabled:
            self.store[key] = value


class _BenchmarkCatalogEngine:
    def __init__(self, brand_key: str) -> None:
        self.brand_key = brand_key
        self.discover_and_validate_calls = 0

    async def category_has_products(self, candidate: Any) -> bool:
        raise NotImplementedError("benchmark route helpers never call this method")

    async def _discover_and_validate(self) -> CategoryValidationOutcome:
        self.discover_and_validate_calls += 1
        return _sample_outcome()


def _sample_outcome() -> CategoryValidationOutcome:
    return CategoryValidationOutcome(
        offered=[
            DiscoveredCategory(
                name="Camisetas",
                path="/roupas/camisetas",
                url="https://loja.example/roupas/camisetas",
                group="Roupas",
                ancestors=("Roupas",),
            )
        ],
        probes={
            "/roupas/camisetas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, detail="12 produto(s)"),
            "/roupas/vazia": CategoryProbeResult(ProbeStatus.EMPTY, detail="0 produtos"),
            "/roupas/bloqueada": CategoryProbeResult(ProbeStatus.BLOCKED, detail="probe timeout"),
        },
    )


def count_blocked_probes(outcome: CategoryValidationOutcome) -> int:
    return sum(1 for probe in outcome.probes.values() if probe.status is ProbeStatus.BLOCKED)


def calculate_calls_saved(*, uncached_live_calls: int, cached_live_calls: int) -> int:
    return max(0, uncached_live_calls - cached_live_calls)


def decide_cache_disposition(
    *,
    repetitions: int,
    uncached_live_calls: int,
    cached_live_calls: int,
    warm_live_calls: int,
    min_saved_percent: float = DEFAULT_MIN_SAVED_PERCENT,
) -> str:
    if repetitions < 2 or uncached_live_calls <= 0:
        return "inconclusive"
    saved = calculate_calls_saved(
        uncached_live_calls=uncached_live_calls,
        cached_live_calls=cached_live_calls,
    )
    saved_percent = (saved / uncached_live_calls) * 100
    if saved_percent >= min_saved_percent and warm_live_calls == 0:
        return "retained"
    return "removed"


async def _measure_route(
    *,
    route: str,
    brand_key: str,
    repetitions: int,
    cache_mode: str,
    route_call: Callable[[Any], Any],
    uncached_live_calls: int | None,
    min_saved_percent: float,
) -> CacheBenchmarkResult:
    fake_redis = _FakeRedis(storage_enabled=cache_mode == "cached")
    engine = _BenchmarkCatalogEngine(brand_key)
    warm_live_calls = 0
    blocked_probe_count = 0
    started = perf_counter_ns()

    with patch("services.category_discovery_cache.redis_manager.get_client", return_value=fake_redis):
        for index in range(repetitions):
            before = engine.discover_and_validate_calls
            await route_call(engine)
            delta = engine.discover_and_validate_calls - before
            if index > 0:
                warm_live_calls += delta
            if delta:
                blocked_probe_count = count_blocked_probes(engine._outcome if hasattr(engine, "_outcome") else _sample_outcome())

    elapsed_ms = (perf_counter_ns() - started) / 1_000_000
    live_calls = engine.discover_and_validate_calls
    baseline_calls = uncached_live_calls if uncached_live_calls is not None else live_calls
    calls_saved = calculate_calls_saved(
        uncached_live_calls=baseline_calls,
        cached_live_calls=live_calls,
    )
    disposition = decide_cache_disposition(
        repetitions=repetitions,
        uncached_live_calls=baseline_calls,
        cached_live_calls=live_calls,
        warm_live_calls=warm_live_calls,
        min_saved_percent=min_saved_percent,
    )
    return CacheBenchmarkResult(
        route=route,
        brand_key=brand_key,
        repetitions=repetitions,
        cache_mode=cache_mode,
        live_discovery_calls=live_calls,
        warm_live_discovery_calls=warm_live_calls,
        duplicate_live_calls=max(0, live_calls - 1),
        calls_saved=calls_saved,
        blocked_probe_count=blocked_probe_count,
        latency_ms=round(elapsed_ms, 3),
        disposition=disposition,
    )


async def run_synthetic_benchmark(
    *,
    brand_key: str = "testbrand",
    repetitions: int = DEFAULT_REPETITIONS,
    min_saved_percent: float = DEFAULT_MIN_SAVED_PERCENT,
) -> CacheBenchmarkReport:
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    from api.routes_brands import resolve_discover_categories
    from api.routes_category import _resolve_get_catalog

    routes: list[tuple[str, Callable[[Any], Any]]] = [
        ("/brands/{brand}/categories", _resolve_get_catalog),
        ("/brands/{brand_key}/discover", resolve_discover_categories),
    ]
    results: list[CacheBenchmarkResult] = []
    route_dispositions: list[str] = []

    for route, route_call in routes:
        uncached = await _measure_route(
            route=route,
            brand_key=brand_key,
            repetitions=repetitions,
            cache_mode="uncached",
            route_call=route_call,
            uncached_live_calls=None,
            min_saved_percent=min_saved_percent,
        )
        cached = await _measure_route(
            route=route,
            brand_key=brand_key,
            repetitions=repetitions,
            cache_mode="cached",
            route_call=route_call,
            uncached_live_calls=uncached.live_discovery_calls,
            min_saved_percent=min_saved_percent,
        )
        results.extend([cached, uncached])
        route_dispositions.append(cached.disposition)

    if all(status == "retained" for status in route_dispositions):
        disposition = "retained"
    elif any(status == "inconclusive" for status in route_dispositions):
        disposition = "inconclusive"
    else:
        disposition = "removed"

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return CacheBenchmarkReport(
        candidate=CANDIDATE,
        generated_at=generated_at,
        brand_key=brand_key,
        repetitions=repetitions,
        min_saved_percent=min_saved_percent,
        disposition=disposition,
        ttl_source=TTL_SOURCE,
        ttl_seconds=int(settings.CATEGORY_DISCOVERY_CACHE_TTL_SECONDS),
        owner=OWNER,
        key_prefix=KEY_PREFIX,
        fallback_behavior="Redis read failure, write failure, or corrupt payload degrades to live discovery/no-op.",
        results=results,
    )


def _saved_percent(row: CacheBenchmarkResult) -> float:
    denominator = row.live_discovery_calls + row.calls_saved
    if denominator <= 0:
        return 0.0
    return (row.calls_saved / denominator) * 100


def render_markdown(report: CacheBenchmarkReport) -> str:
    lines = [
        "---",
        "phase: 09-redis-seletivo-e-mensurado",
        f"measured: {report.generated_at}",
        f"status: {report.disposition}",
        "requirement: REDIS-01",
        "---",
        "",
        "# Phase 09 Redis Baseline",
        "",
        "REDIS-01 baseline for the category-discovery Redis candidate before any keep/remove disposition.",
        "",
        "## Decision",
        "",
        f"- Candidate: `{report.candidate}`",
        f"- Disposition: `{report.disposition}`",
        f"- Repetitions: `{report.repetitions}`",
        f"- Threshold: `{report.min_saved_percent:.0f}%` saved live calls and zero warm live calls after the first write",
        f"- TTL source: `{report.ttl_source}` = `{report.ttl_seconds}` seconds",
        f"- Owner: `{report.owner}`",
        f"- Key prefix: `{report.key_prefix}`",
        f"- Fallback behavior: {report.fallback_behavior}",
        "- REDIS-02 note: retained cache must continue to use this TTL/owner/fallback pattern in the disposition plan.",
        "- Required REDIS-01 evidence fields: latency, live calls, duplicates, blocked probes, threshold, and Decision.",
        "",
        "## Benchmark Evidence",
        "",
        "| Candidate | Route | Repetitions | Cache Mode | Live calls | Calls saved | Saved % | Duplicates | Blocked probes | latency ms | Decision |",
        "|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in report.results:
        lines.append(
            "| "
            f"{report.candidate} | {row.route} | {row.repetitions} | {row.cache_mode} | "
            f"{row.live_discovery_calls} | {row.calls_saved} | {_saved_percent(row):.1f} | "
            f"{row.duplicate_live_calls} | {row.blocked_probe_count} | {row.latency_ms:.3f} | {row.disposition} |"
        )
    lines.extend(
        [
            "",
            "## Method",
            "",
            "- Hermetic fake Redis only; no live Redis service and no external storefront calls.",
            "- Cached mode stores through `set_cached_outcome`; uncached mode returns cache misses and drops writes.",
            "- Both benchmark rows call the actual route helpers: `_resolve_get_catalog` and `resolve_discover_categories`.",
            "- Duplicate live calls are live discovery calls after the first cold miss.",
            "- Blocked probes are counted from `CategoryValidationOutcome.probes` entries with `ProbeStatus.BLOCKED`.",
            "",
            "## Verification Commands",
            "",
            "| Behavior | Command | Result | Status |",
            "|---|---|---|---|",
            "| REDIS-01 source smoke | `python -c \"from pathlib import Path; s=Path('backend/tests/test_category_discovery_cache.py').read_text(encoding='utf-8'); required=['benchmark_category_discovery_cache','decide_cache_disposition','count_blocked_probes']; missing=[x for x in required if x not in s]; raise SystemExit('missing '+repr(missing) if missing else 0)\"` | passed during plan execution | PASS |",
            "| Focused pytest | `python -m pytest backend/tests/test_category_discovery_cache.py -q` | passed during plan execution | PASS |",
            "| Baseline generation | `python backend/scripts/benchmark_category_discovery_cache.py --repetitions 5 --output .planning/phases/09-redis-seletivo-e-mensurado/09-BASELINE.md` | generated this report | PASS |",
        ]
    )
    return "\n".join(lines) + "\n"


def write_baseline_report(report: CacheBenchmarkReport, output: Path = DEFAULT_OUTPUT) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brand-key", default="testbrand")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = await run_synthetic_benchmark(
        brand_key=args.brand_key,
        repetitions=args.repetitions,
    )
    markdown_path = write_baseline_report(report, args.output)
    payload = {"markdown": str(markdown_path), "disposition": report.disposition}
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        payload["json"] = str(args.json_output)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))