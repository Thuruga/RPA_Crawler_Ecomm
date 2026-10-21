"""
Auditoria live de precisão de categorias — Plano 01-04 (Fase 01, D-04/D-05/D-06).

Objetivo: provar, contra o site real de cada marca ativa, que o pipeline
compartilhado de descoberta de categorias (VTEX/Shopify/Wake/SFCC/Zara —
`services.category_discovery`, Wave 2/3) nunca aprova silenciosamente uma
categoria vazia, bloqueada ou fora de escopo, e que engines search-only
(Amazon/Mercado Livre/Netshoes) são explicitamente `not_applicable` — nunca
confundidos com um "empty"/"blocked" genuíno.

Estilo/convenções: espelha `audit_brand_stock_reviews.py` (dataclass de
estado, funções async top-level, `asyncio.Semaphore` + timeout por marca,
CLI via `argparse`, saída JSON sanitizada).

Segurança (threat_model do plano):
  - Marcas vêm SOMENTE de `brand_service.list_brands()`/`get_brand()` — nunca
    de um host/URL fornecido pelo caller (`--brand` é validado contra os
    brand_keys já registrados antes de qualquer fetch: sem isso, um valor
    arbitrário poderia cair no fallback `VTEXEngine` da factory).
  - Reaproveita os clients/engines já existentes (mesma sessão/proxy/config
    que o resto do backend usa) — nenhum fetch novo é implementado aqui.
  - Resultado inclui somente brand_key/engine/domain/contagens/estado/diff;
    exceções são sanitizadas antes de aparecer no relatório (nunca token,
    cookie, header de autenticação ou URL privada).
  - `--concurrency` limitado e timeout por marca; nunca invocado pelo pytest
    default (é um script standalone, fora de `testpaths`).

NÃO edita nenhum adapter/probe de engine — reusa exclusivamente os métodos já
existentes de cada engine catalog-capable (`_discover_and_validate()`, mesma
convenção introduzida em 01-03/01-06) e os helpers puros já existentes de
`services.category_discovery` / `services.category_policy` (nenhum dos dois
está no `files_modified` deste plano — apenas importados, nunca alterados).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.session_manager import SessionManager  # noqa: E402
from services.brand_service import brand_service  # noqa: E402
from services.engines.base_engine import BaseEngine  # noqa: E402
from services.engines.brand_key_utils import normalize_brand_key  # noqa: E402
from services.engines.factory import engine_factory  # noqa: E402

logger = logging.getLogger("audit_category_accuracy")

# D-08d (Plano 01-12): era 3. Rodar multiplas marcas VTEX-IO-pesadas (que
# dependem do fallback Playwright para a maioria das categorias, ex. hugoboss)
# em paralelo causa contencao real de recursos (varias instancias Chromium
# concorrentes) e produz falso BLOCKED/timeout que nao existe quando a marca
# roda sozinha — confirmado ao vivo: hugoboss isolado = 122/153 ofertadas, 0
# bloqueadas; hugoboss junto com foxton+hering = 68/153 ofertadas, 83
# bloqueadas. Rebaixado para 1 (sequencial por marca) — esta e uma auditoria
# de precisao, nao uma rotina sensivel a latencia; confiabilidade > velocidade.
DEFAULT_CONCURRENCY = 1
# VTEX-IO tiles fallback (Playwright) so navega uma pagina real por candidato
# quando o count rapido via search API legado retorna 0 — para marcas com
# muitas categorias isso pode levar minutos reais; 90s era tempo demais curto
# e produzia "unverified" por timeout do PROPRIO script, nao por um bloqueio
# real do site (falso negativo). Ajustado apos observar o live gate real.
#
# 01-09: 300s ainda era curto demais para marcas 100% VTEX-IO (sem Search API
# legada) com arvores de categoria grandes — toda folha precisa do fallback
# Playwright (~25-50s cada, medido ao vivo). 2400s (40min) foi testado ao vivo
# (01-09-DIAG.json, --concurrency 1): resolveu austral (antes falso-unverified
# por timeout, agora corretamente "empty" com 44 categorias reais). hugoboss
# (153 candidatos) e hering (640 candidatos) AINDA excedem 2400s mesmo com essa
# folga de 8x sobre o valor original — residuo de ESCALA real (arvore oficial
# grande + custo por-candidato do fallback Playwright), nao um bug de
# classificacao. Nao escalado ainda mais: um gate de auditoria "de rotina" nao
# deveria depender de um timeout de 80+ minutos para uma unica marca — ver
# 01-09-SUMMARY.md para a decisao do operador sobre este residuo.
PER_BRAND_TIMEOUT_SECONDS = 2400

# D-04: SOMENTE estes engines de marketplace search-only podem ser N/A. Um
# engine catalog-capable que perca seu override de `category_has_products`
# (bug) e caia neste "not_applicable" por engano NUNCA deve ser confundido com
# uma aprovação — ver `_classify_not_applicable` abaixo.
ALLOWED_NOT_APPLICABLE_ENGINE_CANONICAL_KEYS: frozenset[str] = frozenset(
    {"amazon", "netshoes", "mercadolivre", "dafiti", "privalia"}
)


# ---------------------------------------------------------------------------
# Estado diagnóstico por marca — nunca aprova silenciosamente (D-04)
# ---------------------------------------------------------------------------
class AuditStatus(str, Enum):
    VERIFIED = "verified"
    NOT_APPLICABLE = "not_applicable"
    EMPTY = "empty"
    BLOCKED = "blocked"
    OUT_OF_SCOPE = "out_of_scope"
    MISSING = "missing"
    UNEXPECTED = "unexpected"
    UNVERIFIED = "unverified"

    @property
    def is_passing(self) -> bool:
        """Estados que NÃO impedem o sign-off (exit 0)."""
        return self in (AuditStatus.VERIFIED, AuditStatus.NOT_APPLICABLE)


# Prioridade de reporte quando múltiplos problemas coexistem na mesma marca —
# do mais grave (nunca aprovado) ao menos grave. `blocked/unverified nunca é
# aprovado` (acceptance criteria do plano) fica no topo.
_STATUS_PRIORITY: List[AuditStatus] = [
    AuditStatus.UNVERIFIED,
    AuditStatus.BLOCKED,
    AuditStatus.MISSING,
    AuditStatus.UNEXPECTED,
    AuditStatus.OUT_OF_SCOPE,
    AuditStatus.EMPTY,
    AuditStatus.VERIFIED,
]


@dataclass
class BrandAuditResult:
    brand_key: str
    brand_name: Optional[str]
    engine: str
    domain: Optional[str]
    catalog_applicability: str  # "catalog_capable" | "not_applicable"
    status: AuditStatus
    official_count: int = 0
    in_scope_count: int = 0
    offered_count: int = 0
    empty_removed: List[Dict[str, str]] = field(default_factory=list)
    missing: List[Dict[str, str]] = field(default_factory=list)
    unexpected: List[Dict[str, str]] = field(default_factory=list)
    out_of_scope: List[Dict[str, str]] = field(default_factory=list)
    blocked: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "brand_key": self.brand_key,
            "brand_name": self.brand_name,
            "engine": self.engine,
            "domain": self.domain,
            "catalog_applicability": self.catalog_applicability,
            "status": self.status.value,
            "official_count": self.official_count,
            "in_scope_count": self.in_scope_count,
            "offered_count": self.offered_count,
            "empty_removed": self.empty_removed,
            "missing": self.missing,
            "unexpected": self.unexpected,
            "out_of_scope": self.out_of_scope,
            "blocked": self.blocked,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Sanitização — nunca token/cookie/header/URL privada no relatório
# ---------------------------------------------------------------------------
_SECRET_PATTERN = re.compile(
    r"(?i)\b(authorization|api[-_]?key|apikey|token|cookie|set-cookie|access[-_]?token)\b"
    r"\s*[:=]\s*(?:Bearer\s+)?\S+"
)
_MAX_ERROR_LEN = 400


def _sanitize_error(exc: BaseException) -> str:
    """Sanitiza a mensagem de exceção antes de entrar no relatório (T-01-04-audit-SSRF).

    Remove qualquer trecho `chave=valor`/`chave: valor` cujo nome de chave seja
    um marcador conhecido de credencial (authorization/api-key/token/cookie),
    e trunca o tamanho para evitar vazar payloads grandes de resposta HTTP.
    """
    message = f"{type(exc).__name__}: {exc}"
    message = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", message)
    return message[:_MAX_ERROR_LEN]


# ---------------------------------------------------------------------------
# Field helpers (brand pode vir como dict ou objeto Pydantic)
# ---------------------------------------------------------------------------
def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Classificação catalog_applicability — sem hardcode de brand_key (D-04)
# ---------------------------------------------------------------------------
def is_catalog_capable(engine: Any) -> bool:
    """True quando o engine sobrescreve `category_has_products` (participa do
    pipeline compartilhado). Nenhum brand_key é hardcoded — a classificação
    vem exclusivamente da estrutura do engine resolvido pela `EngineFactory`
    (mesmo critério usado em `api/routes_brands.py`/`api/routes_category.py`)."""
    return type(engine).category_has_products is not BaseEngine.category_has_products


def _out_of_scope_leak(candidate: Any) -> bool:
    """Re-verifica um candidato JÁ OFERECIDO contra os dois ramos de
    `category_policy.evaluate_category_scope` que são INDEPENDENTES de
    `male_only_brand` (marcador feminino explícito; infantil ambíguo sem
    evidência masculina) — nunca os ramos dependentes de `male_only_brand`,
    o que evitaria falso-positivo em marcas single-gender legítimas (ex.
    Aramis) cujas folhas neutras são corretamente aprovadas via
    `infer_male_only_brand()`. Detecta vazamento real de D-06 sem
    reimplementar a política (apenas reusa `category_matches_keywords`)."""
    from services.category_policy import (
        CHILD_CONTEXT_ALIASES,
        FEMININE_ALIASES,
        MASCULINE_ADULT_ALIASES,
        MASCULINE_CHILD_ALIASES,
        CategoryCandidate as _PolicyCandidate,
        category_matches_keywords,
    )

    policy_candidate = _PolicyCandidate(
        name=getattr(candidate, "name", "") or "",
        path=getattr(candidate, "path", "") or "",
        ancestors=tuple(getattr(candidate, "ancestors", ()) or ()),
    )
    if category_matches_keywords(policy_candidate, FEMININE_ALIASES):
        return True
    if category_matches_keywords(policy_candidate, CHILD_CONTEXT_ALIASES):
        has_masculine_evidence = category_matches_keywords(
            policy_candidate, MASCULINE_ADULT_ALIASES | MASCULINE_CHILD_ALIASES
        )
        if not has_masculine_evidence:
            return True
    return False


def _pick_status(
    *,
    unverified: bool,
    blocked_count: int,
    missing_count: int,
    unexpected_count: int,
    out_of_scope_count: int,
    empty_count: int,
) -> AuditStatus:
    if unverified:
        return AuditStatus.UNVERIFIED
    if blocked_count:
        return AuditStatus.BLOCKED
    if missing_count:
        return AuditStatus.MISSING
    if unexpected_count:
        return AuditStatus.UNEXPECTED
    if out_of_scope_count:
        return AuditStatus.OUT_OF_SCOPE
    if empty_count:
        return AuditStatus.EMPTY
    return AuditStatus.VERIFIED


# ---------------------------------------------------------------------------
# audit_brand — uma marca
# ---------------------------------------------------------------------------
async def audit_brand(brand: Any) -> BrandAuditResult:
    brand_key = str(_field(brand, "brand_key") or "").lower()
    engine_name = str(_field(brand, "engine") or "")
    domain = _field(brand, "domain")
    brand_name = _field(brand, "brand_name")

    try:
        engine = engine_factory.get_engine(brand_key)
    except Exception as exc:  # nunca deixa uma falha de resolução derrubar o audit inteiro
        return BrandAuditResult(
            brand_key=brand_key,
            brand_name=brand_name,
            engine=engine_name,
            domain=domain,
            catalog_applicability="catalog_capable",
            status=AuditStatus.UNVERIFIED,
            error=_sanitize_error(exc),
        )

    if not is_catalog_capable(engine):
        canonical = normalize_brand_key(brand_key)
        if canonical in ALLOWED_NOT_APPLICABLE_ENGINE_CANONICAL_KEYS:
            return BrandAuditResult(
                brand_key=brand_key,
                brand_name=brand_name,
                engine=engine_name,
                domain=domain,
                catalog_applicability="not_applicable",
                status=AuditStatus.NOT_APPLICABLE,
            )
        # D-04: engine catalog-capable perdeu o override silenciosamente — NUNCA
        # tratar como aprovado; reportar como unverified (bug de regressão).
        return BrandAuditResult(
            brand_key=brand_key,
            brand_name=brand_name,
            engine=engine_name,
            domain=domain,
            catalog_applicability="not_applicable",
            status=AuditStatus.UNVERIFIED,
            error=(
                f"engine '{engine_name}' nao sobrescreve category_has_products e "
                f"'{brand_key}' nao esta na whitelist not_applicable (D-04)."
            ),
        )

    discover_and_validate = getattr(engine, "_discover_and_validate", None)
    if discover_and_validate is None:
        return BrandAuditResult(
            brand_key=brand_key,
            brand_name=brand_name,
            engine=engine_name,
            domain=domain,
            catalog_applicability="catalog_capable",
            status=AuditStatus.UNVERIFIED,
            error="engine catalog-capable sem _discover_and_validate exposto.",
        )

    try:
        outcome = await asyncio.wait_for(
            discover_and_validate(), timeout=PER_BRAND_TIMEOUT_SECONDS
        )
    except Exception as exc:
        return BrandAuditResult(
            brand_key=brand_key,
            brand_name=brand_name,
            engine=engine_name,
            domain=domain,
            catalog_applicability="catalog_capable",
            status=AuditStatus.UNVERIFIED,
            error=_sanitize_error(exc),
        )

    from services.category_discovery import ProbeStatus

    probes = outcome.probes
    offered = outcome.offered

    offered_identities: Set[str] = {c.identity for c in offered}
    has_products_identities: Set[str] = {
        identity for identity, probe in probes.items() if probe.status == ProbeStatus.HAS_PRODUCTS
    }
    empty_removed = [
        {"path": identity, "detail": probe.detail}
        for identity, probe in probes.items()
        if probe.status == ProbeStatus.EMPTY
    ]
    blocked = [
        {"path": identity, "detail": _sanitize_error_text(probe.detail)}
        for identity, probe in probes.items()
        if probe.status == ProbeStatus.BLOCKED
    ]
    missing_identities = has_products_identities - offered_identities
    unexpected_identities = offered_identities - has_products_identities
    missing = [{"path": identity} for identity in sorted(missing_identities)]
    unexpected = [{"path": identity} for identity in sorted(unexpected_identities)]

    out_of_scope = [
        {"name": c.name, "path": c.path}
        for c in offered
        if _out_of_scope_leak(c)
    ]

    status = _pick_status(
        unverified=False,
        blocked_count=len(blocked),
        missing_count=len(missing),
        unexpected_count=len(unexpected),
        out_of_scope_count=len(out_of_scope),
        empty_count=len(empty_removed),
    )

    return BrandAuditResult(
        brand_key=brand_key,
        brand_name=brand_name,
        engine=engine_name,
        domain=domain,
        catalog_applicability="catalog_capable",
        status=status,
        official_count=len(probes),
        in_scope_count=len(probes),
        offered_count=len(offered),
        empty_removed=empty_removed,
        missing=missing,
        unexpected=unexpected,
        out_of_scope=out_of_scope,
        blocked=blocked,
    )


def _sanitize_error_text(detail: str) -> str:
    """Mesma sanitização de `_sanitize_error`, mas para strings de detalhe já
    formatadas (ex.: `CategoryProbeResult.detail`), evitando reconstruir a
    exceção original."""
    message = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", detail or "")
    return message[:_MAX_ERROR_LEN]


# ---------------------------------------------------------------------------
# audit_all — todas as marcas (com concorrência limitada + timeout por marca)
# ---------------------------------------------------------------------------
async def audit_all(
    brands: List[Any], *, concurrency: int = DEFAULT_CONCURRENCY
) -> List[BrandAuditResult]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(brand: Any) -> BrandAuditResult:
        async with semaphore:
            try:
                return await audit_brand(brand)
            except Exception as exc:  # nunca deixa uma marca derrubar o gather inteiro
                return BrandAuditResult(
                    brand_key=str(_field(brand, "brand_key") or "").lower(),
                    brand_name=_field(brand, "brand_name"),
                    engine=str(_field(brand, "engine") or ""),
                    domain=_field(brand, "domain"),
                    catalog_applicability="catalog_capable",
                    status=AuditStatus.UNVERIFIED,
                    error=_sanitize_error(exc),
                )

    return list(await asyncio.gather(*(_guarded(brand) for brand in brands)))


# ---------------------------------------------------------------------------
# Agregação + exit code — nenhum catalog-capable pode ficar fora de "verified"
# ---------------------------------------------------------------------------
def compute_exit_code(results: List[BrandAuditResult]) -> int:
    """Exit 0 somente quando TODO resultado é `is_passing` (verified ou
    not_applicable). Qualquer missing/unexpected/empty/out_of_scope/blocked/
    unverified em um engine catalog-capable (ou uma classificação not_applicable
    inválida) produz exit != 0 — nunca aprova silenciosamente (D-04)."""
    return 0 if all(result.status.is_passing for result in results) else 1


def summarize(results: List[BrandAuditResult]) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    for result in results:
        by_status[result.status.value] = by_status.get(result.status.value, 0) + 1
    return {
        "brands_audited": len(results),
        "by_status": by_status,
        "exit_code": compute_exit_code(results),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Auditoria live de precisao de categorias (D-04/D-05/D-06) — "
            "compara o pipeline compartilhado de descoberta contra o site real "
            "de cada marca ativa e falha (exit != 0) em qualquer missing/"
            "unexpected/empty/out_of_scope/blocked/unverified."
        )
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        help="Audita somente marcas com is_active=True (padrao recomendado para o gate final).",
    )
    parser.add_argument(
        "--brand",
        action="append",
        default=None,
        help="Restringe a auditoria a um brand_key especifico (repetivel). "
        "Deve ser um brand_key ja cadastrado em brands.json.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Caminho do arquivo JSON de saida (sanitizado).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Concorrencia maxima de marcas auditadas em paralelo (padrao {DEFAULT_CONCURRENCY}).",
    )
    return parser.parse_args(argv)


def _select_brands(args: argparse.Namespace) -> List[Any]:
    all_brands = brand_service.list_brands(active_only=bool(args.active_only))

    if not args.brand:
        return all_brands

    # T-01-04-audit-SSRF: --brand so pode selecionar brand_keys JA registrados
    # em brand_service — nunca um valor arbitrario do caller (evita que um
    # brand_key desconhecido caia no fallback VTEXEngine com dominio vazio).
    requested = {str(b).lower() for b in args.brand}
    by_key = {str(_field(b, "brand_key") or "").lower(): b for b in all_brands}
    selected = [by_key[key] for key in requested if key in by_key]
    unknown = sorted(requested - set(by_key.keys()))
    if unknown:
        logger.warning(
            "Ignorando --brand desconhecido(s) (nao registrado em brand_service): %s",
            ", ".join(unknown),
        )
    return selected


async def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)

    brands = _select_brands(args)
    results = await audit_all(brands, concurrency=args.concurrency)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary = summarize(results)
    payload = {
        "generated_at": generated_at,
        "active_only": bool(args.active_only),
        "summary": summary,
        "brands": [result.to_dict() for result in results],
    }

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"REPORT_FILE={args.json_out.resolve()}")

    for result in results:
        print(
            "\t".join(
                [
                    result.brand_key,
                    result.engine,
                    result.catalog_applicability,
                    result.status.value,
                    f"offered={result.offered_count}",
                    f"empty={len(result.empty_removed)}",
                    f"blocked={len(result.blocked)}",
                    f"out_of_scope={len(result.out_of_scope)}",
                ]
            )
        )

    print(json.dumps(summary, ensure_ascii=False))

    await SessionManager.close_session()
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
