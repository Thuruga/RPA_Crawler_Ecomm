"""
Cache em Memória (curto/médio prazo) do resultado JÁ VALIDADO da descoberta de
categorias — compartilhado entre TODOS os engines catalog-capable e as rotas de API
sem depender de Redis (D-13).

Mantém a mesma garantia: uma entrada expirada NUNCA é servida como atual.
Se o pre-warm falhar, degrada para busca ao vivo.
O uso de pg_try_advisory_xact_lock previne que múltiplos workers simultâneos
façam a mesma varredura de API externa.

Persistência (D-14): as categorias descobertas são salvas na tabela
`brand_category_mappings` do PostgreSQL após cada crawl. O pre-warm de startup
lê primeiro do banco (sem crawl externo), garantindo que reinicializações do
servidor (Azure restarts, deploys, --reload) NÃO zeram o cache visível ao usuário.
"""
from __future__ import annotations

import logging
import asyncio
from typing import Optional
from urllib.parse import urlsplit
from cachetools import TTLCache

from config import settings
from services.category_discovery import CategoryValidationOutcome
from services.wake_category_paths import canonicalize_wake_category_source

logger = logging.getLogger("CategoryDiscoveryCache")

# Cache em memória na RAM do processo Python.
# Maxsize 100 é suficiente para armazenar o catálogo de todas as marcas ativas.
_cache = TTLCache(maxsize=100, ttl=settings.CATEGORY_DISCOVERY_CACHE_TTL_SECONDS)


async def get_cached_outcome(brand_key: str) -> Optional[CategoryValidationOutcome]:
    """Retorna o resultado cacheado (ainda dentro do TTL) ou `None` em cache miss."""
    return _cache.get(brand_key.lower())


async def set_cached_outcome(brand_key: str, outcome: CategoryValidationOutcome) -> None:
    """Grava o resultado no cache RAM com o TTL configurado."""
    _cache[brand_key.lower()] = outcome


async def invalidate_cached_outcome(brand_key: str) -> None:
    """Remove uma marca da camada RAM após alteração do catálogo persistido."""
    _cache.pop(brand_key.lower(), None)


# ---------------------------------------------------------------------------
# Pre-warm orquestrado (advisory lock para multi-worker safety)
# ---------------------------------------------------------------------------

# Chave fixa do advisory lock do pre-warm.
#
# NÃO derivar de `hash("...")`: o hash de str em Python é randomizado por
# processo (PYTHONHASHSEED), então cada worker calcularia uma chave diferente e
# o lock deixaria de excluir quem quer que fosse — exclusão mútua só na
# aparência. Pior: a versão anterior mascarava com `& 0xFFFFFFFF`, que passa do
# teto de int4, e aí o Postgres tipava o literal como bigint e a chamada de dois
# argumentos não casava com nenhuma sobrecarga (`pg_try_advisory_xact_lock`
# aceita `(bigint)` ou `(int, int)`) — erro em ~metade dos boots, conforme o
# hash sorteado.
#
# Valor arbitrário e estável, na forma de um argumento bigint.
_PRE_WARM_ADVISORY_LOCK_KEY = 4_213_559_001

async def pre_warm_all_brands_cache(force_refresh: bool = False) -> None:
    """Pré-aquece o cache de categorias para todas as marcas ativas.

    Estratégia de duas camadas no startup:
      1. Lê brand_category_mappings do banco (rápido, sem crawl externo).
         Se houver dados persistidos, usa-os diretamente → startup instantâneo.
      2. Se o banco estiver vazio para a marca (primeira vez ou após limpeza),
         faz crawl ao vivo, persiste no banco e popula o cache.

    Com ``force_refresh=True`` (job diário), ignora a camada persistida e pede
    uma nova fotografia ao engine. A substituição integral só ocorre quando o
    próprio engine confirma que a descoberta foi completa e sem bloqueios.

    O advisory lock garante que múltiplos workers não façam o crawl ao mesmo tempo.
    """
    from core.db import db

    try:
        res = await db.query_raw(
            f"SELECT pg_try_advisory_lock({_PRE_WARM_ADVISORY_LOCK_KEY}) as locked"
        )
        locked = res[0].get("locked", False) if res else False
        if not locked:
            logger.info(
                "Outro worker já está realizando o pre-warm (advisory lock ocupado). Bypass..."
            )
            return

        try:
            await _run_pre_warm(force_refresh=force_refresh)
        finally:
            try:
                await db.query_raw(
                    f"SELECT pg_advisory_unlock({_PRE_WARM_ADVISORY_LOCK_KEY})"
                )
            except Exception:
                pass
    except Exception as exc:
        logger.error("Erro fatal no pre-warm ou na aquisição do lock: %s", exc)


async def _run_pre_warm(force_refresh: bool = False) -> None:
    from services.brand_service import brand_service
    from services.engines.factory import engine_factory

    brands = brand_service.list_brands(active_only=True)
    logger.info("Iniciando pre-warm do cache de categorias para %d marcas.", len(brands))

    db_hits = 0
    crawl_hits = 0
    errors = 0

    for brand in brands:
        try:
            engine = engine_factory.get_engine(brand.brand_key)
            if not hasattr(engine, "_discover_and_validate"):
                continue

            # --- Camada 1: banco de dados (sem crawl externo) ---
            if not force_refresh:
                outcome = await load_from_db(brand.brand_key)
                if outcome is not None:
                    await set_cached_outcome(brand.brand_key, outcome)
                    db_hits += 1
                    logger.info(
                        "[%s] Cache pre-warmed a partir do banco (%d categorias).",
                        brand.brand_key,
                        len(outcome.offered),
                    )
                    continue

            # --- Camada 2: crawl ao vivo + persiste para próximas reinicializações ---
            crawl_reason = "refresh diário" if force_refresh else "banco vazio"
            logger.info("[%s] %s → crawl ao vivo.", brand.brand_key, crawl_reason)
            outcome = await engine._discover_and_validate()
            replace_existing = bool(
                force_refresh
                and getattr(engine, "_last_category_snapshot_complete", False)
                and outcome.offered
                and all(probe.status.value != "blocked" for probe in outcome.probes.values())
            )
            await persist_to_db(
                brand.brand_key,
                outcome,
                replace_existing=replace_existing,
            )
            await set_cached_outcome(brand.brand_key, outcome)
            crawl_hits += 1
            logger.info(
                "[%s] Cache pre-warmed via crawl ao vivo (%d categorias persistidas).",
                brand.brand_key,
                len(outcome.offered),
            )

        except Exception as exc:
            errors += 1
            logger.error("[%s] Erro no pre-warm de categoria: %s", brand.brand_key, exc)

        # Pequena pausa entre marcas para não saturar rede (só relevante no crawl ao vivo)
        await asyncio.sleep(1)

    logger.info(
        "Pre-warm concluído: %d do banco, %d via crawl ao vivo, %d erros.",
        db_hits,
        crawl_hits,
        errors,
    )


# ---------------------------------------------------------------------------
# Persistência no banco — brand_category_mappings
# ---------------------------------------------------------------------------

def _normalize_source_path(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path if parsed.scheme and parsed.netloc else raw
    normalized = "/" + path.strip("/") if path.strip("/") else ""
    return normalized.casefold()


def _persisted_group(brand_key: str, path: str) -> tuple[str, tuple[str, ...]]:
    """Reconstrói grupos estáveis quando a tabela legada não possui coluna de grupo."""
    if brand_key.casefold() != "aramis":
        return "", ()
    normalized = _normalize_source_path(path)
    if normalized.startswith("/urban/"):
        return "Urban", ("Urban",)
    if normalized.startswith("/infantil-next/"):
        return "Next", ("Next", "Meninos")
    if normalized.startswith("/outlet/"):
        return "Outlet", ("Outlet",)
    return "Aramis", ("Aramis",)


async def persist_to_db(
    brand_key: str,
    outcome: CategoryValidationOutcome,
    *,
    replace_existing: bool = False,
) -> None:
    """Persiste as categorias oferecidas (offered) na tabela brand_category_mappings.

    Usa upsert (ON CONFLICT DO UPDATE) por (brand_id, canonical_slug), portanto é
    idempotente: pode ser chamado múltiplas vezes sem duplicar linhas.

    Por padrão, categorias que sumiram do site não são apagadas. Quando
    ``replace_existing`` é verdadeiro, remove apenas registros ausentes de uma
    fotografia que o engine marcou como completa e sem bloqueios. Assim o job
    diário acompanha mudanças reais sem apagar dados por falha transitória.
    """
    from core.db import db

    if not outcome.offered:
        logger.info("[%s] Nenhuma categoria oferecida para persistir no banco.", brand_key)
        return

    # Busca o brand_id numérico (FK da tabela brands)
    brand_rows = await db.query_raw(
        "SELECT id FROM brands WHERE brand_key = $1 LIMIT 1",
        brand_key.lower(),
    )
    if not brand_rows:
        logger.warning(
            "[%s] Marca não encontrada na tabela brands — não é possível persistir categorias.",
            brand_key,
        )
        return

    brand_id = brand_rows[0]["id"]
    existing_rows = await db.query_raw(
        """
        SELECT id, canonical_slug, source_path
        FROM brand_category_mappings
        WHERE brand_id = $1
        """,
        brand_id,
    )
    existing_by_path = {
        _normalize_source_path(row.get("source_path") or row.get("canonical_slug")): row
        for row in existing_rows
        if _normalize_source_path(row.get("source_path") or row.get("canonical_slug"))
    }
    upserted = 0
    errors = 0
    offered_paths: set[str] = set()

    for cat in outcome.offered:
        slug = (cat.path or cat.url or cat.name or "").strip().rstrip("/").lower()
        if not slug:
            continue
        source_path = cat.path or cat.url or None
        label = cat.name or None
        normalized_path = _normalize_source_path(source_path or slug)
        offered_paths.add(normalized_path)

        try:
            existing = existing_by_path.get(normalized_path)
            if existing:
                await db.execute_raw(
                    """
                    UPDATE brand_category_mappings
                    SET source_path = $1, label = $2, updated_at = NOW()
                    WHERE id = $3
                    """,
                    source_path,
                    label,
                    existing["id"],
                )
            else:
                await db.execute_raw(
                    """
                    INSERT INTO brand_category_mappings
                        (brand_id, canonical_slug, source_path, label, created_at, updated_at)
                    VALUES
                        ($1, $2, $3, $4, NOW(), NOW())
                    ON CONFLICT (brand_id, canonical_slug)
                    DO UPDATE SET
                        source_path = EXCLUDED.source_path,
                        label       = EXCLUDED.label,
                        updated_at  = NOW()
                    """,
                    brand_id,
                    slug,
                    source_path,
                    label,
                )
            upserted += 1
        except Exception as exc:
            errors += 1
            logger.debug("[%s] Falha ao upsert categoria '%s': %s", brand_key, slug, exc)

    if replace_existing and offered_paths:
        stale_ids = [
            row["id"]
            for path, row in existing_by_path.items()
            if path not in offered_paths
        ]
        for stale_id in stale_ids:
            await db.execute_raw(
                "DELETE FROM brand_category_mappings WHERE id = $1",
                stale_id,
            )

    logger.info(
        "[%s] Persistência concluída: %d categorias upsertadas, %d erros.",
        brand_key,
        upserted,
        errors,
    )


async def load_from_db(brand_key: str) -> Optional[CategoryValidationOutcome]:
    """Lê brand_category_mappings do banco e reconstrói um CategoryValidationOutcome.

    Retorna None se não houver nenhuma linha para a marca (banco vazio / primeira vez),
    permitindo que o caller decida fazer um crawl ao vivo como fallback.

    As categorias retornadas são marcadas com ProbeStatus.HAS_PRODUCTS porque
    foram validadas por um crawl anterior — a prova de conteúdo já foi feita.
    """
    from core.db import db
    from services.category_discovery import (
        DiscoveredCategory,
        CategoryProbeResult,
        ProbeStatus,
    )

    rows = await db.query_raw(
        """
        SELECT bcm.canonical_slug, bcm.source_path, bcm.label
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = $1
        ORDER BY bcm.id ASC
        """,
        brand_key.lower(),
    )

    if not rows:
        return None

    offered = []
    probes: dict = {}
    seen_paths: set[str] = set()
    for row in rows:
        slug = canonicalize_wake_category_source(brand_key, row["canonical_slug"] or "")
        source = canonicalize_wake_category_source(
            brand_key, row["source_path"] or slug
        )
        parsed_source = urlsplit(source)
        path = parsed_source.path if parsed_source.scheme and parsed_source.netloc else source
        path = "/" + path.strip("/") if path.strip("/") else ""
        url = source if parsed_source.scheme and parsed_source.netloc else ""
        name = row["label"] or path or slug
        group, ancestors = _persisted_group(brand_key, path)

        # Legacy and discovered records can coexist in the durable table. Once
        # normalized they represent one source collection, so do not offer it
        # twice to the category picker.
        identity = (path or url or slug).rstrip("/").casefold()
        if identity and identity in seen_paths:
            continue
        if identity:
            seen_paths.add(identity)

        cat = DiscoveredCategory(
            name=name,
            path=path,
            url=url,
            group=group,
            ancestors=ancestors,
        )
        offered.append(cat)
        # Marca como has_products — prova de conteúdo já foi feita em crawl anterior
        probes[cat.identity] = CategoryProbeResult(
            ProbeStatus.HAS_PRODUCTS, detail="loaded_from_db"
        )

    logger.info("[%s] %d categorias carregadas do banco.", brand_key, len(offered))
    return CategoryValidationOutcome(offered=offered, probes=probes)
