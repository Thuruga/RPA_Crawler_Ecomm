"""
Pipeline compartilhado de descoberta de categorias — Wave 2 (D-02, D-04, D-05, D-06).

Sequência única para todo engine catalog-capable:

    official discovery -> scope policy -> probe (has_products) -> offered

Autoridade única (D-05): os candidatos que entram neste pipeline devem ser
SOMENTE o que o menu/API oficial do engine devolveu nesta chamada. Mapping
de/para (`brand.mappings`), cache expirado e qualquer fallback estático podem
no máximo RESOLVER/anotar um candidato já presente na resposta oficial — eles
nunca criam um candidato novo aqui. Categorias sem evidência de produto
(D-04) são removidas antes de serem oferecidas; bloqueio/timeout nunca vira
sucesso silencioso.

Módulo PURO quanto a classificação: nenhuma dependência de NLP/fuzzy. A
decisão de escopo (masculino/infantil-masculino, D-02/D-06) é delegada a
`services.category_policy` (determinístico, fail-closed, token-based).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Probe result — tipado, nunca equivalente a True/empty (D-04)
# ---------------------------------------------------------------------------
class ProbeStatus(str, Enum):
    """Estado diagnóstico de uma checagem de conteúdo de categoria.

    `BLOCKED` cobre timeout, HTTP 403/anti-bot, exceção de rede ou qualquer
    resposta não confiável — nunca é tratado como `EMPTY` nem como
    `HAS_PRODUCTS`, para não confundir bloqueio com sucesso ou com vazio.
    """

    HAS_PRODUCTS = "has_products"
    EMPTY = "empty"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class CategoryProbeResult:
    """Resultado tipado de uma probe de conteúdo (engine-specific)."""

    status: ProbeStatus
    detail: str = ""

    @property
    def has_products(self) -> bool:
        return self.status is ProbeStatus.HAS_PRODUCTS


# ---------------------------------------------------------------------------
# Candidato de descoberta — vindo SOMENTE do menu/API oficial (D-05)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiscoveredCategory:
    """Candidato de categoria oficial, antes/depois de escopo e probe.

    `ancestors` preserva os nomes dos nós-pai (breadcrumb) na ordem em que
    aparecem na árvore/menu oficial — usado tanto pela política de escopo
    (herança de contexto masculino confirmado) quanto para diagnóstico.
    `group` é o agrupamento de exibição (departamento/coleção) usado por
    `build_grouped_catalog`.
    """

    name: str
    path: str
    url: str = ""
    group: str = ""
    ancestors: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def identity(self) -> str:
        """Identidade canônica para dedup — path normalizado (fallback: url/name)."""
        base = (self.path or self.url or self.name or "").strip().lower()
        return base.rstrip("/") or base


@dataclass(frozen=True)
class CategoryValidationOutcome:
    """Resultado do pipeline: oferecidas + diagnóstico completo de cada probe.

    `probes` preserva o estado de TODO candidato em escopo (inclusive os
    bloqueados/vazios) — bloqueio é diagnóstico, não é descartado em
    silêncio, apenas nunca aparece em `offered` (D-04).
    """

    offered: List[DiscoveredCategory]
    probes: Dict[str, CategoryProbeResult]


ScopePolicy = Callable[[DiscoveredCategory], bool]
HasProductsProbe = Callable[[DiscoveredCategory], Awaitable[CategoryProbeResult]]


# ---------------------------------------------------------------------------
# Dedup determinístico — por identidade/path canônico, preserva 1a ocorrência
# ---------------------------------------------------------------------------
def _dedup_preserve_order(
    candidates: Sequence[DiscoveredCategory],
) -> List[DiscoveredCategory]:
    seen: set = set()
    result: List[DiscoveredCategory] = []
    for candidate in candidates:
        key = candidate.identity
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


# ---------------------------------------------------------------------------
# Scope policy padrão — delega para category_policy (D-02/D-06), zero NLP
# ---------------------------------------------------------------------------
def make_scope_policy(male_only_brand: bool = False) -> ScopePolicy:
    """Constrói uma `ScopePolicy` baseada em `category_policy.evaluate_category_scope`.

    `male_only_brand=True` só deve ser usado quando o chamador tem certeza de
    que TODO o catálogo da marca é masculino (contexto explícito) — categorias
    neutras de marcas mistas (ex.: lojas que vendem masculino e feminino)
    continuam fail-closed a menos que um ancestral já confirme o gênero.
    """
    from services.category_policy import CategoryCandidate as _PolicyCandidate
    from services.category_policy import evaluate_category_scope

    def _policy(candidate: DiscoveredCategory) -> bool:
        policy_candidate = _PolicyCandidate(
            name=candidate.name,
            path=candidate.path,
            ancestors=candidate.ancestors,
        )
        return evaluate_category_scope(
            policy_candidate, male_only_brand=male_only_brand
        ).allowed

    return _policy


def infer_male_only_brand(candidates: Sequence[DiscoveredCategory]) -> bool:
    """Heurística determinística (D-06) para `male_only_brand`, sem exigir
    um campo de configuração novo por marca.

    Se NENHUM candidato oficial desta chamada carrega um marcador feminino
    explícito (`category_policy.FEMININE_ALIASES`) em nome/path/ancestrais,
    o catálogo observado é de fato single-gender (ex.: Aramis, cujas
    departamentos — "Roupas", "Calçados", "Acessórios" — não usam nenhuma
    palavra de gênero) e folhas neutras podem ser aceitas
    (`male_only_brand=True`). Caso ao menos um candidato oficial mencione um
    marcador feminino (catálogo misto, ex.: Reserva/Tommy/Richards, que
    seguem uma estrutura `/masculino/...` + `/feminino/...`), folhas neutras
    exigem ancestral masculino confirmado (`male_only_brand=False`) — nunca
    assumidas masculinas por omissão, para não vazar uma categoria apenas
    "adjacente" ao feminino (D-06 fail-closed).

    100% derivado do discovery oficial desta chamada (D-05) — não lê
    configuração de marca nem estado persistido.
    """
    from services.category_policy import FEMININE_ALIASES, category_matches_keywords

    for candidate in candidates:
        blob = " ".join([candidate.name, candidate.path, *candidate.ancestors])
        if category_matches_keywords(blob, FEMININE_ALIASES):
            return False
    return True


# ---------------------------------------------------------------------------
# Pipeline principal — official -> scope -> probe -> offered
# ---------------------------------------------------------------------------
async def validate_discovered_categories(
    candidates: Sequence[DiscoveredCategory],
    *,
    scope_policy: ScopePolicy,
    has_products: HasProductsProbe,
    concurrency: int = 4,
) -> CategoryValidationOutcome:
    """Aplica escopo determinístico e probe de conteúdo aos candidatos oficiais.

    Contrato:
      - `candidates` já deve conter SOMENTE o que o discovery oficial (menu/
        API) devolveu nesta chamada (D-05) — mapping/cache/fallback nunca
        entram aqui como candidato novo, apenas podem anotar um candidato já
        presente.
      - `scope_policy` é OBRIGATÓRIO — decide fail-closed se o candidato
        pertence ao escopo masculino/infantil-masculino (D-02/D-06).
      - `has_products` é OBRIGATÓRIO, sem default permissivo — probe
        engine-specific que classifica has_products/empty/blocked (D-04).
        `BaseEngine.category_has_products` não tem default `True`; callers
        deste pipeline devem fornecer um probe real.
      - Preserva a ordem de chegada (ordem do menu/API oficial) e deduplica
        por identidade canônica (path normalizado), mantendo a 1a ocorrência.
      - Uma exceção do probe é tratada como `BLOCKED` (fail-closed) — nunca
        derruba o pipeline inteiro nem vira sucesso silencioso.

    Retorna `CategoryValidationOutcome` com `offered` (somente
    `has_products`) e `probes` (diagnóstico completo de todo candidato em
    escopo, inclusive bloqueados/vazios).
    """
    deduped = _dedup_preserve_order(candidates)
    in_scope = [c for c in deduped if scope_policy(c)]

    semaphore = asyncio.Semaphore(max(1, concurrency))
    probes: Dict[str, CategoryProbeResult] = {}

    async def _probe_one(candidate: DiscoveredCategory) -> None:
        async with semaphore:
            try:
                result = await has_products(candidate)
            except Exception as exc:  # probe nunca derruba o pipeline (fail-closed)
                logger.debug(
                    "Probe falhou para candidato '%s': %s", candidate.identity, exc
                )
                result = CategoryProbeResult(ProbeStatus.BLOCKED, detail=str(exc))
            probes[candidate.identity] = result

    if in_scope:
        await asyncio.gather(*(_probe_one(c) for c in in_scope))

    offered = [
        c
        for c in in_scope
        if probes.get(
            c.identity, CategoryProbeResult(ProbeStatus.BLOCKED, "sem resultado")
        ).has_products
    ]

    return CategoryValidationOutcome(offered=offered, probes=probes)


# ---------------------------------------------------------------------------
# Grouping / flatten — discover_categories() e get_catalog() derivam da
# MESMA lista validada e devem ser equivalentes (flatten(get_catalog) ==
# discover_categories()).
# ---------------------------------------------------------------------------
def build_grouped_catalog(
    candidates: Sequence[DiscoveredCategory],
) -> List[Dict[str, Any]]:
    """Agrupa candidatos validados (offered) -> [{group, items:[{label, path}]}].

    Preserva a ordem de 1a aparição de cada grupo e a ordem interna dos itens
    (ordem do menu/API oficial, já preservada pelo pipeline).
    """
    groups: Dict[str, List[Dict[str, str]]] = {}
    order: List[str] = []
    for candidate in candidates:
        group_name = candidate.group or "Categorias"
        if group_name not in groups:
            groups[group_name] = []
            order.append(group_name)
        groups[group_name].append({"label": candidate.name, "path": candidate.path})

    return [
        {"group": group_name, "items": groups[group_name]}
        for group_name in order
        if groups[group_name]
    ]


def flatten_catalog_paths(catalog: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Inverso de `build_grouped_catalog` — usado para provar a equivalência
    `discover_categories() == flatten_catalog_paths(get_catalog())`.
    """
    flat: List[Dict[str, str]] = []
    for group in catalog:
        for item in group.get("items", []):
            flat.append({"name": item.get("label"), "path": item.get("path")})
    return flat
