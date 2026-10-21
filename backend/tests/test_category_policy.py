"""Contrato Nyquist Wave 0 (Phase 01, Plano 01-01) — política de categoria fail-closed.

Cobre D-02 (aliases/regex deterministas com word boundary, sem NLP/fuzzy) e D-06
(feminino/menina/infantil-ambíguo falham fechados; apenas masculino adulto e
infantil masculino passam por evidência explícita).
"""
import ast
import inspect

from services import category_policy
from services.category_filter import is_masculine_category
from services.category_policy import (
    CategoryCandidate,
    category_matches_keywords,
    evaluate_category_scope,
)


def _decide(name="", path="", ancestors=(), male_only_brand=False):
    candidate = CategoryCandidate(name=name, path=path, ancestors=tuple(ancestors))
    return evaluate_category_scope(candidate, male_only_brand=male_only_brand)


# ---------------------------------------------------------------------------
# Masculino adulto e infantil masculino passam por evidência explícita (D-06)
# ---------------------------------------------------------------------------

def test_masculine_adult_explicit_passes():
    assert _decide("Moda Masculina", "/roupas/masculino").allowed is True


def test_homem_explicit_passes():
    assert _decide("Homem", "/homem").allowed is True


def test_menino_in_child_context_passes():
    assert _decide("Infantil Menino", "/infantil-menino").allowed is True


def test_menino_outside_child_context_still_passes_as_masculine_evidence():
    assert _decide("Camisa Menino", "/roupas/menino").allowed is True


# ---------------------------------------------------------------------------
# Feminino, menina e infantil ambíguo falham fechados (D-06)
# ---------------------------------------------------------------------------

def test_feminine_explicit_blocks():
    assert _decide("Moda Feminina", "/roupas/feminino").allowed is False


def test_menina_blocks_even_inside_child_context():
    assert _decide("Infantil Menina", "/infantil-menina").allowed is False


def test_infantil_alone_ambiguous_fails_closed():
    decision = _decide("Infantil", "/infantil")
    assert decision.allowed is False
    assert "fail-closed" in decision.reason or "fechado" in decision.reason


def test_mixed_feminine_and_masculine_text_rejected_feminine_has_precedence():
    # Texto ambíguo com AMBOS os marcadores — feminino sempre vence.
    decision = _decide("Roupas Femininas e Masculinas", "/roupas")
    assert decision.allowed is False


def test_generic_irrelevant_category_without_gender_marker_fails_closed():
    assert _decide("Beleza", "/beleza").allowed is False


def test_neutral_leaf_without_ancestor_or_male_only_brand_fails_closed():
    # Categoria neutra (sem qualquer marcador de gênero) e sem contexto do
    # chamador -> nunca assume masculino por padrão.
    assert _decide("Camisas", "/roupas/camisas").allowed is False


# ---------------------------------------------------------------------------
# Contexto do chamador: ancestral masculino confirmado ou male_only_brand=True
# ---------------------------------------------------------------------------

def test_neutral_leaf_passes_with_masculine_ancestor():
    decision = _decide("Camisas", "/roupas/masculino/camisas", ancestors=["Masculino"])
    assert decision.allowed is True


def test_neutral_leaf_passes_with_male_only_brand_flag():
    decision = _decide("Camisas", "/roupas/camisas", male_only_brand=True)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Wrapper de compatibilidade — is_masculine_category(name, url)
# ---------------------------------------------------------------------------

def test_wrapper_matches_policy_for_masculine_and_boys():
    assert is_masculine_category("Moda Masculina", "/roupas/masculino") is True
    assert is_masculine_category("Infantil Menino", "/infantil-menino") is True
    assert is_masculine_category("Homem", "/homem") is True


def test_wrapper_matches_policy_for_feminine_and_ambiguous():
    assert is_masculine_category("Moda Feminina", "/roupas/feminino") is False
    assert is_masculine_category("Infantil Menina", "/infantil-menina") is False
    assert is_masculine_category("Beleza", "/beleza") is False


# ---------------------------------------------------------------------------
# 'calcas' nunca casa com 'calcados' — word boundary real (D-02)
# ---------------------------------------------------------------------------

def test_calcas_keyword_does_not_match_calcados_category():
    calcas_candidate = CategoryCandidate(name="Calças", path="/roupas/calcas")
    calcados_candidate = CategoryCandidate(name="Calçados", path="/roupas/calcados")
    keywords = ["calca", "calcas", "jeans", "denim"]

    assert category_matches_keywords(calcas_candidate, keywords) is True
    assert category_matches_keywords(calcados_candidate, keywords) is False


def test_category_matches_keywords_uses_word_boundary_not_substring():
    # 'infantil' nao deve casar dentro de token nao relacionado.
    candidate = CategoryCandidate(name="Infante Colecionavel", path="/colecionaveis")
    assert category_matches_keywords(candidate, ["infantil"]) is False


def test_category_matches_keywords_accepts_plain_text_blob():
    # Aceita tanto CategoryCandidate quanto string simples (compat com callers
    # existentes que já normalizam o blob antes de chamar).
    assert category_matches_keywords("roupas calcas", ["calcas"]) is True
    assert category_matches_keywords("roupas calcados", ["calcas"]) is False


# ---------------------------------------------------------------------------
# Guard: category_policy.py NUNCA importa/chama NLP ou fuzzy matching (D-02)
# ---------------------------------------------------------------------------

_FORBIDDEN_MODULES = ("nlp_service", "rapidfuzz", "difflib", "fuzzywuzzy", "thefuzz")


def _imported_module_names(module) -> set[str]:
    """Extrai nomes de módulos de todos os `import`/`from ... import` via AST.

    Usa AST (não substring no texto-fonte) para não gerar falso positivo com
    docstrings/comentários que mencionam esses módulos em prosa explicativa.
    """
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_category_policy_module_has_no_nlp_or_fuzzy_dependency():
    imported = _imported_module_names(category_policy)
    for forbidden in _FORBIDDEN_MODULES:
        assert not any(forbidden in name for name in imported), (
            f"category_policy.py nao deve importar {forbidden!r} (imports: {imported})"
        )


def test_category_filter_module_has_no_nlp_or_fuzzy_dependency():
    from services import category_filter

    imported = _imported_module_names(category_filter)
    for forbidden in _FORBIDDEN_MODULES:
        assert not any(forbidden in name for name in imported), (
            f"category_filter.py nao deve importar {forbidden!r} (imports: {imported})"
        )
