"""
Política determinística e fail-closed de escopo de categoria — Wave 0 (D-02, D-06).

Módulo PURO: nenhuma dependência de rede, de classificação por linguagem natural ou
de qualquer biblioteca de similaridade/casamento aproximado de texto — apenas stdlib.
Toda decisão nasce de normalização determinística (lowercase + remoção de acento via
`unicodedata`, stdlib) seguida de matching por TOKEN (word boundary real) — nunca
substring puro, o que evita falsos positivos como a keyword 'calca' casando dentro
de 'calcados'.

Os usos existentes de classificação de linguagem natural em relevância, marca e
limpeza de query (módulo de NLP do serviço de busca, gates de relevância) NÃO são
tocados por este módulo.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence, Union


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CategoryCandidate:
    """Candidato de categoria a ser avaliado pela política (nome + path + ancestrais)."""

    name: str = ""
    path: str = ""
    ancestors: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class CategoryDecision:
    """Decisão fail-closed: sem evidência explícita suficiente, `allowed=False`."""

    allowed: bool
    reason: str


# ---------------------------------------------------------------------------
# Normalização determinística (sem NLP/fuzzy)
# ---------------------------------------------------------------------------
def normalize_category_token(text: str) -> str:
    """Lowercase + remoção de acentos (stdlib `unicodedata`). Determinístico, sem NLP."""
    if not text:
        return ""
    lowered = text.lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", lowered)
        if unicodedata.category(c) != "Mn"
    )


def _tokenize(text: str) -> set[str]:
    normalized = normalize_category_token(text)
    return {t for t in re.split(r"[^a-z0-9]+", normalized) if t}


def _candidate_tokens(candidate: Union["CategoryCandidate", str]) -> set[str]:
    if isinstance(candidate, CategoryCandidate):
        blob = " ".join([candidate.name or "", candidate.path or "", *candidate.ancestors])
    else:
        blob = candidate or ""
    return _tokenize(blob)


# ---------------------------------------------------------------------------
# Alias sets deterministas — formas de palavra COMPLETAS, nunca radicais/stems.
# Radicais truncados (ex.: 'calca' pretendendo casar 'calça'/'calças' via substring)
# são a causa raiz do bypass 'calcas' vs 'calcados'; por isso cada forma flexionada
# é listada explicitamente e o matching é por token exato.
# ---------------------------------------------------------------------------
FEMININE_ALIASES = frozenset({
    "feminino", "feminina", "femininos", "femininas",
    "mulher", "mulheres", "menina", "meninas",
})
MASCULINE_ADULT_ALIASES = frozenset({
    "masculino", "masculina", "masculinos", "masculinas", "homem", "homens",
})
MASCULINE_CHILD_ALIASES = frozenset({"menino", "meninos"})
CHILD_CONTEXT_ALIASES = frozenset({"infantil", "infantis", "kids", "mini", "teen"})


def category_matches_keywords(
    candidate: Union["CategoryCandidate", str],
    keywords: Iterable[str],
) -> bool:
    """Matching determinístico por TOKEN (word boundary real) contra uma lista de keywords.

    NUNCA usa substring puro — corrige bugs como a keyword 'calca' casando dentro de
    'calcados' (compartilham prefixo, mas são tokens/palavras distintas). Aceita tanto
    um `CategoryCandidate` quanto um texto simples já concatenado (ex.: blob normalizado
    de um caller existente).
    """
    tokens = _candidate_tokens(candidate)
    normalized_keywords = {normalize_category_token(k) for k in keywords if k}
    return bool(tokens & normalized_keywords)


def evaluate_category_scope(
    candidate: CategoryCandidate,
    male_only_brand: bool = False,
) -> CategoryDecision:
    """Decide, fail-closed, se uma categoria pertence ao escopo masculino/infantil-masculino.

    Ordem de precedência (D-02, D-06):
      1. Marcador feminino/menina explícito -> bloqueia sempre (mesmo com termo masculino
         presente no mesmo texto — feminino tem precedência absoluta).
      2. Contexto infantil + evidência masculina explícita (menino/masculino) -> permite
         (infantil masculino).
      3. Contexto infantil AMBÍGUO (sem masculino/feminino explícito) -> falha fechado.
      4. Evidência masculina adulta ou de menino fora de contexto infantil -> permite.
      5. Neutro (sem qualquer marcador de gênero): permite SOMENTE se algum ancestral já
         for masculino confirmado OU `male_only_brand=True` (contexto explícito do
         chamador). Caso contrário, falha fechado — neutro nunca é assumido masculino.
    """
    tokens = _candidate_tokens(candidate)

    if tokens & FEMININE_ALIASES:
        return CategoryDecision(False, "marcador feminino/menina explícito")

    is_child_context = bool(tokens & CHILD_CONTEXT_ALIASES)
    has_masculine_child = bool(tokens & MASCULINE_CHILD_ALIASES)
    has_masculine_adult = bool(tokens & MASCULINE_ADULT_ALIASES)

    if is_child_context:
        if has_masculine_child or has_masculine_adult:
            return CategoryDecision(
                True, "infantil com evidência masculina explícita (menino/masculino)"
            )
        return CategoryDecision(
            False, "infantil ambíguo sem evidência explícita de menino/masculino — fail-closed"
        )

    if has_masculine_adult or has_masculine_child:
        return CategoryDecision(True, "evidência masculina explícita (adulto ou menino)")

    ancestor_tokens = _tokenize(" ".join(candidate.ancestors))
    ancestor_masculine = bool(
        ancestor_tokens & (MASCULINE_ADULT_ALIASES | MASCULINE_CHILD_ALIASES)
    )

    if ancestor_masculine or male_only_brand:
        return CategoryDecision(
            True, "folha neutra sob ancestral masculino confirmado ou marca male_only_brand"
        )

    return CategoryDecision(False, "sem evidência explícita de gênero — fail-closed")


def is_masculine_category(name: str, url: str) -> bool:
    """Wrapper compatível com o call site original (`category_filter.is_masculine_category`).

    Delega para `evaluate_category_scope`; NÃO herda o substring matching antigo.
    """
    candidate = CategoryCandidate(name=name or "", path=url or "")
    return evaluate_category_scope(candidate).allowed
