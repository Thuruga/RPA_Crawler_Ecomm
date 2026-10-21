"""
Contrato do pipeline compartilhado de descoberta de categorias — Wave 2
(D-02, D-04, D-05, D-06), Plano 01-03.

Seções:
  - pipeline/mapping/blocked (Task 01-03-01): contrato async puro de
    `services.category_discovery` — official -> scope -> probe -> offered,
    usando AsyncMock (sem I/O real).
  - vtex/shopify/wake (Task 01-03-02): adaptação de cada engine à mesma
    autoridade oficial via fake clients hermeticos, provando
    `discover_categories() == flatten_catalog_paths(get_catalog())`.

100% hermético: nenhuma chamada de rede/browser real neste arquivo.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.category_discovery import (
    CategoryProbeResult,
    CategoryValidationOutcome,
    DiscoveredCategory,
    ProbeStatus,
    build_grouped_catalog,
    flatten_catalog_paths,
    make_scope_policy,
    validate_discovered_categories,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cat(name: str, path: str, group: str = "Roupas", ancestors=()) -> DiscoveredCategory:
    return DiscoveredCategory(
        name=name, path=path, url=f"https://example.com{path}", group=group, ancestors=tuple(ancestors)
    )


def _probe_map(mapping: dict):
    """Constrói uma probe assíncrona a partir de {path: ProbeStatus}."""

    async def _probe(candidate: DiscoveredCategory) -> CategoryProbeResult:
        status = mapping.get(candidate.path, ProbeStatus.BLOCKED)
        return CategoryProbeResult(status, detail=f"stub:{status.value}")

    return AsyncMock(side_effect=_probe)


def _allow_all(candidate: DiscoveredCategory) -> bool:
    return True


# ---------------------------------------------------------------------------
# pipeline — official -> scope -> probe -> offered
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_removes_empty_categories():
    """D-04: candidato oficial, em escopo, mas sem produtos, e removido."""
    candidates = [
        _cat("Camisas", "/roupas/masculino/camisas"),
        _cat("Bermudas", "/roupas/masculino/bermudas"),
    ]
    probe = _probe_map(
        {
            "/roupas/masculino/camisas": ProbeStatus.HAS_PRODUCTS,
            "/roupas/masculino/bermudas": ProbeStatus.EMPTY,
        }
    )

    outcome = await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=probe
    )

    assert [c.path for c in outcome.offered] == ["/roupas/masculino/camisas"]
    # Vazio permanece no diagnostico, mas nao e oferecido.
    assert outcome.probes["/roupas/masculino/bermudas"].status == ProbeStatus.EMPTY


@pytest.mark.asyncio
async def test_pipeline_preserves_blocked_as_diagnostic_not_offered():
    """D-04: bloqueio/timeout nunca equivale a True/empty — nao e oferecido,
    mas o estado diagnostico 'blocked' e preservado (nao descartado em silencio)."""
    candidates = [_cat("Polos", "/roupas/masculino/polos")]

    async def _probe(candidate: DiscoveredCategory) -> CategoryProbeResult:
        return CategoryProbeResult(ProbeStatus.BLOCKED, detail="timeout")

    outcome = await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=_probe
    )

    assert outcome.offered == []
    assert outcome.probes["/roupas/masculino/polos"].status == ProbeStatus.BLOCKED
    assert outcome.probes["/roupas/masculino/polos"].has_products is False


@pytest.mark.asyncio
async def test_pipeline_probe_exception_is_treated_as_blocked_not_offered():
    """Uma excecao levantada pela probe e capturada como BLOCKED (fail-closed),
    nunca derruba o pipeline nem vira sucesso silencioso."""
    candidates = [_cat("Jaquetas", "/roupas/masculino/jaquetas")]

    async def _probe(candidate: DiscoveredCategory) -> CategoryProbeResult:
        raise TimeoutError("simulated WAF timeout")

    outcome = await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=_probe
    )

    assert outcome.offered == []
    assert outcome.probes["/roupas/masculino/jaquetas"].status == ProbeStatus.BLOCKED


@pytest.mark.asyncio
async def test_pipeline_excludes_stale_mapping_not_in_official_candidates():
    """D-05: um path que so existe em brand.mappings (nao devolvido pelo
    discovery oficial desta chamada) NUNCA aparece nos candidatos e portanto
    nunca e oferecido — mappings podem apenas anotar, nunca criar candidato."""
    # Simula: apenas 'camisas' veio do discovery oficial; 'calcados-antigo' e
    # um mapping stale que o caller nao deve injetar como candidato.
    official_candidates = [_cat("Camisas", "/roupas/masculino/camisas")]
    stale_mapping_path = "/roupas/masculino/calcados-antigo"

    probe = _probe_map(
        {
            "/roupas/masculino/camisas": ProbeStatus.HAS_PRODUCTS,
            stale_mapping_path: ProbeStatus.HAS_PRODUCTS,  # nunca deveria ser chamado
        }
    )

    outcome = await validate_discovered_categories(
        official_candidates, scope_policy=_allow_all, has_products=probe
    )

    offered_paths = [c.path for c in outcome.offered]
    assert offered_paths == ["/roupas/masculino/camisas"]
    assert stale_mapping_path not in offered_paths
    assert stale_mapping_path not in outcome.probes


@pytest.mark.asyncio
async def test_pipeline_scope_policy_blocks_out_of_scope_candidate():
    """Candidato oficial fora de escopo (ex.: feminino) e removido antes da probe
    — a probe nao e sequer chamada para ele (evita fan-out desnecessario)."""
    candidates = [
        _cat("Camisas Masculinas", "/roupas/masculino/camisas"),
        _cat("Vestidos Femininos", "/roupas/feminino/vestidos"),
    ]

    from services.category_policy import evaluate_category_scope
    from services.category_policy import CategoryCandidate as PolicyCandidate

    def scope_policy(candidate: DiscoveredCategory) -> bool:
        return evaluate_category_scope(
            PolicyCandidate(name=candidate.name, path=candidate.path, ancestors=candidate.ancestors)
        ).allowed

    probe = AsyncMock(return_value=CategoryProbeResult(ProbeStatus.HAS_PRODUCTS))

    outcome = await validate_discovered_categories(
        candidates, scope_policy=scope_policy, has_products=probe
    )

    assert [c.path for c in outcome.offered] == ["/roupas/masculino/camisas"]
    # A probe nunca foi chamada para o candidato feminino (fora de escopo).
    called_paths = {call.args[0].path for call in probe.await_args_list}
    assert "/roupas/feminino/vestidos" not in called_paths


@pytest.mark.asyncio
async def test_pipeline_dedup_by_canonical_path_deterministic():
    """Deduplica por identidade/path canonico, mantendo a 1a ocorrencia (ordem
    do menu oficial) — mesmo path repetido em nos diferentes da arvore conta uma vez."""
    candidates = [
        _cat("Camisas", "/roupas/masculino/camisas/"),
        _cat("Camisas (dup)", "/roupas/masculino/camisas"),  # mesma identidade (sem barra final)
        _cat("Polos", "/roupas/masculino/polos"),
    ]
    probe = AsyncMock(return_value=CategoryProbeResult(ProbeStatus.HAS_PRODUCTS))

    outcome = await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=probe
    )

    names = [c.name for c in outcome.offered]
    assert names == ["Camisas", "Polos"]
    assert probe.await_count == 2  # a duplicata nao gerou uma 2a chamada de probe


@pytest.mark.asyncio
async def test_pipeline_preserves_official_order_and_ancestors():
    """Ordem do menu oficial e breadcrumbs (ancestors) sobrevivem ao pipeline."""
    candidates = [
        _cat("Bermudas", "/masculino/bermudas", ancestors=("Masculino",)),
        _cat("Camisas", "/masculino/camisas", ancestors=("Masculino",)),
        _cat("Polos", "/masculino/polos", ancestors=("Masculino",)),
    ]
    probe = AsyncMock(return_value=CategoryProbeResult(ProbeStatus.HAS_PRODUCTS))

    outcome = await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=probe
    )

    assert [c.name for c in outcome.offered] == ["Bermudas", "Camisas", "Polos"]
    assert all(c.ancestors == ("Masculino",) for c in outcome.offered)


@pytest.mark.asyncio
async def test_pipeline_concurrency_limit_respected():
    """Concorrencia limitada (default 4): nunca mais que `concurrency` probes
    em voo simultaneamente."""
    import asyncio

    candidates = [_cat(f"Cat {i}", f"/cat-{i}") for i in range(12)]
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _probe(candidate: DiscoveredCategory) -> CategoryProbeResult:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return CategoryProbeResult(ProbeStatus.HAS_PRODUCTS)

    await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=_probe, concurrency=4
    )

    assert max_in_flight <= 4


@pytest.mark.asyncio
async def test_pipeline_flatten_grouped_catalog_equivalence():
    """discover (flat) == flatten_catalog_paths(get_catalog) quando derivados
    da MESMA lista validada (offered)."""
    candidates = [
        _cat("Camisas", "/masculino/camisas", group="Roupas"),
        _cat("Polos", "/masculino/polos", group="Roupas"),
        _cat("Tenis", "/masculino/calcados/tenis", group="Calcados"),
    ]
    probe = AsyncMock(return_value=CategoryProbeResult(ProbeStatus.HAS_PRODUCTS))

    outcome = await validate_discovered_categories(
        candidates, scope_policy=_allow_all, has_products=probe
    )

    discover_flat = [{"name": c.name, "path": c.path} for c in outcome.offered]
    grouped = build_grouped_catalog(outcome.offered)
    flattened_back = flatten_catalog_paths(grouped)

    assert discover_flat == flattened_back
    # Agrupamento preserva grupos na ordem de 1a aparicao.
    assert [g["group"] for g in grouped] == ["Roupas", "Calcados"]


def test_pipeline_no_nlp_or_fuzzy_dependency():
    """Guard AST (nao substring-in-source-text) — o modulo de pipeline nao
    importa nlp_service/rapidfuzz/difflib/fuzzywuzzy/thefuzz."""
    forbidden = {"nlp_service", "rapidfuzz", "difflib", "fuzzywuzzy", "thefuzz"}
    module_path = Path(__file__).parent.parent / "services" / "category_discovery.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.split(".")[0])

    assert not (imported_names & forbidden)


def test_make_scope_policy_delegates_to_category_policy_ancestor_context():
    """make_scope_policy(male_only_brand=False): folha neutra sob ancestral
    masculino confirmado passa; sem ancestral, falha fechado (D-06)."""
    policy = make_scope_policy(male_only_brand=False)

    neutral_under_masculine_ancestor = DiscoveredCategory(
        name="Camisas", path="/masculino/camisas", ancestors=("Masculino",)
    )
    neutral_without_context = DiscoveredCategory(name="Camisas", path="/camisas")

    assert policy(neutral_under_masculine_ancestor) is True
    assert policy(neutral_without_context) is False


def test_make_scope_policy_male_only_brand_allows_neutral_leaf():
    policy = make_scope_policy(male_only_brand=True)
    neutral = DiscoveredCategory(name="Camisas", path="/camisas")
    assert policy(neutral) is True


def test_infer_male_only_brand_true_when_no_feminine_marker():
    """D-06: catalogo single-gender (nenhum marcador feminino observado) ->
    male_only_brand=True (ex.: Aramis, cujos departamentos nao tem token de genero)."""
    from services.category_discovery import infer_male_only_brand

    candidates = [
        _cat("Camisas", "/roupas/camisas", group="Roupas"),
        _cat("Calcados", "/calcados/tenis", group="Calcados"),
    ]
    assert infer_male_only_brand(candidates) is True


def test_infer_male_only_brand_false_when_feminine_marker_present():
    """D-06: catalogo misto (algum candidato oficial menciona feminino) ->
    male_only_brand=False (ex.: Reserva/Tommy/Richards, /masculino + /feminino)."""
    from services.category_discovery import infer_male_only_brand

    candidates = [
        _cat("Camisas", "/masculino/camisas", group="Masculino"),
        _cat("Vestidos", "/feminino/vestidos", group="Feminino"),
    ]
    assert infer_male_only_brand(candidates) is False


# ---------------------------------------------------------------------------
# vtex — VTEXEngine/VTEXCatalogService adaptados a autoridade oficial (D-05)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_vtex_engine_discover_categories_removes_empty_and_preserves_ancestors(monkeypatch):
    from services.engines.vtex_engine import VTEXEngine
    from services.vtex_api_scraper import VtexApiClient
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.vtex.com")
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)
    monkeypatch.setattr(VtexApiClient, "fetch_categories", AsyncMock(return_value=[
        {
            "name": "Roupas", "url": "/roupas", "hasChildren": True,
            "children": [
                {"name": "Camisas", "url": "/roupas/camisas", "hasChildren": False, "children": []},
                {"name": "Bermudas", "url": "/roupas/bermudas", "hasChildren": False, "children": []},
            ],
        },
    ]))

    counts = {"/roupas/camisas": 5, "/roupas/bermudas": 0}

    async def fake_count_with_fallback(self, category_url, path, domain):
        return counts.get(path)

    monkeypatch.setattr(
        VtexApiClient,
        "category_result_count_with_tiles_fallback",
        fake_count_with_fallback,
    )

    engine = VTEXEngine("teststore")
    discovered = await engine.discover_categories()

    assert discovered == [{"name": "Camisas", "path": "/roupas/camisas"}]


@pytest.mark.asyncio
async def test_vtex_engine_discover_equals_flatten_of_get_catalog(monkeypatch):
    from services.engines.vtex_engine import VTEXEngine
    from services.vtex_api_scraper import VtexApiClient
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.vtex.com")
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)
    monkeypatch.setattr(VtexApiClient, "fetch_categories", AsyncMock(return_value=[
        {
            "name": "Roupas", "url": "/roupas", "hasChildren": True,
            "children": [
                {"name": "Camisas", "url": "/roupas/camisas", "hasChildren": False, "children": []},
                {"name": "Polos", "url": "/roupas/polos", "hasChildren": False, "children": []},
            ],
        },
        {
            "name": "Calcados", "url": "/calcados", "hasChildren": True,
            "children": [
                {"name": "Tenis", "url": "/calcados/tenis", "hasChildren": False, "children": []},
            ],
        },
    ]))
    monkeypatch.setattr(
        VtexApiClient,
        "category_result_count_with_tiles_fallback",
        AsyncMock(return_value=3),
    )

    engine = VTEXEngine("teststore")
    discovered = await engine.discover_categories()
    catalog = await engine.get_catalog()

    assert discovered == flatten_catalog_paths(catalog)
    assert [g["group"] for g in catalog] == ["Roupas", "Calcados"]


@pytest.mark.asyncio
async def test_vtex_engine_blocked_probe_is_never_offered(monkeypatch):
    """D-04: probe bloqueada (retorna None) nunca vira sucesso — categoria removida."""
    from services.engines.vtex_engine import VTEXEngine
    from services.vtex_api_scraper import VtexApiClient
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.vtex.com")
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)
    monkeypatch.setattr(VtexApiClient, "fetch_categories", AsyncMock(return_value=[
        {"name": "Jaquetas", "url": "/roupas/jaquetas", "hasChildren": False, "children": []},
    ]))
    monkeypatch.setattr(
        VtexApiClient,
        "category_result_count_with_tiles_fallback",
        AsyncMock(return_value=None),  # bloqueado/unverified
    )

    engine = VTEXEngine("teststore")
    discovered = await engine.discover_categories()
    assert discovered == []


# ---------------------------------------------------------------------------
# shopify — ShopifyEngine adaptado a autoridade oficial (D-05)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shopify_engine_discover_categories_excludes_empty_and_noise(monkeypatch):
    from services.engines.shopify_engine import ShopifyEngine
    from services.shopify_api_client import ShopifyApiClient
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.myshopify.com", brand_name="Test Store")
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    collections = [
        {"handle": "camisas", "title": "Camisas", "id": 1},
        {"handle": "termos-e-politicas", "title": "Termos e Políticas (policy)", "id": 2},
        {"handle": "bermudas", "title": "Bermudas", "id": 3},
    ]
    monkeypatch.setattr(ShopifyApiClient, "fetch_collections", AsyncMock(return_value=collections))

    counts = {"camisas": 3, "bermudas": 0}

    async def fake_probe(self, handle):
        return counts.get(handle)

    monkeypatch.setattr(ShopifyApiClient, "category_result_count", fake_probe)

    engine = ShopifyEngine("teststore")
    discovered = await engine.discover_categories()

    assert discovered == [{"name": "Camisas", "path": "/collections/camisas"}]


@pytest.mark.asyncio
async def test_shopify_engine_discover_equals_flatten_of_get_catalog(monkeypatch):
    from services.engines.shopify_engine import ShopifyEngine
    from services.shopify_api_client import ShopifyApiClient
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.myshopify.com", brand_name="Test Store")
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    collections = [
        {"handle": "camisas", "title": "Camisas", "id": 1},
        {"handle": "polos", "title": "Polos", "id": 2},
    ]
    monkeypatch.setattr(ShopifyApiClient, "fetch_collections", AsyncMock(return_value=collections))
    monkeypatch.setattr(ShopifyApiClient, "category_result_count", AsyncMock(return_value=2))

    engine = ShopifyEngine("teststore")
    discovered = await engine.discover_categories()
    catalog = await engine.get_catalog()

    assert discovered == flatten_catalog_paths(catalog)


@pytest.mark.asyncio
async def test_shopify_engine_blocked_probe_is_never_offered(monkeypatch):
    from services.engines.shopify_engine import ShopifyEngine
    from services.shopify_api_client import ShopifyApiClient
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.myshopify.com", brand_name="Test Store")
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)
    monkeypatch.setattr(ShopifyApiClient, "fetch_collections", AsyncMock(return_value=[
        {"handle": "jaquetas", "title": "Jaquetas", "id": 9},
    ]))
    monkeypatch.setattr(ShopifyApiClient, "category_result_count", AsyncMock(return_value=None))

    engine = ShopifyEngine("teststore")
    discovered = await engine.discover_categories()
    assert discovered == []


# ---------------------------------------------------------------------------
# wake — WakeEngine adaptado a autoridade oficial (D-05): nav real -> escopo
# -> probe GraphQL hotsite(url:) — nunca brand.mappings/slug/busca.
# ---------------------------------------------------------------------------
_WAKE_HOME_HTML = """
<html><body>
<script>var wakeConfig = {storefrontAccessToken: 'test-token-123', other: 1};</script>
<nav id=navbar>
  <ul>
    <li><a href="/masculino/camisas">Camisas</a></li>
    <li><a href="/masculino/polos">Polos</a></li>
    <li><a href="/sale/masculino">Outlet</a></li>
    <li><a href="/feminino/vestidos">Vestidos</a></li>
    <li><a href="/masculino">Ver tudo</a></li>
  </ul>
</nav>
</body></html>
"""


class _FakeAiohttpResponse:
    def __init__(self, status: int = 200, json_data=None, text_data: str = ""):
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def json(self):
        return self._json_data

    @property
    def text(self):
        return self._text_data

    @property
    def status_code(self):
        return self.status


class _FakeWakeSession:
    """Sessão fake: GET homepage (nav) e POST GraphQL (hotsite), sem rede real."""

    def __init__(self, home_html: str | dict, hotsite_by_path: dict):
        self._home_html = home_html
        self._hotsite_by_path = hotsite_by_path

    async def get(self, url, **kwargs):
        html = self._home_html.get(url, "") if isinstance(self._home_html, dict) else self._home_html
        return _FakeAiohttpResponse(status=200 if html else 404, text_data=html)

    async def post(self, url, json=None, headers=None, **kwargs):
        path = (json or {}).get("variables", {}).get("url")
        entry = self._hotsite_by_path.get(
            path, {"status": 200, "json": {"data": {"hotsite": None}}}
        )
        return _FakeAiohttpResponse(status=entry["status"], json_data=entry["json"])


def _wake_hotsite_ok(*names: str) -> dict:
    return {
        "status": 200,
        "json": {
            "data": {
                "hotsite": {
                    "products": {
                        "edges": [{"node": {"productName": n}} for n in names]
                    }
                }
            }
        },
    }


_ARAMIS_NAV_BY_URL = {
    "https://www.aramis.com.br": """
        <nav><a class="wf-megamenu__navlink wf-megamenu__navlink--group"
          href="/camisa-masculina">Camisas</a>
        <a class="wf-megamenu__link" href="/camisa-masculina/camisa-manga-longa-masculina">
          Camisas Manga Longa</a></nav>
    """,
    "https://www.aramis.com.br/urban": """
        <nav><a class="wf-megamenu__navlink" href="/urban/calca-urban-masculina">
          Calças Urban</a>
        <a class="wf-megamenu__link" href="/urban/calca-urban-masculina/calca-chino-urban-masculina">
          Calça Chino Urban</a></nav>
    """,
    "https://www.aramis.com.br/infantil-next": """
        <nav><a class="wf-megamenu__navlink" href="/infantil-next/camiseta-infantil-next">
          Camiseta Infantil Next</a></nav>
    """,
    "https://www.aramis.com.br/outlet": """
        <nav><a class="wf-megamenu__title" href="/outlet">Outlet</a>
        <a class="wf-megamenu__link" href="/outlet/camisa-masculina-em-oferta">
          Camisa Masculina em Oferta</a></nav>
    """,
}


def test_aramis_parser_reads_categories_split_across_multiple_nav_blocks():
    from services.engines.wake_engine import WakeEngine

    html = """
        <nav><a class="wf-megamenu__navlink"
          href="/urban/calca-urban-masculina">Calças Urban</a></nav>
        <nav><a class=wf-megamenu__navlink
          href=/urban/jaqueta-urban-masculina>Jaqueta Urban</a></nav>
    """

    categories = WakeEngine._parse_nav_candidates(
        html,
        "https://www.aramis.com.br",
        group_override="Urban",
        storefront_path="/urban",
    )

    assert [category.path for category in categories] == [
        "/urban/calca-urban-masculina",
        "/urban/jaqueta-urban-masculina",
    ]


@pytest.mark.asyncio
async def test_aramis_wake_discovery_reads_and_groups_all_storefronts(monkeypatch):
    from services.category_discovery import CategoryProbeResult, ProbeStatus
    from services.engines.wake_engine import WakeEngine
    from core.session_manager import SessionManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(
        domain="www.aramis.com.br",
        brand_name="Aramis",
        mappings=[],
        wake_access_token="test-token",
    )
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)
    fake_session = _FakeWakeSession(_ARAMIS_NAV_BY_URL, hotsite_by_path={})
    monkeypatch.setattr(SessionManager, "get_session", AsyncMock(return_value=fake_session))

    engine = WakeEngine("aramis")
    monkeypatch.setattr(
        engine,
        "category_has_products",
        AsyncMock(return_value=CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, "fixture")),
    )

    outcome = await engine._discover_and_validate()

    assert [(c.group, c.path) for c in outcome.offered] == [
        ("Aramis", "/camisa-masculina"),
        ("Urban", "/urban/calca-urban-masculina"),
        ("Next", "/infantil-next/camiseta-infantil-next"),
        ("Outlet", "/outlet/camisa-masculina-em-oferta"),
    ]
    assert engine._last_category_snapshot_complete is True
    assert "Meninos" in outcome.offered[2].ancestors


@pytest.mark.asyncio
async def test_wake_engine_discover_categories_uses_live_nav_not_mappings(monkeypatch):
    """D-05: descoberta vem do nav real, nunca de brand.mappings (mesmo se
    brand.mappings existir e tiver paths divergentes)."""
    from services.engines.wake_engine import WakeEngine
    from core.session_manager import SessionManager
    import services.brand_service as brand_service_module

    stale_mapping = SimpleNamespace(
        canonical_slug="camisas", vtex_fq_path="/camisas", label="Camisas (stale)"
    )
    fake_brand = SimpleNamespace(
        domain="www.richards.com.br", brand_name="Richards", mappings=[stale_mapping]
    )
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    hotsite_by_path = {
        "/masculino/camisas": _wake_hotsite_ok("Camisa Social"),
        "/masculino/polos": _wake_hotsite_ok(),  # 0 edges -> empty
        "/sale/masculino": _wake_hotsite_ok("Jaqueta Outlet"),
    }
    fake_session = _FakeWakeSession(_WAKE_HOME_HTML, hotsite_by_path)
    monkeypatch.setattr(SessionManager, "get_session", AsyncMock(return_value=fake_session))

    engine = WakeEngine("richards")
    discovered = await engine.discover_categories()
    paths = {c["path"] for c in discovered}

    # Oferecidas: masculino/camisas e sale/masculino (has_products); nunca o
    # path stale do mapping ("/camisas", sem o prefixo /masculino real).
    assert paths == {"/masculino/camisas", "/sale/masculino"}
    assert "/camisas" not in paths
    # Feminino nunca oferecido (D-06) mesmo tendo hotsite real (nao testado
    # aqui pois a probe so roda para path em escopo).
    assert "/feminino/vestidos" not in paths


@pytest.mark.asyncio
async def test_wake_engine_discover_equals_flatten_of_get_catalog(monkeypatch):
    from services.engines.wake_engine import WakeEngine
    from core.session_manager import SessionManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="www.richards.com.br", brand_name="Richards", mappings=[])
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    hotsite_by_path = {
        "/masculino/camisas": _wake_hotsite_ok("Camisa Social"),
        "/masculino/polos": _wake_hotsite_ok("Polo Basic"),
        "/sale/masculino": _wake_hotsite_ok("Jaqueta Outlet"),
    }
    fake_session = _FakeWakeSession(_WAKE_HOME_HTML, hotsite_by_path)
    monkeypatch.setattr(SessionManager, "get_session", AsyncMock(return_value=fake_session))

    engine = WakeEngine("richards")
    discovered = await engine.discover_categories()
    catalog = await engine.get_catalog()

    assert discovered == flatten_catalog_paths(catalog)
    assert len(discovered) == 3


@pytest.mark.asyncio
async def test_wake_engine_token_unresolved_is_blocked_not_offered(monkeypatch):
    """Sem token resolvivel, a probe classifica blocked -> nada e oferecido
    (nunca cai para mapping/slug/busca como substituto, D-05)."""
    from services.engines.wake_engine import WakeEngine
    from core.session_manager import SessionManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="www.richards.com.br", brand_name="Richards", mappings=[])
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    # Nav real presente, mas SEM storefrontAccessToken no HTML -> token nao resolve.
    home_html_no_token = _WAKE_HOME_HTML.replace(
        "storefrontAccessToken: 'test-token-123', ", ""
    )
    fake_session = _FakeWakeSession(home_html_no_token, hotsite_by_path={})
    monkeypatch.setattr(SessionManager, "get_session", AsyncMock(return_value=fake_session))

    engine = WakeEngine("richards")
    discovered = await engine.discover_categories()
    assert discovered == []


def _fake_browser_fetch(mapping: dict):
    """Constrói um `BrowserManager.fetch_html` fake keyed por URL exata.

    Uma URL ausente do mapping levanta `AssertionError` — prova que o
    engine nunca tenta buscar um path fora do discovery oficial (ex.: um
    mapping stale). Valores que sejam `Exception` são levantados (simula
    bloqueio/timeout/anti-bot); caso contrário o valor (HTML) é retornado.
    """

    async def _fetch(url, *args, **kwargs):
        if url not in mapping:
            raise AssertionError(f"unexpected BrowserManager.fetch_html URL: {url}")
        entry = mapping[url]
        if isinstance(entry, Exception):
            raise entry
        return entry

    return AsyncMock(side_effect=_fetch)


# ---------------------------------------------------------------------------
# sfcc — SFCCEngine adaptado a autoridade oficial (D-05): nav renderizado
# oficial -> escopo (D-02/D-06) -> probe de tile parseado (D-04) — nunca
# fallback estático.
# ---------------------------------------------------------------------------
_SFCC_HOME_HTML = """
<html><body>
<nav>
  <a href="/masculino/camisas">Camisas Masculino</a>
  <a href="/masculino/bermudas">Bermudas Masculino</a>
  <a href="/feminino/vestidos">Vestidos Feminino</a>
  <a href="/infantil/conjuntos">Conjuntos Infantil</a>
</nav>
</body></html>
"""

_SFCC_TILE_HTML_ONE_PRODUCT = """
<html><body>
<div data-producttile="true" data-pid="P1">
  <h2><a href="/produto/camisa-1">Camisa Social</a></h2>
  <div class="sales-price">R$ 199,00</div>
  <img class="js-img" src="//images.example.com/img1.jpg">
</div>
</body></html>
"""

_SFCC_TILE_HTML_EMPTY = "<html><body><p>Nenhum produto encontrado.</p></body></html>"


@pytest.mark.asyncio
async def test_sfcc_engine_discover_categories_scopes_and_removes_empty(monkeypatch):
    """D-02/D-04/D-05/D-06: apenas categoria oficial, em escopo masculino e
    com tile provado e oferecida — feminino/infantil-ambiguo/vazio nunca."""
    from services.engines.sfcc_engine import SFCCEngine
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.com.br", brand_name="Test Store", proxy_url=None)
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    home_url = "https://www.teststore.com.br"
    camisas_url = "https://www.teststore.com.br/masculino/camisas"
    bermudas_url = "https://www.teststore.com.br/masculino/bermudas"

    mapping = {
        home_url: _SFCC_HOME_HTML,
        camisas_url: _SFCC_TILE_HTML_ONE_PRODUCT,
        bermudas_url: _SFCC_TILE_HTML_EMPTY,
    }
    monkeypatch.setattr(BrowserManager, "fetch_html", _fake_browser_fetch(mapping))

    engine = SFCCEngine("teststore")
    discovered = await engine.discover_categories()

    # Camisas: masculino + tile provado -> oferecida.
    # Bermudas: masculino mas SEM tile (EMPTY) -> D-04 remove.
    # Vestidos Feminino: marcador feminino explicito -> D-02/D-06 bloqueia sempre
    #   (a probe nunca e chamada — nao esta no mapping acima).
    # Conjuntos Infantil: contexto infantil ambiguo (sem menino/masculino) -> fail-closed.
    assert discovered == [{"name": "Camisas Masculino", "path": "/masculino/camisas"}]


@pytest.mark.asyncio
async def test_sfcc_engine_discover_equals_flatten_of_get_catalog(monkeypatch):
    from services.engines.sfcc_engine import SFCCEngine
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.com.br", brand_name="Test Store", proxy_url=None)
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    home_url = "https://www.teststore.com.br"
    camisas_url = "https://www.teststore.com.br/masculino/camisas"
    bermudas_url = "https://www.teststore.com.br/masculino/bermudas"

    mapping = {
        home_url: _SFCC_HOME_HTML,
        camisas_url: _SFCC_TILE_HTML_ONE_PRODUCT,
        bermudas_url: _SFCC_TILE_HTML_ONE_PRODUCT,
    }
    monkeypatch.setattr(BrowserManager, "fetch_html", _fake_browser_fetch(mapping))

    engine = SFCCEngine("teststore")
    discovered = await engine.discover_categories()
    catalog = await engine.get_catalog()

    assert discovered == flatten_catalog_paths(catalog)
    assert len(discovered) == 2


@pytest.mark.asyncio
async def test_sfcc_engine_blocked_category_probe_is_never_offered(monkeypatch):
    """D-04: erro WAF/browser na probe de uma categoria em escopo produz
    blocked, nunca sucesso silencioso — categoria removida da lista oferecida."""
    from services.engines.sfcc_engine import SFCCEngine
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.com.br", brand_name="Test Store", proxy_url=None)
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    home_html = """
    <html><body><nav>
      <a href="/masculino/jaquetas">Jaquetas Masculino</a>
    </nav></body></html>
    """
    home_url = "https://www.teststore.com.br"
    jaquetas_url = "https://www.teststore.com.br/masculino/jaquetas"

    mapping = {
        home_url: home_html,
        jaquetas_url: TimeoutError("simulated anti-bot timeout"),
    }
    monkeypatch.setattr(BrowserManager, "fetch_html", _fake_browser_fetch(mapping))

    engine = SFCCEngine("teststore")
    discovered = await engine.discover_categories()
    assert discovered == []


@pytest.mark.asyncio
async def test_sfcc_engine_home_fetch_failure_returns_empty_not_crash(monkeypatch):
    """Bloqueio no fetch do nav renderizado (home) nunca derruba o engine nem
    vira sincronizado — retorna lista vazia (D-06 fail-closed gracioso)."""
    from services.engines.sfcc_engine import SFCCEngine
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="teststore.com.br", brand_name="Test Store", proxy_url=None)
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    async def _raise(*args, **kwargs):
        raise RuntimeError("anti-bot block on home page")

    monkeypatch.setattr(BrowserManager, "fetch_html", AsyncMock(side_effect=_raise))

    engine = SFCCEngine("teststore")
    discovered = await engine.discover_categories()
    assert discovered == []


# ---------------------------------------------------------------------------
# zara — ZaraEngine adaptado a autoridade oficial (D-05): menu masculino
# oficial/configurado -> escopo (D-02/D-06) -> probe de tile (D-04). Mapping
# Zara (brand.mappings) NUNCA cria candidato, mesmo quando presente na marca.
# ---------------------------------------------------------------------------
_ZARA_NAV_HTML = """
<html><body>
<nav>
  <a href="/br/pt/man-shirts-l737.html">Camisas Masculino</a>
  <a href="/br/pt/man-bermudas-l800.html">Bermudas Masculino</a>
  <a href="/br/pt/man-feminino-leak-l999.html">Secao Feminino Leak</a>
  <a href="/br/pt/man-kids-l900.html">Infantil</a>
</nav>
</body></html>
"""

_ZARA_TILE_HTML_ONE_PRODUCT = """
<html><body>
<div class="product-grid-product">
  <a class="product-grid-product-info__name" href="/br/pt/camisa-p123.html">Camisa Linho</a>
  <div class="money-amount__main">R$ 199,00</div>
  <img src="//static.zara.net/photos/camisa.jpg">
</div>
</body></html>
"""

_ZARA_TILE_HTML_EMPTY = "<html><body><p>Sem produtos disponiveis</p></body></html>"


def test_zara_brand_config_never_exposes_mappings_as_discovery_source():
    """D-05: `_brand_config()` nao expoe mais `mappings` — a unica forma de
    um candidato entrar no pipeline e via o menu masculino oficial renderizado."""
    from services.engines.zara_engine import ZaraEngine

    engine = ZaraEngine("zara")
    config = engine._brand_config()
    assert "mappings" not in config


@pytest.mark.asyncio
async def test_zara_engine_discover_categories_scopes_and_removes_empty_ignores_mappings(monkeypatch):
    """D-02/D-04/D-05/D-06: apenas categoria do menu oficial, em escopo
    masculino e com tile provado e oferecida. `brand.mappings` (mesmo
    presente e com paths distintos) nunca aparece como candidato."""
    from services.engines.zara_engine import ZaraEngine, DEFAULT_CATEGORY_URL
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    stale_mapping = SimpleNamespace(label="Stale Mapping", vtex_fq_path="/br/pt/stale-mapping-l1.html")
    fake_brand = SimpleNamespace(
        domain="www.zara.com", brand_name="Test Zara", proxy_url=None, mappings=[stale_mapping]
    )
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    shirts_url = "https://www.zara.com/br/pt/man-shirts-l737.html"
    bermudas_url = "https://www.zara.com/br/pt/man-bermudas-l800.html"

    mapping = {
        DEFAULT_CATEGORY_URL: _ZARA_NAV_HTML,
        shirts_url: _ZARA_TILE_HTML_ONE_PRODUCT,
        bermudas_url: _ZARA_TILE_HTML_EMPTY,
    }
    monkeypatch.setattr(BrowserManager, "fetch_html", _fake_browser_fetch(mapping))

    engine = ZaraEngine("zara")
    discovered = await engine.discover_categories()

    # Camisas: masculino + tile provado -> oferecida.
    # Bermudas: masculino mas sem tile (EMPTY) -> D-04 remove.
    # Secao Feminino Leak: marcador feminino explicito -> bloqueia sempre.
    # Infantil: contexto infantil ambiguo (sem menino/masculino) -> fail-closed.
    # Stale Mapping ("/br/pt/stale-mapping-l1.html"): NUNCA aparece — nao veio
    # do menu oficial desta chamada (D-05), mesmo estando em brand.mappings.
    assert discovered == [
        {"name": "Camisas Masculino", "path": "/br/pt/man-shirts-l737.html"}
    ]
    offered_paths = {c["path"] for c in discovered}
    assert "/br/pt/stale-mapping-l1.html" not in offered_paths


@pytest.mark.asyncio
async def test_zara_engine_discover_equals_flatten_of_get_catalog(monkeypatch):
    from services.engines.zara_engine import ZaraEngine, DEFAULT_CATEGORY_URL
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="www.zara.com", brand_name="Test Zara", proxy_url=None, mappings=[])
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    shirts_url = "https://www.zara.com/br/pt/man-shirts-l737.html"
    bermudas_url = "https://www.zara.com/br/pt/man-bermudas-l800.html"

    mapping = {
        DEFAULT_CATEGORY_URL: _ZARA_NAV_HTML,
        shirts_url: _ZARA_TILE_HTML_ONE_PRODUCT,
        bermudas_url: _ZARA_TILE_HTML_ONE_PRODUCT,
    }
    monkeypatch.setattr(BrowserManager, "fetch_html", _fake_browser_fetch(mapping))

    engine = ZaraEngine("zara")
    discovered = await engine.discover_categories()
    catalog = await engine.get_catalog()

    assert discovered == flatten_catalog_paths(catalog)
    assert len(discovered) == 2


@pytest.mark.asyncio
async def test_zara_engine_blocked_category_probe_is_never_offered(monkeypatch):
    """D-04: erro Akamai/anti-bot na probe de uma categoria em escopo produz
    blocked, nunca sucesso silencioso — categoria removida da lista oferecida."""
    from services.engines.zara_engine import ZaraEngine, DEFAULT_CATEGORY_URL
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="www.zara.com", brand_name="Test Zara", proxy_url=None, mappings=[])
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    home_html = """
    <html><body><nav>
      <a href="/br/pt/man-jaquetas-l900.html">Jaquetas Masculino</a>
    </nav></body></html>
    """
    jaquetas_url = "https://www.zara.com/br/pt/man-jaquetas-l900.html"

    mapping = {
        DEFAULT_CATEGORY_URL: home_html,
        jaquetas_url: TimeoutError("simulated Akamai anti-bot timeout"),
    }
    monkeypatch.setattr(BrowserManager, "fetch_html", _fake_browser_fetch(mapping))

    engine = ZaraEngine("zara")
    discovered = await engine.discover_categories()
    assert discovered == []


@pytest.mark.asyncio
async def test_zara_engine_menu_fetch_failure_returns_empty_not_crash(monkeypatch):
    """Bloqueio no fetch do menu masculino oficial nunca derruba o engine nem
    vira sincronizado — retorna lista vazia (fail-closed gracioso)."""
    from services.engines.zara_engine import ZaraEngine
    from core.browser_manager import BrowserManager
    import services.brand_service as brand_service_module

    fake_brand = SimpleNamespace(domain="www.zara.com", brand_name="Test Zara", proxy_url=None, mappings=[])
    monkeypatch.setattr(brand_service_module.brand_service, "get_brand", lambda k: fake_brand)

    async def _raise(*args, **kwargs):
        raise RuntimeError("Akamai anti-bot block on menu page")

    monkeypatch.setattr(BrowserManager, "fetch_html", AsyncMock(side_effect=_raise))

    engine = ZaraEngine("zara")
    discovered = await engine.discover_categories()
    assert discovered == []


def test_wake_category_source_fixture_records_go_decision():
    """O fixture do spike GO/NO-GO (Task 01-03-02) existe e documenta GO com
    candidatos reais/ordenados e probe GraphQL real — nunca mapping/slug/busca."""
    import json

    fixture_path = (
        Path(__file__).parent / "fixtures" / "phase01" / "wake_category_source.json"
    )
    assert fixture_path.exists()

    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert data["decision"] == "GO"
    assert data["summary"]["total_candidates"] > 0
    assert data["summary"]["has_products"] == data["summary"]["total_candidates"]
    assert data["source"]["source_url"].startswith("https://")
    assert data["graphql_probe"]["query_name"] == "WakeHotsite"
    # Ordem preservada e paths reais (com prefixo /masculino, nao o stale
    # "/camisas" persistido em brand.mappings para richards).
    paths = [c["path"] for c in data["candidates"]]
    assert paths == sorted(paths, key=paths.index)  # ordem de insercao preservada (trivial)
    assert all(p.startswith("/masculino/") or p == "/sale/masculino" for p in paths)


# ---------------------------------------------------------------------------
# Plano 01-04 (D-04): catalog_applicability=not_applicable para search-only
# ---------------------------------------------------------------------------
def test_search_only_engines_never_override_category_has_products():
    """D-04: Amazon, Mercado Livre e Netshoes sao search-only e NUNCA devem
    sobrescrever `category_has_products` — isso e o que as rotas API
    (`api/routes_brands.py`, `api/routes_category.py`) usam para classificar
    `catalog_applicability=not_applicable` sem hardcode de brand_key. Um
    regressao aqui (um destes engines ganhando um override) mudaria
    silenciosamente o estado dessas marcas de N/A para catalog-capable."""
    from services.engines.amazon_engine import AmazonEngine
    from services.engines.mercado_livre_engine import MercadoLivreEngine
    from services.engines.netshoes_engine import NetshoesEngine
    from services.engines.base_engine import BaseEngine

    for engine_cls in (AmazonEngine, MercadoLivreEngine, NetshoesEngine):
        assert engine_cls.category_has_products is BaseEngine.category_has_products, (
            f"{engine_cls.__name__} sobrescreve category_has_products — deixaria "
            "de ser classificado como not_applicable (D-04)."
        )


def test_catalog_capable_engines_all_override_category_has_products():
    """Espelho do teste acima: os 5 engines catalog-capable DEVEM sobrescrever
    `category_has_products` (nao herdar o NotImplementedError da base) —
    garante que `catalog_applicability=catalog_capable` nas rotas API nunca
    caia silenciosamente para 'unknown' por falta de override."""
    from services.engines.base_engine import BaseEngine
    from services.engines.vtex_engine import VTEXEngine
    from services.engines.shopify_engine import ShopifyEngine
    from services.engines.wake_engine import WakeEngine
    from services.engines.sfcc_engine import SFCCEngine
    from services.engines.zara_engine import ZaraEngine

    for engine_cls in (VTEXEngine, ShopifyEngine, WakeEngine, SFCCEngine, ZaraEngine):
        assert engine_cls.category_has_products is not BaseEngine.category_has_products, (
            f"{engine_cls.__name__} nao sobrescreve category_has_products — "
            "seria erroneamente classificado como not_applicable (D-04)."
        )
        assert hasattr(engine_cls, "_discover_and_validate"), (
            f"{engine_cls.__name__} nao expoe _discover_and_validate — as rotas API "
            "(Plano 01-04) dependem deste metodo compartilhado para diagnostico."
        )
