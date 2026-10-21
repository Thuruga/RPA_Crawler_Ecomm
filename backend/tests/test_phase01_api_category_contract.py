"""
Contrato das duas superfícies API de categoria — Plano 01-04 (Fase 01, D-04).

Cobre:
  - `GET /brands/{brand_key}/discover`  (api.routes_brands.discover_categories)
  - `GET /brands/{brand}/categories`    (api.routes_category.get_categories)

Objetivo: preservar EXATAMENTE o shape/status code que o frontend já consome
(`frontend/src/api/client.ts` + `frontend/src/App.tsx`), travar o comportamento
atual (Task 01-04-01 "cobrir resposta e status code atuais antes de qualquer
mudança") e provar que a nova metadata de diagnóstico (`category_discovery_state`
no corpo de `/categories`, header `X-Category-Discovery-State` em `/discover`)
é puramente aditiva — nenhum campo anterior é renomeado/removido, e engines
search-only (Amazon/Mercado Livre/Netshoes) nunca mais recebem o erro genérico
de "não foi possível descobrir" (bug real corrigido nesta plano, D-04).

100% hermético: nenhuma chamada de rede real; engines fake controlam
`CategoryValidationOutcome` diretamente (mesmo padrão de
`test_category_discovery_contract.py`).
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Response

from services.category_discovery import (
    CategoryProbeResult,
    CategoryValidationOutcome,
    DiscoveredCategory,
    ProbeStatus,
    build_grouped_catalog,
    flatten_catalog_paths,
)
from services.engines.base_engine import BaseEngine

import api.routes_brands as routes_brands
import api.routes_category as routes_category


# ---------------------------------------------------------------------------
# Fake engines — hermeticos, sem I/O real
# ---------------------------------------------------------------------------
class _FakeCatalogEngine(BaseEngine):
    """Espelha a estrutura real dos 5 engines catalog-capable (VTEX/Shopify/
    Wake/SFCC/Zara): sobrescreve `category_has_products` e expõe
    `_discover_and_validate()` retornando um `CategoryValidationOutcome`
    pré-construído (controlado pelo teste), sem nenhuma rede/browser real."""

    def __init__(self, outcome: CategoryValidationOutcome):
        self._outcome = outcome

    async def _discover_and_validate(self) -> CategoryValidationOutcome:
        return self._outcome

    async def discover_categories(self) -> List[Dict[str, Any]]:
        outcome = await self._discover_and_validate()
        return [{"name": c.name, "path": c.path} for c in outcome.offered]

    async def get_catalog(self) -> List[Dict[str, Any]]:
        outcome = await self._discover_and_validate()
        return build_grouped_catalog(outcome.offered)

    async def category_has_products(self, candidate: Any) -> CategoryProbeResult:
        return CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)

    async def run_bulk_scrape(self, *args, **kwargs):
        raise NotImplementedError

    async def search(self, *args, **kwargs):
        raise NotImplementedError

    async def get_product_details(self, *args, **kwargs):
        return None

    async def calculate_shipping(self, *args, **kwargs):
        return None

    def get_engine_name(self) -> str:
        return "FakeCatalogEngine"


class _FakeSearchOnlyEngine(BaseEngine):
    """Espelha Amazon/Mercado Livre/Netshoes: NUNCA sobrescreve
    `category_has_products` (herda o `NotImplementedError` de `BaseEngine`) —
    logo é `catalog_applicability=not_applicable` (D-04)."""

    async def discover_categories(self) -> List[Dict[str, Any]]:
        return []

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return []

    async def run_bulk_scrape(self, *args, **kwargs):
        raise NotImplementedError

    async def search(self, *args, **kwargs):
        raise NotImplementedError

    async def get_product_details(self, *args, **kwargs):
        return None

    async def calculate_shipping(self, *args, **kwargs):
        return None

    def get_engine_name(self) -> str:
        return "FakeSearchOnlyEngine"


def _cat(name: str, path: str, group: str = "Roupas") -> DiscoveredCategory:
    return DiscoveredCategory(name=name, path=path, url=f"https://example.com{path}", group=group)


_FAKE_BRAND = SimpleNamespace(brand_key="fakebrand", domain="www.example.com", is_active=True)


# ---------------------------------------------------------------------------
# discover_categories() — GET /brands/{brand_key}/discover
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_discover_brand_not_found_returns_404():
    """Contrato atual preservado: marca inexistente -> 404 (inalterado)."""
    with patch.object(routes_brands.brand_service, "get_brand", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await routes_brands.discover_categories("does-not-exist", Response())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_discover_catalog_capable_with_offered_preserves_current_shape():
    """Trava o comportamento ATUAL (Task 01-04-01): lista plana
    `[{"name","path"}]`, sem exceção, quando há categorias oferecidas."""
    outcome = CategoryValidationOutcome(
        offered=[_cat("Camisas", "/masculino/camisas")],
        probes={"/masculino/camisas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)},
    )
    engine = _FakeCatalogEngine(outcome)
    response = Response()

    with patch.object(routes_brands.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_brands.engine_factory, "get_engine", return_value=engine):
        result = await routes_brands.discover_categories("fakebrand", response)

    # Shape EXATAMENTE igual ao anterior — lista de dicts com name/path.
    assert result == [{"name": "Camisas", "path": "/masculino/camisas"}]
    # Metadata aditiva: header novo, nao um campo no corpo (corpo continua lista).
    assert response.headers["X-Category-Discovery-State"] == "ok"


@pytest.mark.asyncio
async def test_discover_catalog_capable_empty_no_blocked_raises_400_current_behavior():
    """Trava o comportamento ATUAL: categoria catalog-capable genuinamente
    vazia (sem blocked) ainda produz o mesmo status 400/detail de antes —
    esta plano NAO relaxa o fail-closed para o caso vazio legitimo."""
    outcome = CategoryValidationOutcome(
        offered=[],
        probes={"/masculino/bermudas": CategoryProbeResult(ProbeStatus.EMPTY)},
    )
    engine = _FakeCatalogEngine(outcome)
    response = Response()

    with patch.object(routes_brands.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_brands.engine_factory, "get_engine", return_value=engine):
        with pytest.raises(HTTPException) as exc_info:
            await routes_brands.discover_categories("fakebrand", response)

    assert exc_info.value.status_code == 400
    assert "Não foi possível descobrir" in exc_info.value.detail
    # Header aditivo distingue "empty legitimo" de "blocked" (D-04) mesmo no erro.
    assert exc_info.value.headers["X-Category-Discovery-State"] == "empty"


@pytest.mark.asyncio
async def test_discover_catalog_capable_blocked_raises_400_and_flags_blocked_state():
    """D-04: bloqueio/timeout nunca é sucesso silencioso — 400 preservado, mas
    o estado aditivo distingue 'blocked' de 'empty legítimo'."""
    outcome = CategoryValidationOutcome(
        offered=[],
        probes={"/masculino/jaquetas": CategoryProbeResult(ProbeStatus.BLOCKED, detail="timeout")},
    )
    engine = _FakeCatalogEngine(outcome)
    response = Response()

    with patch.object(routes_brands.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_brands.engine_factory, "get_engine", return_value=engine):
        with pytest.raises(HTTPException) as exc_info:
            await routes_brands.discover_categories("fakebrand", response)

    assert exc_info.value.status_code == 400
    assert exc_info.value.headers["X-Category-Discovery-State"] == "blocked"


@pytest.mark.asyncio
async def test_discover_search_only_engine_never_raises_misleading_sync_error():
    """FIX (D-04): engine search-only (Amazon/ML/Netshoes) NUNCA recebe o erro
    genérico de sincronização — retorna 200 com lista vazia + estado
    not_applicable explícito (nem 'empty', nem 'blocked')."""
    engine = _FakeSearchOnlyEngine()
    response = Response()

    with patch.object(routes_brands.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_brands.engine_factory, "get_engine", return_value=engine):
        result = await routes_brands.discover_categories("amazon", response)

    assert result == []
    assert response.headers["X-Category-Discovery-State"] == "not_applicable"


# ---------------------------------------------------------------------------
# get_categories() — GET /brands/{brand}/categories
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_categories_brand_not_found_returns_404():
    """Contrato atual preservado: marca inexistente -> 404 (inalterado)."""
    with patch.object(routes_category.brand_service, "get_brand", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await routes_category.get_categories("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_categories_preserves_brand_and_categories_keys_plus_additive_state():
    """Trava o shape ATUAL (`brand`, `categories`) e prova que a nova chave
    `category_discovery_state` é puramente aditiva (frontend só lê
    `data.categories`, então uma chave nova extra não quebra nada)."""
    outcome = CategoryValidationOutcome(
        offered=[_cat("Camisas", "/masculino/camisas")],
        probes={"/masculino/camisas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)},
    )
    engine = _FakeCatalogEngine(outcome)

    with patch.object(routes_category.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_category.engine_factory, "get_engine", return_value=engine):
        result = await routes_category.get_categories("fakebrand")

    assert result["brand"] == "fakebrand"
    assert result["categories"] == [
        {"group": "Roupas", "items": [{"label": "Camisas", "path": "/masculino/camisas"}]}
    ]
    assert result["category_discovery_state"]["catalog_applicability"] == "catalog_capable"
    assert result["category_discovery_state"]["state"] == "ok"


@pytest.mark.asyncio
async def test_categories_search_only_engine_is_not_applicable_not_error():
    """Engine search-only nunca gera exceção nesta rota (comportamento
    preservado) e agora carrega o estado not_applicable explícito (D-04)."""
    engine = _FakeSearchOnlyEngine()

    with patch.object(routes_category.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_category.engine_factory, "get_engine", return_value=engine):
        result = await routes_category.get_categories("amazon")

    assert result["categories"] == []
    assert result["category_discovery_state"]["catalog_applicability"] == "not_applicable"


# ---------------------------------------------------------------------------
# Equivalência entre as duas superfícies (mesmas identidades oficiais)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_discover_and_categories_endpoints_return_equivalent_identities():
    """As duas rotas API devem retornar exatamente as mesmas identidades
    oficiais após flatten — nenhuma diverge nem duplica lógica de categoria
    (mesmo `CategoryValidationOutcome`, mesmo helper `build_grouped_catalog`)."""
    outcome = CategoryValidationOutcome(
        offered=[
            _cat("Camisas", "/masculino/camisas", group="Vestuário"),
            _cat("Bermudas", "/masculino/bermudas", group="Vestuário"),
            _cat("Tênis", "/masculino/tenis", group="Calçados"),
        ],
        probes={
            "/masculino/camisas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS),
            "/masculino/bermudas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS),
            "/masculino/tenis": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS),
        },
    )
    engine = _FakeCatalogEngine(outcome)
    response = Response()

    with patch.object(routes_brands.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_brands.engine_factory, "get_engine", return_value=engine):
        discover_result = await routes_brands.discover_categories("fakebrand", response)

    with patch.object(routes_category.brand_service, "get_brand", return_value=_FAKE_BRAND), \
         patch.object(routes_category.engine_factory, "get_engine", return_value=engine):
        categories_result = await routes_category.get_categories("fakebrand")

    flattened = flatten_catalog_paths(categories_result["categories"])
    # discover_categories() retorna {"name","path"}; flatten_catalog_paths retorna
    # {"name","path"} também (ver services/category_discovery.py) — comparáveis
    # diretamente por conjunto de paths (ordem de grupo pode diferir do flat).
    assert {c["path"] for c in discover_result} == {c["path"] for c in flattened}
    assert {c["name"] for c in discover_result} == {c["name"] for c in flattened}
    assert len(discover_result) == len(flattened) == 3


# ---------------------------------------------------------------------------
# backend/scripts/audit_category_accuracy.py — testes hermeticos
# (agregacao/exit code, whitelist N/A, sanitizacao). Task 01-04-02.
# ---------------------------------------------------------------------------
import scripts.audit_category_accuracy as audit_script  # noqa: E402


def _fake_audit_result(**overrides) -> "audit_script.BrandAuditResult":
    defaults = dict(
        brand_key="fakebrand",
        brand_name="Fake Brand",
        engine="vtex",
        domain="www.example.com",
        catalog_applicability="catalog_capable",
        status=audit_script.AuditStatus.VERIFIED,
    )
    defaults.update(overrides)
    return audit_script.BrandAuditResult(**defaults)


class Test_AuditStatus:
    def test_only_verified_and_not_applicable_are_passing(self):
        passing = {s for s in audit_script.AuditStatus if s.is_passing}
        assert passing == {audit_script.AuditStatus.VERIFIED, audit_script.AuditStatus.NOT_APPLICABLE}

    def test_all_non_passing_states_are_the_ones_named_in_the_plan(self):
        non_passing = {s.value for s in audit_script.AuditStatus if not s.is_passing}
        assert non_passing == {"empty", "blocked", "out_of_scope", "missing", "unexpected", "unverified"}


class Test_ComputeExitCode:
    def test_exit_zero_when_all_verified_or_not_applicable(self):
        results = [
            _fake_audit_result(status=audit_script.AuditStatus.VERIFIED),
            _fake_audit_result(
                brand_key="amazon",
                engine="amazon",
                catalog_applicability="not_applicable",
                status=audit_script.AuditStatus.NOT_APPLICABLE,
            ),
        ]
        assert audit_script.compute_exit_code(results) == 0

    @pytest.mark.parametrize(
        "status",
        [
            audit_script.AuditStatus.EMPTY,
            audit_script.AuditStatus.BLOCKED,
            audit_script.AuditStatus.OUT_OF_SCOPE,
            audit_script.AuditStatus.MISSING,
            audit_script.AuditStatus.UNEXPECTED,
            audit_script.AuditStatus.UNVERIFIED,
        ],
    )
    def test_exit_nonzero_when_any_catalog_capable_brand_is_not_verified(self, status):
        """D-04: nenhum destes estados pode produzir exit 0 — bloqueio/empty/
        diff nunca e aprovado silenciosamente."""
        results = [
            _fake_audit_result(status=audit_script.AuditStatus.VERIFIED),
            _fake_audit_result(brand_key="broken", status=status),
        ]
        assert audit_script.compute_exit_code(results) == 1

    def test_summarize_reports_exit_code_and_counts_by_status(self):
        results = [
            _fake_audit_result(status=audit_script.AuditStatus.VERIFIED),
            _fake_audit_result(brand_key="blocked-brand", status=audit_script.AuditStatus.BLOCKED),
        ]
        summary = audit_script.summarize(results)
        assert summary["brands_audited"] == 2
        assert summary["by_status"] == {"verified": 1, "blocked": 1}
        assert summary["exit_code"] == 1


class Test_NotApplicableWhitelist:
    """D-04: SOMENTE Amazon/Mercado Livre/Netshoes podem ser not_applicable."""

    @pytest.mark.parametrize(
        "brand_key,engine_name,domain",
        [
            ("mercado_livre", "mercadolivre", "mercadolivre.com.br"),
            ("amazon", "amazon", "amazon.com.br"),
            ("netshoes", "netshoes", "netshoes.com.br"),
            ("dafiti", "dafiti", "www.dafiti.com.br"),
            ("privalia", "privalia", "br.privalia.com"),
        ],
    )
    @pytest.mark.asyncio
    async def test_whitelisted_marketplace_is_not_applicable(self, brand_key, engine_name, domain):
        engine = _FakeSearchOnlyEngine()
        brand = SimpleNamespace(
            brand_key=brand_key, brand_name=brand_key.title(),
            engine=engine_name, domain=domain,
        )
        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.catalog_applicability == "not_applicable"
        assert result.status == audit_script.AuditStatus.NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_non_whitelisted_brand_missing_override_is_unverified_not_approved(self):
        """Se um engine catalog-capable perder o override de
        `category_has_products` (bug/regressao), NUNCA deve virar
        not_applicable/aprovado — deve ser reportado como unverified."""
        engine = _FakeSearchOnlyEngine()  # nao sobrescreve category_has_products
        brand = SimpleNamespace(
            brand_key="aramis", brand_name="Aramis", engine="vtex", domain="www.aramis.com.br",
        )
        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.UNVERIFIED
        assert result.catalog_applicability == "not_applicable"
        assert "aramis" in (result.error or "")
        assert audit_script.compute_exit_code([result]) == 1


class Test_AuditBrandAggregation:
    @pytest.mark.asyncio
    async def test_verified_when_all_offered_and_no_blocked_empty(self):
        outcome = CategoryValidationOutcome(
            offered=[_cat("Camisas", "/masculino/camisas")],
            probes={"/masculino/camisas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)},
        )
        engine = _FakeCatalogEngine(outcome)
        brand = SimpleNamespace(brand_key="fakebrand", brand_name="Fake", engine="vtex", domain="www.example.com")

        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.VERIFIED
        assert result.offered_count == 1
        assert result.empty_removed == []
        assert result.blocked == []

    @pytest.mark.asyncio
    async def test_empty_probe_is_reported_and_fails_gate(self):
        outcome = CategoryValidationOutcome(
            offered=[],
            probes={"/masculino/bermudas": CategoryProbeResult(ProbeStatus.EMPTY, detail="0 produtos")},
        )
        engine = _FakeCatalogEngine(outcome)
        brand = SimpleNamespace(brand_key="fakebrand", brand_name="Fake", engine="vtex", domain="www.example.com")

        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.EMPTY
        assert len(result.empty_removed) == 1
        assert audit_script.compute_exit_code([result]) == 1

    @pytest.mark.asyncio
    async def test_blocked_probe_is_reported_and_never_coerced_to_success(self):
        outcome = CategoryValidationOutcome(
            offered=[],
            probes={"/masculino/jaquetas": CategoryProbeResult(ProbeStatus.BLOCKED, detail="timeout 403")},
        )
        engine = _FakeCatalogEngine(outcome)
        brand = SimpleNamespace(brand_key="fakebrand", brand_name="Fake", engine="vtex", domain="www.example.com")

        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.BLOCKED
        assert len(result.blocked) == 1
        assert audit_script.compute_exit_code([result]) == 1

    @pytest.mark.asyncio
    async def test_out_of_scope_leak_on_offered_category_fails_gate(self):
        """D-06: um item OFERECIDO com marcador feminino explicito (vazamento
        real de escopo) e detectado e reprova o gate — mesmo tendo passado
        pelo pipeline (simula um bug hipotetico de regressao na policy)."""
        leaked = _cat("Vestidos Feminino", "/feminino/vestidos")
        outcome = CategoryValidationOutcome(
            offered=[leaked],
            probes={leaked.identity: CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)},
        )
        engine = _FakeCatalogEngine(outcome)
        brand = SimpleNamespace(brand_key="fakebrand", brand_name="Fake", engine="vtex", domain="www.example.com")

        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.OUT_OF_SCOPE
        assert len(result.out_of_scope) == 1
        assert audit_script.compute_exit_code([result]) == 1

    @pytest.mark.asyncio
    async def test_single_gender_brand_neutral_leaf_is_never_flagged_out_of_scope(self):
        """Regressao negativa: uma folha neutra legitimamente aprovada (sem
        nenhum marcador de genero) NUNCA deve ser marcada out_of_scope —
        `_out_of_scope_leak` so reage a marcador feminino/infantil-ambiguo
        explicito, nunca ao ramo dependente de `male_only_brand`."""
        neutral = _cat("Camisetas", "/roupas/camisetas")  # sem nenhum token de genero
        outcome = CategoryValidationOutcome(
            offered=[neutral],
            probes={neutral.identity: CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)},
        )
        engine = _FakeCatalogEngine(outcome)
        brand = SimpleNamespace(brand_key="aramis", brand_name="Aramis", engine="vtex", domain="www.aramis.com.br")

        with patch.object(audit_script.engine_factory, "get_engine", return_value=engine):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.VERIFIED
        assert result.out_of_scope == []

    @pytest.mark.asyncio
    async def test_engine_resolution_failure_is_unverified_never_crashes_gather(self):
        brand = SimpleNamespace(brand_key="broken", brand_name="Broken", engine="vtex", domain="broken.example.com")

        def _raise(_key):
            raise RuntimeError("token=super-secret-value engine resolution failed")

        with patch.object(audit_script.engine_factory, "get_engine", side_effect=_raise):
            result = await audit_script.audit_brand(brand)

        assert result.status == audit_script.AuditStatus.UNVERIFIED
        # Sanitizacao (T-01-04-audit-SSRF): nunca vaza o valor do token.
        assert "super-secret-value" not in (result.error or "")
        assert "<redacted>" in (result.error or "")


class Test_Sanitization:
    def test_sanitize_error_redacts_known_secret_markers(self):
        exc = RuntimeError("request failed Authorization: Bearer abc123 token=xyz789")
        sanitized = audit_script._sanitize_error(exc)
        assert "abc123" not in sanitized
        assert "xyz789" not in sanitized
        assert "<redacted>" in sanitized

    def test_sanitize_error_truncates_long_messages(self):
        exc = RuntimeError("x" * 5000)
        sanitized = audit_script._sanitize_error(exc)
        assert len(sanitized) <= audit_script._MAX_ERROR_LEN

    def test_sanitize_error_text_redacts_cookie_marker(self):
        sanitized = audit_script._sanitize_error_text("Set-Cookie: session=deadbeef; blocked")
        assert "deadbeef" not in sanitized
        assert "<redacted>" in sanitized


class Test_AuditAllConcurrencyAndTimeout:
    @pytest.mark.asyncio
    async def test_audit_all_never_raises_even_if_one_brand_errors(self):
        good_outcome = CategoryValidationOutcome(
            offered=[_cat("Camisas", "/masculino/camisas")],
            probes={"/masculino/camisas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)},
        )
        good_engine = _FakeCatalogEngine(good_outcome)
        good_brand = SimpleNamespace(brand_key="good", brand_name="Good", engine="vtex", domain="www.good.com")
        bad_brand = SimpleNamespace(brand_key="bad", brand_name="Bad", engine="vtex", domain="www.bad.com")

        def _get_engine(key):
            if key == "good":
                return good_engine
            raise RuntimeError("simulated resolution failure")

        with patch.object(audit_script.engine_factory, "get_engine", side_effect=_get_engine):
            results = await audit_script.audit_all([good_brand, bad_brand], concurrency=2)

        statuses = {r.brand_key: r.status for r in results}
        assert statuses["good"] == audit_script.AuditStatus.VERIFIED
        assert statuses["bad"] == audit_script.AuditStatus.UNVERIFIED
        assert audit_script.compute_exit_code(results) == 1


class Test_SelectBrandsCliSsrfGuard:
    def test_unknown_brand_flag_is_ignored_not_passed_to_factory(self):
        """T-01-04-audit-SSRF: --brand so seleciona brand_keys ja registrados
        em brand_service — um valor desconhecido nunca chega ao engine_factory."""
        known_brand = SimpleNamespace(brand_key="aramis", brand_name="Aramis", engine="vtex", domain="www.aramis.com.br")
        args = SimpleNamespace(active_only=False, brand=["attacker-supplied-host.example"])

        with patch.object(audit_script.brand_service, "list_brands", return_value=[known_brand]):
            selected = audit_script._select_brands(args)

        assert selected == []

    def test_known_brand_flag_selects_only_that_brand(self):
        known_brand = SimpleNamespace(brand_key="aramis", brand_name="Aramis", engine="vtex", domain="www.aramis.com.br")
        other_brand = SimpleNamespace(brand_key="tommy", brand_name="Tommy", engine="vtex", domain="br.tommy.com")
        args = SimpleNamespace(active_only=False, brand=["aramis"])

        with patch.object(audit_script.brand_service, "list_brands", return_value=[known_brand, other_brand]):
            selected = audit_script._select_brands(args)

        assert [b.brand_key for b in selected] == ["aramis"]
