"""Persistência e workers reproduzíveis da busca comparativa/SKU (D-08).

Refatorado para usar `JsonJobRepository` (Plan 07-02) como fonte única de
verdade em `search_history.json`: nenhuma cópia mutável em memória é mantida
entre chamadas, então o serviço nunca pode divergir do disco (D-03). Todo
input necessário para reproduzir a busca é persistido antes de qualquer I/O
externo, e cada worker (`run_search_job`/`run_cross_search_job`) transiciona
PENDING -> RUNNING -> terminal sob CAS, deixando resultado ausente até o
job realmente terminar.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from core.job_manager import job_manager
from core.models import (
    ComparisonResult,
    JobExecutionRecord,
    JobExecutionStatus,
    SearchHistory,
)
from services.job_repository import JsonJobRepository

logger = logging.getLogger("SearchHistoryService")
STORAGE_FILE = Path(__file__).resolve().parents[1] / "data" / "search_history.json"

_TERMINAL_STATUSES = {
    JobExecutionStatus.COMPLETED,
    JobExecutionStatus.FAILED,
    JobExecutionStatus.INTERRUPTED,
}
_RESUMABLE_STATUSES = {JobExecutionStatus.PENDING, JobExecutionStatus.RUNNING}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_valid_uuid(val: Any) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _fire_and_forget(coro: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    except RuntimeError:
        if hasattr(coro, "close"):
            coro.close()


async def _persist_job_to_db_async(
    job_id: str,
    query: str,
    brands: List[str],
    search_type: str = "search",
    target_sku: Optional[str] = None,
    status: str = "PENDING",
    result_payload: Optional[Any] = None,
    error_message: Optional[str] = None,
    created_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
) -> None:
    if not _is_valid_uuid(job_id):
        return

    from core.db import db, ensure_db_connected
    from prisma import Json

    try:
        await asyncio.wait_for(ensure_db_connected(), timeout=1.5)
    except Exception as e:
        logger.debug("DB indisponível para busca: %s", e)
        return

    now = datetime.now(timezone.utc)
    c_at = created_at or now
    u_at = updated_at or c_at

    try:
        existing = await db.search_jobs.find_unique(where={"job_id": job_id})
        if existing:
            update_data: Dict[str, Any] = {
                "status": status.upper(),
                "updated_at": u_at,
            }
            if result_payload is not None:
                update_data["result_payload"] = Json(result_payload)
            if error_message is not None:
                update_data["error_message"] = error_message
            await db.search_jobs.update(where={"job_id": job_id}, data=update_data)
        else:
            brands_data = [
                {"source_key": str(b), "search_order": idx}
                for idx, b in enumerate(brands)
            ]
            create_data: Dict[str, Any] = {
                "job_id": job_id,
                "query": query,
                "target_sku": target_sku,
                "search_type": search_type,
                "status": status.upper(),
                "created_at": c_at,
                "updated_at": u_at,
                "error_message": error_message,
            }
            if result_payload is not None:
                create_data["result_payload"] = Json(result_payload)
            if brands_data:
                create_data["search_job_brands"] = {"create": brands_data}

            await db.search_jobs.create(data=create_data)
    except Exception as e:
        logger.warning("Falha ao salvar search_job no DB (%s): %s", job_id, e)


async def _delete_job_from_db_async(job_id: str) -> None:
    if not _is_valid_uuid(job_id):
        return
    from core.db import db, ensure_db_connected
    try:
        await asyncio.wait_for(ensure_db_connected(), timeout=1.5)
    except Exception:
        return
    try:
        await db.search_jobs.delete(where={"job_id": job_id})
    except Exception as e:
        logger.debug("Falha ao deletar search_job %s do DB: %s", job_id, e)


async def sync_search_history_json_to_db(path: Optional[Path] = None) -> int:
    """Carrega registros de search_history.json para search_jobs no PostgreSQL."""
    p = path or STORAGE_FILE
    if not p.exists():
        return 0
    from core.db import db, ensure_db_connected
    from prisma import Json

    try:
        await asyncio.wait_for(ensure_db_connected(), timeout=1.5)
    except Exception as e:
        logger.warning("DB não conectado para sincronizar histórico: %s", e)
        return 0

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return 0

    if not isinstance(raw, list):
        return 0

    synced = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        job_id = item.get("job_id")
        if not job_id or not _is_valid_uuid(job_id):
            continue

        try:
            existing = await db.search_jobs.find_unique(where={"job_id": job_id})
            if existing:
                continue

            input_data = item.get("input") or {}
            query = str(input_data.get("query") or item.get("query") or "")
            target_sku = input_data.get("target_sku") or item.get("target_sku")
            brands = list(input_data.get("brands") or item.get("brands") or [])
            search_type = str(item.get("kind") or item.get("type") or "search")
            status = str(item.get("status") or "completed").upper()
            result_payload = item.get("result") or item.get("results")
            error_message = item.get("error")

            def parse_dt(v):
                if not v:
                    return None
                try:
                    return datetime.fromisoformat(v)
                except Exception:
                    return None

            c_at = parse_dt(item.get("created_at")) or datetime.now(timezone.utc)
            u_at = parse_dt(item.get("updated_at")) or c_at

            brands_data = [
                {"source_key": str(b), "search_order": idx}
                for idx, b in enumerate(brands)
            ]
            create_data = {
                "job_id": job_id,
                "query": query,
                "target_sku": target_sku,
                "search_type": search_type,
                "status": status,
                "created_at": c_at,
                "updated_at": u_at,
                "error_message": error_message,
            }
            if result_payload is not None:
                create_data["result_payload"] = Json(result_payload)
            if brands_data:
                create_data["search_job_brands"] = {"create": brands_data}

            await db.search_jobs.create(data=create_data)
            synced += 1
        except Exception as e:
            logger.warning("Erro ao sincronizar job %s: %s", job_id, e)

    if synced:
        logger.info("Sincronizados %d jobs de search_history.json para o banco.", synced)
    return synced


def _atomic_write_json(path: Path, payload: Any) -> None:
    import time
    """Mesmo padrão de publicação atômica de `job_repository.py`: temp no
    mesmo diretório + `os.replace`, usado apenas pela migração one-shot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.1)
                    continue
                raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _legacy_status_to_execution(value: Any) -> str:
    normalized = str(value or "pending").strip().lower()
    valid = {status.value for status in JobExecutionStatus}
    return normalized if normalized in valid else "interrupted"


def _legacy_record_to_execution_payload(job_id: str, legacy: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(legacy.get("type") or legacy.get("kind") or "search")
    created_at = legacy.get("created_at") or _utc_now_iso()
    status = _legacy_status_to_execution(legacy.get("status"))
    return {
        "job_id": job_id,
        "kind": kind,
        "input": {
            "query": legacy.get("query"),
            "type": kind,
            "brands": list(legacy.get("brands") or []),
            "target_sku": legacy.get("target_sku"),
        },
        "status": status,
        "created_at": created_at,
        "updated_at": legacy.get("updated_at") or created_at,
        "completed_at": legacy.get("completed_at")
        or (created_at if status in {"completed", "failed"} else None),
        "result": legacy.get("results"),
        "error": legacy.get("error"),
        # Consumido por JobManager.reconcile (D-05/D-06); estável por job_id.
        "logical_cycle_id": job_id,
    }


def _migrate_legacy_document_if_needed(path: Path) -> None:
    """Upgrade one-shot do formato legado (dict chaveado por job_id) para a
    lista attempt-aware que `JsonJobRepository` espera. Idempotente: uma vez
    migrado, o arquivo já é uma lista e esta função não faz nada."""
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Não foi possível ler %s para migração de formato.", path)
        return
    if not isinstance(raw, dict):
        return

    migrated: List[Dict[str, Any]] = []
    for job_id, legacy in raw.items():
        if not isinstance(legacy, dict):
            continue
        try:
            record = JobExecutionRecord.model_validate(
                _legacy_record_to_execution_payload(job_id, legacy)
            )
        except Exception:
            logger.exception("Registro legado inválido descartado: %s", job_id)
            continue
        migrated.append(record.model_dump(mode="json", exclude_none=False))

    _atomic_write_json(path, migrated)
    logger.info(
        "Migrados %d registros legados de %s para o formato de job durável.",
        len(migrated),
        path,
    )


def _execution_status_to_legacy(status: JobExecutionStatus) -> str:
    return status.value.upper()


def _to_search_history(record: JobExecutionRecord) -> SearchHistory:
    input_data = dict(record.input or {})
    payload = {
        "job_id": record.job_id,
        "query": input_data.get("query") or "",
        "target_sku": input_data.get("target_sku"),
        "type": record.kind,
        "brands": list(input_data.get("brands") or []),
        "status": _execution_status_to_legacy(record.status),
        "kind": record.kind,
        "input": input_data,
        "attempt": record.attempt,
        "progress": record.progress,
        "attempts": record.attempts,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "results": record.result,
        "error": record.error,
    }
    return SearchHistory.model_validate(payload)


_migrate_legacy_document_if_needed(STORAGE_FILE)
_default_repository = JsonJobRepository(STORAGE_FILE)


class SearchHistoryService:
    """Adapter da busca em background sobre o repositório durável comum.

    Não guarda cache mutável: toda leitura/escrita atravessa o
    `JsonJobRepository` injetado, então duas instâncias apontando para o
    mesmo arquivo nunca divergem entre si nem do disco.
    """

    def __init__(self, repository: Optional[JsonJobRepository] = None):
        self._repository = repository or _default_repository

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------
    def create_job(
        self,
        job_id: str,
        query: str,
        brands: List[str],
        type: str = "search",
        target_sku: Optional[str] = None,
        **extra_input: Any,
    ) -> SearchHistory:
        """Persiste o input completo e reproduzível ANTES de qualquer
        lookup/scraping (D-08, Pitfall 3): marcas, limite, ordenação, CEP,
        frete e, para SKU, query estrita/ampla e limiar."""
        input_payload: Dict[str, Any] = {
            "query": query,
            "type": type,
            "brands": list(brands or []),
            "target_sku": target_sku,
        }
        for key, value in extra_input.items():
            # A ausência da chave preserva jobs legados (default 10), enquanto
            # `max_per_brand: null` é a intenção explícita de buscar Tudo.
            if value is not None or key == "max_per_brand":
                input_payload[key] = value

        record = JobExecutionRecord.model_validate(
            {
                "job_id": job_id,
                "kind": type,
                "input": input_payload,
                "status": JobExecutionStatus.PENDING,
                # job_id já é uma identidade estável e única por execução —
                # serve diretamente como logical_cycle_id (D-05/D-06/D-07).
                "logical_cycle_id": job_id,
            }
        )
        saved = self._repository.add(record)
        _fire_and_forget(
            _persist_job_to_db_async(
                job_id=job_id,
                query=query,
                brands=brands,
                search_type=type,
                target_sku=target_sku,
                status="PENDING",
            )
        )
        return _to_search_history(saved)

    def update_job(
        self,
        job_id: str,
        status: str,
        results: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> Optional[SearchHistory]:
        """Setter legado mantido por compatibilidade; workers novos usam
        `_repository.transition` diretamente para CAS explícito."""
        current = self._repository.get(job_id)
        if current is None:
            return None
        target = _legacy_status_to_execution(status)
        patch: Dict[str, Any] = {}
        if results is not None:
            patch["result"] = results
        if error is not None:
            patch["error"] = error
        updated = self._repository.transition(
            job_id, current.attempt, {current.status}, target, patch
        )
        if updated is None:
            return None
        _fire_and_forget(
            _persist_job_to_db_async(
                job_id=job_id,
                query=dict(updated.input or {}).get("query") or "",
                brands=list(dict(updated.input or {}).get("brands") or []),
                search_type=updated.kind,
                target_sku=dict(updated.input or {}).get("target_sku"),
                status=status,
                result_payload=results,
                error_message=error,
                updated_at=updated.updated_at,
            )
        )
        return _to_search_history(updated)

    def get_job(self, job_id: str) -> Optional[SearchHistory]:
        record = self._repository.get(job_id)
        return _to_search_history(record) if record else None

    def list_jobs(self) -> List[SearchHistory]:
        jobs = [_to_search_history(record) for record in self._repository.list_jobs()]
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return jobs

    def delete_job(self, job_id: str) -> bool:
        # job_repository.py (Plan 07-02) não expõe delete público — reutiliza
        # a mesma transação lock+replace já validada em vez de duplicar o
        # protocolo de atomicidade num segundo arquivo.
        with self._repository._transaction():
            records = self._repository._read_unlocked()
            index = next(
                (i for i, record in enumerate(records) if record.job_id == job_id),
                None,
            )
            if index is None:
                return False
            del records[index]
            self._repository._write_unlocked(records)
            _fire_and_forget(_delete_job_from_db_async(job_id))
            return True

    def cleanup_old_records(self) -> int:
        """Retém apenas terminais com mais de 30 dias (Pitfall 5): nunca
        remove PENDING/RUNNING antes da reconciliação de boot."""
        now = datetime.now(timezone.utc)
        removed = 0
        with self._repository._transaction():
            records = self._repository._read_unlocked()
            keep = []
            for record in records:
                if (
                    record.status in _TERMINAL_STATUSES
                    and record.completed_at is not None
                    and (now - record.completed_at) > timedelta(days=30)
                ):
                    removed += 1
                    continue
                keep.append(record)
            if removed:
                self._repository._write_unlocked(keep)
        if removed:
            logger.info("Cleanup removeu %d registros antigos.", removed)
        return removed

    def load_history(self) -> None:
        """Mantido por compatibilidade de nome; dispara apenas o cleanup —
        não há mais cache em memória para (re)carregar."""
        self.cleanup_old_records()

    # ------------------------------------------------------------------
    # Workers reproduzíveis (D-08) — registrados como resume factories
    # ------------------------------------------------------------------
    async def run_search_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        """Worker da busca comparativa. Idempotente por design: recebe o
        payload persistido (nunca defaults novos) e pode ser chamado de novo
        pela reconciliação de boot sem duplicar side effects observáveis."""
        from api.routes_search import _normalize_brand_results_for_response
        from services.engines.factory import engine_factory
        from services.notification_service import notification_store

        record = self._repository.get(job_id)
        if record is None or record.status not in _RESUMABLE_STATUSES:
            return

        query = str(payload.get("query") or "")
        brands = list(payload.get("brands") or [])

        if record.status == JobExecutionStatus.PENDING:
            running = self._repository.transition(
                job_id,
                record.attempt,
                {JobExecutionStatus.PENDING},
                JobExecutionStatus.RUNNING,
                {"progress": {"stage": "searching", "completed_units": 0}},
            )
            if running is None:
                return
            await _persist_job_to_db_async(
                job_id=job_id,
                query=query,
                brands=brands,
                search_type="search",
                status="RUNNING",
                updated_at=running.updated_at,
            )
        else:
            running = record

        try:
            brand_results = await engine_factory.search_all_brands(
                query=query,
                brands=brands or None,
                max_per_brand=payload.get("max_per_brand", 10),
                sort=payload.get("sort", "relevance"),
                zipcode=payload.get("zipcode"),
                include_shipping=bool(payload.get("include_shipping", False)),
            )
            brand_results = _normalize_brand_results_for_response(brand_results)
            from core.pricing import calculate_price_averages
            all_products = [p for b in brand_results for p in b.products]
            global_avg = calculate_price_averages(all_products)
            result = ComparisonResult(
                query=query,
                brands_searched=brands,
                results=brand_results,
                average_price=global_avg["average_price"],
                average_price_original=global_avg["average_price_original"],
                brand_averages=global_avg["brand_averages"],
            )
            result_payload = result.model_dump(mode="json")["results"]
        except Exception as exc:
            logger.exception("Erro no worker de busca comparativa %s", job_id)
            self._repository.transition(
                job_id,
                running.attempt,
                {JobExecutionStatus.RUNNING},
                JobExecutionStatus.FAILED,
                {"error": str(exc)},
            )
            await _persist_job_to_db_async(
                job_id=job_id,
                query=query,
                brands=brands,
                search_type="search",
                status="FAILED",
                error_message=str(exc),
            )
            notification_store.add_notification(
                title="Erro na Busca",
                message=f"A busca por '{query}' falhou devido a um erro.",
                tab="search",
                route_kind="search",
                route_job_id=job_id,
            )
            return

        completed = self._repository.transition(
            job_id,
            running.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
            {
                "result": result_payload,
                "progress": {
                    "stage": "done",
                    "completed_units": len(result_payload),
                },
            },
        )
        if completed is None:
            return
        await _persist_job_to_db_async(
            job_id=job_id,
            query=query,
            brands=brands,
            search_type="search",
            status="COMPLETED",
            result_payload=result_payload,
            updated_at=completed.updated_at,
        )
        notification_store.add_notification(
            title="Busca Concluída",
            message=f"Busca por '{query}' finalizada.",
            tab="search",
            route_kind="search",
            route_job_id=job_id,
        )

    async def run_cross_search_job(self, job_id: str, payload: Dict[str, Any]) -> None:
        """Worker da busca por SKU. Move para cá, inclusive, o lookup VTEX
        inicial de referência — nenhum I/O externo roda antes do 202 (D-08)."""
        from services.cross_marketplace_service import cross_marketplace_service
        from services.nlp_service import nlp_service
        from services.notification_service import notification_store
        from services.vtex_api_scraper import VtexApiClient

        record = self._repository.get(job_id)
        if record is None or record.status not in _RESUMABLE_STATUSES:
            return

        target_sku = payload.get("target_sku")
        strict_q = payload.get("search_query")
        broad_q = payload.get("broad_query")
        min_score = payload.get("min_score", 55.0)
        zipcode = payload.get("zipcode")
        # Marca de origem do SKU (configurada pelo usuário, padrão retrocompatível: aramis)
        ref_brand_key = (payload.get("brand_key") or "aramis").lower().strip()
        ref_product_data: Optional[Dict[str, Any]] = None

        if record.status == JobExecutionStatus.PENDING:
            running = self._repository.transition(
                job_id,
                record.attempt,
                {JobExecutionStatus.PENDING},
                JobExecutionStatus.RUNNING,
                {"progress": {"stage": "resolving_reference", "completed_units": 0}},
            )
            if running is None:
                return
            await _persist_job_to_db_async(
                job_id=job_id,
                query=f"SKU: {target_sku}",
                brands=["mercadolivre", "netshoes", "amazon"],
                search_type="cross",
                target_sku=target_sku,
                status="RUNNING",
                updated_at=running.updated_at,
            )
        else:
            running = record

        # Obtém o nome de exibição da marca de referência
        from services.brand_service import brand_service as _brand_service
        ref_brand = _brand_service.get_brand(ref_brand_key)
        ref_brand_name = (
            getattr(ref_brand, "brand_name", None) or ref_brand_key.capitalize()
        )

        try:
            if not strict_q:
                scraper = VtexApiClient(brand_name=ref_brand_name)
                ref_search_result = await scraper.search(query=target_sku, max_results=1)

                if not ref_search_result.products:
                    logger.warning(
                        "SKU '%s' não encontrado em '%s'. Usando o SKU como "
                        "fallback para a busca nos marketplaces.",
                        target_sku,
                        ref_brand_key,
                    )
                    strict_q = target_sku
                    broad_q = target_sku
                else:
                    ref_product = ref_search_result.products[0]
                    ref_product_data = {
                        "name": ref_product.product_name,
                        "url": ref_product.url,
                        "image_url": getattr(ref_product, "image_url", ""),
                        "price": ref_product.price_full,
                    }
                    # A query estrita se torna o nome completo do produto (Sniper)
                    strict_q = ref_product.product_name

                    # Query ampla enxuta: Categoria (mapeada) + primeiros tokens +
                    # nome da marca, evitando queries grandes rejeitadas (PREC-01).
                    category_synonyms = {
                        "tênis": "tênis",
                        "sapatenis": "tênis",
                        "camisa": "camisa",
                        "t-shirt": "camiseta",
                        "polo": "polo",
                    }
                    cat_lower = ref_product.category.lower() if ref_product.category else ""
                    mapped_cat = category_synonyms.get(cat_lower, ref_product.category)
                    tokens = strict_q.split()[:4]
                    if mapped_cat and mapped_cat.lower() not in strict_q.lower():
                        broad_q = f"{mapped_cat} {' '.join(tokens)} {ref_brand_name}"
                    else:
                        broad_q = f"{' '.join(tokens)} {ref_brand_name}"
            else:
                broad_q = broad_q or strict_q

            # Adiciona o nome da marca ao termo de busca para os marketplaces
            # (se já não estiver presente).
            brand_token = ref_brand_name.lower()
            if broad_q and brand_token not in broad_q.lower():
                broad_q = f"{broad_q} {ref_brand_name}"
            if strict_q and brand_token not in strict_q.lower():
                strict_q = f"{strict_q} {ref_brand_name}"

            strict_q_no_color = nlp_service.remove_colors(strict_q) if strict_q else strict_q

            result = await cross_marketplace_service.compare_product(
                broad_query=broad_q,
                strict_query=strict_q_no_color,
                target_sku=target_sku,
                min_score=min_score,
                zipcode=zipcode,
                brand_key=ref_brand_key,
            )
            result["reference_product"] = ref_product_data
            result["job_id"] = job_id
        except Exception as exc:
            logger.exception("Erro no worker de busca por SKU %s", job_id)
            self._repository.transition(
                job_id,
                running.attempt,
                {JobExecutionStatus.RUNNING},
                JobExecutionStatus.FAILED,
                {"error": str(exc)},
            )
            await _persist_job_to_db_async(
                job_id=job_id,
                query=f"SKU: {target_sku}",
                brands=["mercadolivre", "netshoes", "amazon"],
                search_type="cross",
                target_sku=target_sku,
                status="FAILED",
                error_message=str(exc),
            )
            notification_store.add_notification(
                title="Erro na Busca por SKU",
                message=f"A busca do SKU '{target_sku}' falhou devido a um erro.",
                tab="cross",
                route_kind="cross",
                route_job_id=job_id,
            )
            return

        completed = self._repository.transition(
            job_id,
            running.attempt,
            {JobExecutionStatus.RUNNING},
            JobExecutionStatus.COMPLETED,
            {"result": result, "progress": {"stage": "done", "completed_units": 1}},
        )
        if completed is None:
            return
        await _persist_job_to_db_async(
            job_id=job_id,
            query=f"SKU: {target_sku}",
            brands=["mercadolivre", "netshoes", "amazon"],
            search_type="cross",
            target_sku=target_sku,
            status="COMPLETED",
            result_payload=result,
            updated_at=completed.updated_at,
        )
        notification_store.add_notification(
            title="Busca por SKU Concluída",
            message=f"Busca do SKU '{target_sku}' finalizada.",
            tab="cross",
            route_kind="cross",
            route_job_id=job_id,
        )


search_history_service = SearchHistoryService()

# Resume factories (D-05/D-06): boot reconciliation (fora do escopo deste
# plano) resolve por `kind` e retoma exatamente com o input persistido.
job_manager.register_resume_factory("search", search_history_service.run_search_job)
job_manager.register_resume_factory("cross", search_history_service.run_cross_search_job)
