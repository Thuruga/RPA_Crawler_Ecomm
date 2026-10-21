"""
Cache de descoberta de categorias — services/category_discovery_cache.py.

O Redis saiu. O cache hoje tem duas camadas, ambas dentro do container:

1. `TTLCache` na RAM do processo (`_cache`) — camada quente, por container;
2. tabela `brand_category_mappings` no Postgres — camada persistente, que
   sobrevive a restart e e compartilhada entre replicas.

`_resolve_get_catalog` consulta as duas antes de cair no crawl ao vivo
(RAM -> banco -> ao vivo), e e esse encadeamento que substitui o que o Redis
fazia. `resolve_discover_categories` usa apenas a camada de RAM.

Hermetic: nenhuma conexao real: a camada de banco entra via AsyncMock em
`core.db.db`, e a de RAM e o proprio dict do processo.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from services import category_discovery_cache as cache_module
from services.category_discovery import (
    CategoryProbeResult,
    CategoryValidationOutcome,
    DiscoveredCategory,
    ProbeStatus,
)
from services.category_discovery_cache import (
    get_cached_outcome,
    load_from_db,
    persist_to_db,
    set_cached_outcome,
)
from scripts.benchmark_category_discovery_cache import (
    calculate_calls_saved,
    count_blocked_probes,
    decide_cache_disposition,
)


@pytest.fixture(autouse=True)
def _clean_ram_cache():
    """`_cache` e um singleton de modulo: sem limpar, um teste contamina o outro."""
    cache_module._cache.clear()
    yield
    cache_module._cache.clear()


def _sample_outcome() -> CategoryValidationOutcome:
    return CategoryValidationOutcome(
        offered=[
            DiscoveredCategory(
                name="Camisetas", path="/roupas/camisetas", url="https://loja.com/roupas/camisetas",
                group="Roupas", ancestors=("Roupas",),
            ),
        ],
        probes={
            "/roupas/camisetas": CategoryProbeResult(ProbeStatus.HAS_PRODUCTS, detail="12 produto(s)"),
            "/roupas/vazia": CategoryProbeResult(ProbeStatus.EMPTY, detail="0 produtos"),
            "/roupas/bloqueada": CategoryProbeResult(ProbeStatus.BLOCKED, detail="probe timeout"),
        },
    )


# ---------------------------------------------------------------------------
# Camada 1 — TTLCache na RAM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_preserves_outcome_including_tuples_and_enums():
    original = _sample_outcome()

    await set_cached_outcome("testbrand", original)
    result = await get_cached_outcome("testbrand")

    assert result is not None
    assert result.offered == original.offered
    assert result.probes == original.probes
    assert isinstance(result.offered[0].ancestors, tuple)
    assert result.probes["/roupas/bloqueada"].status is ProbeStatus.BLOCKED


@pytest.mark.asyncio
async def test_cache_miss_returns_none():
    assert await get_cached_outcome("nunca-cacheada") is None


@pytest.mark.asyncio
async def test_brand_key_is_case_insensitive():
    """Chave normalizada em lower: `Tommy` e `tommy` sao a mesma marca."""
    await set_cached_outcome("Tommy", _sample_outcome())

    assert await get_cached_outcome("tommy") is not None
    assert await get_cached_outcome("TOMMY") is not None


def test_ttl_comes_from_configured_setting():
    """O TTL e amarrado na construcao do TTLCache, no import do modulo."""
    assert cache_module._cache.ttl == settings.CATEGORY_DISCOVERY_CACHE_TTL_SECONDS


@pytest.mark.asyncio
async def test_ram_cache_is_bounded_and_evicts_instead_of_growing():
    """maxsize existe para o cache nao virar vazamento de memoria no container."""
    maxsize = cache_module._cache.maxsize

    for index in range(maxsize + 10):
        await set_cached_outcome(f"marca-{index}", _sample_outcome())

    assert len(cache_module._cache) <= maxsize


# ---------------------------------------------------------------------------
# Camada 2 — persistencia em Postgres
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_to_db_skips_write_when_nothing_was_offered():
    """Outcome vazio nunca apaga nem sobrescreve o que ja esta no banco."""
    fake_db = AsyncMock()

    with patch("core.db.db", fake_db):
        await persist_to_db("testbrand", CategoryValidationOutcome(offered=[], probes={}))

    fake_db.execute_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_to_db_unknown_brand_does_not_raise_or_write():
    fake_db = AsyncMock()
    fake_db.query_raw.return_value = []  # marca inexistente em `brands`

    with patch("core.db.db", fake_db):
        await persist_to_db("marca-fantasma", _sample_outcome())

    fake_db.execute_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_to_db_upserts_one_row_per_offered_category():
    fake_db = AsyncMock()
    fake_db.query_raw.side_effect = [[{"id": 7}], []]

    with patch("core.db.db", fake_db):
        await persist_to_db("testbrand", _sample_outcome())

    assert fake_db.execute_raw.await_count == 1
    sql = fake_db.execute_raw.await_args.args[0]
    # Idempotencia e o contrato aqui: reprocessar uma marca nao duplica linha.
    assert "ON CONFLICT" in sql


@pytest.mark.asyncio
async def test_persist_to_db_reuses_existing_semantic_slug_by_source_path():
    fake_db = AsyncMock()
    fake_db.query_raw.side_effect = [
        [{"id": 7}],
        [{"id": 22, "canonical_slug": "camisas", "source_path": "/camisa-masculina"}],
    ]
    outcome = CategoryValidationOutcome(
        offered=[
            DiscoveredCategory(
                name="Camisas",
                path="/camisa-masculina",
                group="Aramis",
            )
        ],
        probes={},
    )

    with patch("core.db.db", fake_db):
        await persist_to_db("aramis", outcome)

    sql = fake_db.execute_raw.await_args.args[0]
    assert "UPDATE brand_category_mappings" in sql
    assert fake_db.execute_raw.await_args.args[-1] == 22


@pytest.mark.asyncio
async def test_load_from_db_returns_none_when_brand_has_no_rows():
    fake_db = AsyncMock()
    fake_db.query_raw.return_value = []

    with patch("core.db.db", fake_db):
        assert await load_from_db("testbrand") is None


@pytest.mark.asyncio
async def test_load_from_db_rebuilds_outcome_marked_has_products():
    """Linha no banco = conteudo ja provado por crawl anterior."""
    fake_db = AsyncMock()
    fake_db.query_raw.return_value = [
        {
            "canonical_slug": "/roupas/camisetas",
            "source_path": "https://loja.com/roupas/camisetas",
            "label": "Camisetas",
        }
    ]

    with patch("core.db.db", fake_db):
        outcome = await load_from_db("testbrand")

    assert outcome is not None
    assert [c.name for c in outcome.offered] == ["Camisetas"]
    assert [c.path for c in outcome.offered] == ["/roupas/camisetas"]
    status = outcome.probes[outcome.offered[0].identity].status
    assert status is ProbeStatus.HAS_PRODUCTS


@pytest.mark.asyncio
async def test_load_from_db_aramis_uses_source_path_and_restores_site_group():
    fake_db = AsyncMock()
    fake_db.query_raw.return_value = [
        {
            "canonical_slug": "urban-camisas",
            "source_path": "/urban/camisa-urban-masculina",
            "label": "Camisa Urban",
        },
        {
            "canonical_slug": "next-camisetas",
            "source_path": "/infantil-next/camiseta-infantil-next",
            "label": "Camiseta Infantil Next",
        },
    ]

    with patch("core.db.db", fake_db):
        outcome = await load_from_db("aramis")

    assert outcome is not None
    assert [(c.group, c.path) for c in outcome.offered] == [
        ("Urban", "/urban/camisa-urban-masculina"),
        ("Next", "/infantil-next/camiseta-infantil-next"),
    ]
    assert outcome.offered[1].ancestors == ("Next", "Meninos")


# ---------------------------------------------------------------------------
# Integracao nas rotas — o comportamento que a reclamacao de lentidao exige
# ---------------------------------------------------------------------------


class _FakeCatalogCapableEngine:
    """Stand-in com override real (nao-BaseEngine) de `category_has_products`,
    para que `_is_catalog_capable`/`is_catalog_capable` o reconhecam."""

    def __init__(self, brand_key: str, outcome: CategoryValidationOutcome):
        self.brand_key = brand_key
        self._outcome = outcome
        self.discover_and_validate_calls = 0

    async def category_has_products(self, candidate):
        raise NotImplementedError  # nunca chamado nestes testes

    async def _discover_and_validate(self):
        self.discover_and_validate_calls += 1
        return self._outcome


def _db_without_rows() -> AsyncMock:
    """Banco acessivel, porem sem categorias — forca o caminho ao vivo."""
    fake_db = AsyncMock()
    fake_db.query_raw.return_value = []
    return fake_db


@pytest.mark.asyncio
async def test_resolve_get_catalog_ram_hit_skips_live_discovery():
    from api.routes_category import _resolve_get_catalog

    engine = _FakeCatalogCapableEngine("testbrand", _sample_outcome())

    with patch("core.db.db", _db_without_rows()):
        await _resolve_get_catalog(engine)
        await _resolve_get_catalog(engine)

    assert engine.discover_and_validate_calls == 1


@pytest.mark.asyncio
async def test_resolve_discover_categories_ram_hit_skips_live_discovery():
    from api.routes_brands import resolve_discover_categories

    engine = _FakeCatalogCapableEngine("testbrand", _sample_outcome())

    await resolve_discover_categories(engine)
    categories, _ = await resolve_discover_categories(engine)

    assert engine.discover_and_validate_calls == 1
    assert categories == [{"name": "Camisetas", "path": "/roupas/camisetas"}]


@pytest.mark.asyncio
async def test_resolve_get_catalog_db_hit_skips_live_discovery_on_cold_ram():
    """A camada que substitui o Redis: RAM fria (restart do container) mas banco
    quente nao deve disparar crawl ao vivo."""
    from api.routes_category import _resolve_get_catalog

    fake_db = AsyncMock()
    fake_db.query_raw.return_value = [
        {
            "canonical_slug": "/roupas/camisetas",
            "source_path": "https://loja.com/roupas/camisetas",
            "label": "Camisetas",
        }
    ]
    engine = _FakeCatalogCapableEngine("testbrand", _sample_outcome())

    with patch("core.db.db", fake_db):
        categories, diagnostic = await _resolve_get_catalog(engine)

    assert engine.discover_and_validate_calls == 0
    assert diagnostic["state"] == "ok"
    # `load_from_db` nao reconstroi `group` (a coluna nao existe), entao o
    # agrupador cai no rotulo default -- diferente do caminho ao vivo, que
    # preserva "Roupas".
    assert categories == [
        {"group": "Categorias", "items": [{"label": "Camisetas", "path": "/roupas/camisetas"}]}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_call", ["query_raw", "execute_raw"])
async def test_resolve_get_catalog_survives_database_failure(failing_call):
    """Banco indisponivel degrada para crawl ao vivo — nunca vira erro de rota.
    E o analogo direto do que os testes de 'redis down' garantiam antes."""
    from api.routes_category import _resolve_get_catalog

    fake_db = AsyncMock()
    fake_db.query_raw.return_value = []
    setattr(fake_db, failing_call, AsyncMock(side_effect=ConnectionError("db down")))

    engine = _FakeCatalogCapableEngine("testbrand", _sample_outcome())

    with patch("core.db.db", fake_db):
        categories, diagnostic = await _resolve_get_catalog(engine)

    assert diagnostic["state"] == "ok"
    assert engine.discover_and_validate_calls == 1
    assert categories == [
        {"group": "Roupas", "items": [{"label": "Camisetas", "path": "/roupas/camisetas"}]}
    ]


# ---------------------------------------------------------------------------
# Guardas de arquitetura
# ---------------------------------------------------------------------------


def _production_python_files() -> list[Path]:
    backend_root = Path("backend")
    return [
        path
        for path in backend_root.rglob("*.py")
        if "tests" not in path.parts
        and "scripts" not in path.parts
        and "__pycache__" not in path.parts
    ]


def test_no_production_file_still_imports_the_deleted_redis_manager():
    """`core/redis_manager.py` foi removido. Qualquer import remanescente e um
    ImportError esperando execucao — foi exatamente o que quebrou
    clear_cache.py e clear_all_caches.py sem ninguem notar."""
    offenders = [
        path.as_posix()
        for path in _production_python_files()
        if "core.redis_manager" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_cache_service_is_the_single_owner_of_the_ram_cache():
    """Só o serviço de cache instancia o TTLCache; o resto passa por
    get/set_cached_outcome, para haver um lugar unico onde TTL e chave vivem."""
    owners = [
        path.as_posix()
        for path in _production_python_files()
        if "TTLCache(" in path.read_text(encoding="utf-8")
    ]

    assert owners == ["backend/services/category_discovery_cache.py"]
    cache_source = Path("backend/services/category_discovery_cache.py").read_text(encoding="utf-8")
    assert "CATEGORY_DISCOVERY_CACHE_TTL_SECONDS" in cache_source


# ---------------------------------------------------------------------------
# Funcoes puras do benchmark (independentes de backend de cache)
# ---------------------------------------------------------------------------


def test_benchmark_decision_requires_clear_gain_threshold():
    assert calculate_calls_saved(uncached_live_calls=5, cached_live_calls=1) == 4
    assert (
        decide_cache_disposition(
            repetitions=5,
            uncached_live_calls=5,
            cached_live_calls=1,
            warm_live_calls=0,
            min_saved_percent=80.0,
        )
        == "retained"
    )
    assert (
        decide_cache_disposition(
            repetitions=5,
            uncached_live_calls=5,
            cached_live_calls=2,
            warm_live_calls=1,
            min_saved_percent=80.0,
        )
        == "removed"
    )


def test_count_blocked_probes_counts_blocked_validation_entries():
    assert count_blocked_probes(_sample_outcome()) == 1
