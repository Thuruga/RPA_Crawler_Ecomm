"""Closed promotion-probe registry and brand/platform resolver."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from core.constants import CONDITIONAL_OFFER_EXCLUDED_MARKETPLACES
from core.models import (
    ConditionalOfferResult,
    OfferProbeLevel,
    PromotionAdapter,
    PromotionProbeConfig,
    PromotionProbeType,
)
from services.promotions.adapters import html, reserva_prime, shopify, vtex


AdapterFactory = Callable[..., Any]
PROMOTION_ADAPTERS: dict[str, AdapterFactory] = {
    "vtex": vtex.parse_teasers,
    "shopify": shopify.parse_product,
    "reserva_prime": reserva_prime.parse_rule,
    "html": html.parse,
}

_ENGINE_DEFAULTS: dict[str, tuple[str, ...]] = {
    "vtex": ("vtex",),
    "shopify": ("shopify",),
    "wake": (),
    "zara": (),
    "sfcc": ("html",),
    "privalia": ("html",),
}
_MARKETPLACES = CONDITIONAL_OFFER_EXCLUDED_MARKETPLACES


@dataclass(frozen=True)
class ResolvedProbe:
    """Closed runtime descriptor for one known promotion source."""

    adapter: PromotionAdapter
    probe_type: PromotionProbeType
    config: PromotionProbeConfig
    source_name: str


@dataclass(frozen=True)
class ResolvedPromotionProbes:
    probes: tuple[ResolvedProbe, ...] = ()
    configs: tuple[PromotionProbeConfig, ...] = ()
    status: str = "ready"
    diagnostics: tuple[str, ...] = ()


def resolve_adapter(name: str) -> AdapterFactory:
    try:
        return PROMOTION_ADAPTERS[name]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unsupported promotion adapter: {name}") from exc


def _source_name(config: PromotionProbeConfig) -> str:
    adapter = config.adapter.value if config.adapter is not None else config.type.value
    return f"{adapter}:{config.path}" if config.path else config.type.value


def _descriptor(config: PromotionProbeConfig) -> ResolvedProbe:
    if config.adapter is None:
        raise ValueError(f"promotion probe {config.type.value} has no runtime adapter")
    resolve_adapter(config.adapter.value)
    return ResolvedProbe(
        adapter=config.adapter,
        probe_type=config.type,
        config=config,
        source_name=_source_name(config),
    )


def _configured_probes(
    configs: Iterable[PromotionProbeConfig | dict[str, Any]],
) -> tuple[tuple[ResolvedProbe, ...], tuple[PromotionProbeConfig, ...]]:
    parsed = tuple(
        item if isinstance(item, PromotionProbeConfig) else PromotionProbeConfig.model_validate(item)
        for item in configs
    )
    return tuple(_descriptor(item) for item in parsed), parsed


def _default_configs(engine: str) -> tuple[PromotionProbeConfig, ...]:
    if engine == "vtex":
        return (
            PromotionProbeConfig(
                type=PromotionProbeType.VTEX_STRUCTURED,
                adapter=PromotionAdapter.VTEX,
                level=OfferProbeLevel.FAST,
            ),
        )
    if engine == "shopify":
        return (
            PromotionProbeConfig(
                type=PromotionProbeType.SHOPIFY_STRUCTURED,
                adapter=PromotionAdapter.SHOPIFY,
                level=OfferProbeLevel.FAST,
            ),
        )
    if engine in ("sfcc", "privalia"):
        return (
            PromotionProbeConfig(
                type=PromotionProbeType.PUBLIC_HTML,
                adapter=PromotionAdapter.HTML,
                level=OfferProbeLevel.FULL,
            ),
        )
    return ()


def resolve_for_engine(
    engine: str | None,
    *,
    brand_key: str,
    promotion_probes: Iterable[PromotionProbeConfig | dict[str, Any]] = (),
) -> ResolvedPromotionProbes:
    normalized_engine = (engine or "").casefold()
    normalized_brand = brand_key.casefold()
    if normalized_engine in _MARKETPLACES or normalized_brand in _MARKETPLACES:
        return ResolvedPromotionProbes(
            status="unsupported",
            diagnostics=("marketplace excluded from conditional-promotion coverage",),
        )
    if normalized_engine not in _ENGINE_DEFAULTS:
        return ResolvedPromotionProbes(
            status="unsupported",
            diagnostics=(f"unsupported promotion engine: {normalized_engine or 'missing'}",),
        )
    default_configs = _default_configs(normalized_engine)
    defaults = tuple(_descriptor(config) for config in default_configs)
    configured, parsed = _configured_probes(promotion_probes)
    probes_by_name = {probe.source_name: probe for probe in (*defaults, *configured)}
    probes = tuple(probes_by_name.values())
    configs = tuple(probe.config for probe in probes)
    if probes:
        return ResolvedPromotionProbes(probes=probes, configs=configs)
    return ResolvedPromotionProbes(
        status="unsupported",
        configs=parsed,
        diagnostics=(f"no proven public promotion source for {normalized_engine}",),
    )


def resolve_brand(brand: Any) -> ResolvedPromotionProbes:
    getter = brand.get if isinstance(brand, dict) else lambda key, default=None: getattr(brand, key, default)
    return resolve_for_engine(
        getter("engine"),
        brand_key=getter("brand_key", "unknown"),
        promotion_probes=getter("promotion_probes", ()),
    )


class _ConfirmCapability:
    async def should_run(self, *, advertised: Iterable[Any], trigger: str) -> bool:
        return bool(tuple(advertised)) and trigger in {
            "product_opened", "monitor_started", "manual_revalidate"
        }


def get_confirm_probe() -> _ConfirmCapability:
    return _ConfirmCapability()


async def run_level(
    level: str,
    *,
    trigger: str,
    browser: Callable[..., Any] | None = None,
    checkout: Callable[..., Any] | None = None,
) -> ConditionalOfferResult:
    parsed_level = OfferProbeLevel(level)
    if parsed_level == OfferProbeLevel.FAST:
        return ConditionalOfferResult(status="none_detected", probe_level=parsed_level)
    if parsed_level == OfferProbeLevel.FULL:
        if browser is not None:
            value = browser()
            if hasattr(value, "__await__"):
                await value
        return ConditionalOfferResult(status="none_detected", probe_level=parsed_level)
    raise ValueError("confirm execution is owned by the orchestrator trigger gate")


async def run_with_session(session: Any, *, brand: str, level: str) -> ConditionalOfferResult:
    parsed_level = OfferProbeLevel(level)
    response = await session.request("GET", f"https://{brand}/", allow_redirects=False)
    status = response.get("status") if isinstance(response, dict) else getattr(response, "status", None)
    if status in (401, 403):
        outcome = "requires_auth" if status == 401 else "blocked"
    elif status in (429,) or (isinstance(status, int) and status >= 500):
        outcome = "error"
    else:
        outcome = "none_detected"
    return ConditionalOfferResult(status=outcome, probe_level=parsed_level)
