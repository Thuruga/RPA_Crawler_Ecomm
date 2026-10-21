import asyncio
import json
import os
import logging
import inspect
from contextlib import suppress
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from pydantic import ValidationError

from core.job_manager import JobManager, job_manager as default_job_manager
from core.models import (
    ConditionalOfferEvent,
    ConditionalOfferResult,
    ConditionalOfferSnapshot,
    PriceCheckRun,
    PriceCheckState,
    PriceHistoryEntry,
    PriceMonitorConfig,
    PriceMonitorStatus,
    condition_signature_key,
    resolve_effective_price,
    resolve_original_price,
    scopes_compatible,
)
from core.websocket import manager
from services.notification_service import notification_store
from services.engines.factory import engine_factory


logger = logging.getLogger("PriceMonitorService")

# Cheap backoff so a tick that observed a busy/foreign claim (rather than one
# it may act on) does not hot-loop while waiting for the owner to finish and
# advance the durable cursor (D-01/D-02, JOB-01/JOB-02).
_CLAIM_RETRY_BACKOFF_SECONDS = 3
_DEFERRED_TICK_RESULTS = {"observing", "already_claimed", "already_terminal", "missing_monitor"}

CONDITIONAL_OFFER_HISTORY_LIMIT = 200
CHECK_RUNS_HISTORY_LIMIT = 200
_CONCLUSIVE_OFFER_STATUSES = {"found", "none_detected"}
_OFFER_STATE_FIELDS = {"state", "observed_from", "observed_until"}
_OFFER_NON_SEMANTIC_FIELDS = {"source", "provenance", "valid_from", "valid_until"}


# Caminho para persistência local dos monitores
STORAGE_FILE = Path(__file__).resolve().parents[1] / "data" / "price_monitors.json"


def _offer_snapshot(value: Any, *, checked_at: str | None = None) -> ConditionalOfferSnapshot:
    if isinstance(value, ConditionalOfferSnapshot):
        snapshot = value.model_copy(deep=True)
    elif hasattr(value, "model_dump"):
        snapshot = ConditionalOfferSnapshot.model_validate(value.model_dump(mode="json"))
    else:
        snapshot = ConditionalOfferSnapshot.model_validate(value)
    if not snapshot.observed_from and checked_at:
        snapshot.observed_from = checked_at
    return snapshot


def _semantic_offer_payload(offer: ConditionalOfferSnapshot) -> dict[str, Any]:
    payload = offer.model_dump(mode="json", exclude_none=True)
    for field in _OFFER_STATE_FIELDS | _OFFER_NON_SEMANTIC_FIELDS:
        payload.pop(field, None)
    return payload


def _snapshot_claim_key(offer: ConditionalOfferSnapshot) -> str | None:
    """Return a correlatable claim key for complete persisted offer snapshots.

    Uses `condition_signature_key` (condition only, no scope) rather than
    `commercial_claim_key` (condition + exact scope) so that two cycles
    observing complementary-but-partial scope for the same commercial claim
    (e.g. cycle 1 sees a full-scope confirmed offer, cycle 2 only re-derives
    checkout evidence with sku_id/seller_id) still land in the same
    candidate bucket; the scope check that decides an actual correlation
    happens afterwards, per-candidate, via `scopes_compatible`.
    """
    if offer.condition_type is None or offer.eligibility is None:
        return None
    return condition_signature_key(offer)


def diff_conditional_offers(
    before: Iterable[Any],
    after: Iterable[Any],
    *,
    collection_status: str,
    checked_at: str,
) -> tuple[list[ConditionalOfferSnapshot] | list[Any], list[ConditionalOfferEvent]]:
    """Diff active offers without expiring them on inconclusive collection outcomes."""
    status = getattr(collection_status, "value", collection_status)
    before_values = list(before)
    if status not in _CONCLUSIVE_OFFER_STATUSES:
        return before_values, []

    prior = [_offer_snapshot(item) for item in before_values]
    incoming = [_offer_snapshot(item, checked_at=checked_at) for item in after]
    prior_by_key = {item.offer_key: item for item in prior}
    prior_claims: dict[str, list[ConditionalOfferSnapshot]] = {}
    for item in prior:
        claim_key = _snapshot_claim_key(item)
        if claim_key is not None:
            prior_claims.setdefault(claim_key, []).append(item)

    # A legacy persisted key may include evidence identity. Once a conclusive
    # observation proves the same commercial claim, retain that key for the
    # existing timeline instead of manufacturing ended + started events.
    for offer in incoming:
        if offer.offer_key in prior_by_key:
            continue
        claim_key = _snapshot_claim_key(offer)
        candidates = prior_claims.get(claim_key or "", [])
        # Same condition is necessary but not sufficient: only correlate when
        # the incoming offer's scope is compatible (never known-divergent)
        # with the candidate's scope, so a real cross-cycle claim match
        # (e.g. checkout-only sku_id/seller_id evidence reconfirming a
        # previously full-scope offer) still resolves to exactly one
        # candidate, while a genuinely different product (different SKU)
        # never correlates.
        compatible_candidates = [
            candidate
            for candidate in candidates
            if scopes_compatible(
                getattr(offer, "scope", offer), getattr(candidate, "scope", candidate)
            )
        ]
        if len(compatible_candidates) == 1:
            offer.offer_key = compatible_candidates[0].offer_key

    incoming_by_key = {item.offer_key: item for item in incoming}
    current: list[ConditionalOfferSnapshot] = []
    events: list[ConditionalOfferEvent] = []

    for offer in incoming:
        previous = prior_by_key.get(offer.offer_key)
        if previous is None:
            current.append(offer)
            events.append(ConditionalOfferEvent(
                event_type="started", offer_key=offer.offer_key, after=offer,
                observed_from=offer.observed_from, created_at=checked_at,
            ))
            continue
        offer.observed_from = previous.observed_from or offer.observed_from or checked_at
        current.append(offer)
        if _semantic_offer_payload(previous) != _semantic_offer_payload(offer):
            event_type = "changed"
        elif previous.state != offer.state:
            event_type = "state_changed"
        else:
            continue
        events.append(ConditionalOfferEvent(
            event_type=event_type, offer_key=offer.offer_key,
            before=previous, after=offer, observed_from=offer.observed_from,
            created_at=checked_at,
        ))

    for previous in prior:
        if previous.offer_key in incoming_by_key:
            continue
        expired = previous.model_copy(deep=True)
        expired.state = "expired"
        expired.observed_until = checked_at
        events.append(ConditionalOfferEvent(
            event_type="ended", offer_key=previous.offer_key,
            before=previous, after=expired,
            observed_from=previous.observed_from, observed_until=checked_at,
            created_at=checked_at,
        ))
    return current, events


def atomic_write_json(path: Path, data: Any) -> None:
    import time
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
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
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _parse_iso_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _advance_due_at(due_at: datetime, interval_minutes: int) -> datetime:
    """Advance `due_at` one interval, skipping whole slots already in the past.

    The cursor stays anchored to the monitor's own slot grid -- every result is
    `due_at` plus a whole number of intervals -- so `logical_cycle_id` remains
    deterministic and the claim keys stay unique. What it does not do is hand
    back a slot that is already overdue.

    Without the skip, a monitor that sat idle (server down, paused, laptop
    asleep) replays every slot it missed: one live scrape per missed slot,
    one `check_runs` entry per missed slot. A 10-minute monitor idle for
    eight weeks came back with an ~8200-slot backlog and ground through it
    at roughly one request per second -- saturating the event loop, inflating
    the ledger, and drawing anti-bot blocks until the process wedged.
    """
    interval = timedelta(minutes=max(1, interval_minutes))
    next_due = due_at + interval
    now = datetime.now(timezone.utc)
    if next_due <= now:
        # Jump straight to the first slot on the grid that is still ahead.
        missed_slots = (now - next_due) // interval + 1
        next_due += interval * missed_slots
    return next_due


def load_monitors(path: Path = STORAGE_FILE) -> list[PriceMonitorConfig]:
    import time
    path = Path(path)
    if not path.exists():
        return []
    raw = None
    for attempt in range(5):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError:
            if attempt < 4:
                time.sleep(0.1)
                continue
            raise
    rows = raw.values() if isinstance(raw, dict) else raw
    loaded = []
    for row in rows:
        payload = dict(row)
        payload.setdefault("job_id", payload.pop("id", None))
        payload.setdefault("brand", "unknown")
        loaded.append(PriceMonitorConfig.model_validate(payload))
    return loaded


def save_monitors(path: Path, monitors: Iterable[PriceMonitorConfig]) -> None:
    rows = []
    for monitor_config in monitors:
        monitor_config.conditional_offer_history = monitor_config.conditional_offer_history[
            -CONDITIONAL_OFFER_HISTORY_LIMIT:
        ]
        rows.append(monitor_config.model_dump(mode="json"))
    atomic_write_json(Path(path), rows)


def apply_conditional_offer_result(
    config: PriceMonitorConfig,
    result: ConditionalOfferResult,
) -> list[ConditionalOfferEvent]:
    current, events = diff_conditional_offers(
        config.conditional_offers_current,
        result.offers,
        collection_status=result.status.value,
        checked_at=result.checked_at,
    )
    config.conditional_offers_current = [
        _offer_snapshot(item) for item in current
    ]
    config.conditional_offers_status = result.status.value
    config.conditional_offers_checked_at = result.checked_at
    config.conditional_offers_diagnostics = list(result.diagnostics)
    if events:
        config.conditional_offer_history.extend(events)
        config.conditional_offer_history = config.conditional_offer_history[
            -CONDITIONAL_OFFER_HISTORY_LIMIT:
        ]
    return events


async def collect_conditional_offers_for_monitor(
    config: PriceMonitorConfig,
    *,
    trigger: str,
) -> ConditionalOfferResult:
    """Use the shared cached promotion runtime through its internal monitor triggers."""
    from api.routes_promotions import promotion_orchestrator
    from services.brand_service import brand_service

    brand = brand_service.get_brand(config.brand)
    if brand is None or not bool(getattr(brand, "is_active", False)):
        return ConditionalOfferResult(
            status="unsupported",
            probe_level="full",
            diagnostics=["conditional offers unavailable for inactive or unknown brand"],
        )
    return await promotion_orchestrator(
        brand=brand,
        brand_key=config.brand.casefold(),
        url=config.url,
        trigger=trigger,
        level="full",
    )


async def refresh_conditional_offers_for_monitor(
    config: PriceMonitorConfig,
    *,
    job_id: str,
) -> list[ConditionalOfferEvent]:
    trigger = (
        "monitor_started"
        if config.conditional_offers_checked_at is None
        else "monitor_cycle"
    )
    try:
        result = await collect_conditional_offers_for_monitor(config, trigger=trigger)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Monitor %s (%s): falha isolada ao consultar ofertas â€” %s",
            job_id,
            config.brand,
            exc,
        )
        result = ConditionalOfferResult(
            status="error",
            probe_level="full",
            diagnostics=[f"monitor offer collection failed: {type(exc).__name__}"],
        )
    events = apply_conditional_offer_result(config, result)
    for event in events:
        await emit_conditional_offer_event(
            event,
            websocket=manager.send_message,
            notifier=notification_store.add_notification,
            job_id=job_id,
        )
    return events


async def emit_conditional_offer_event(
    offer_or_event: Any,
    event_type: str | None = None,
    *,
    websocket: Any,
    notifier: Any,
    job_id: str | None = None,
) -> None:
    """Emit an offer lifecycle event on channels distinct from public price updates."""
    if isinstance(offer_or_event, ConditionalOfferEvent):
        event = offer_or_event
        offer = event.after or event.before
        normalized_type = event.event_type.value
    else:
        offer = _offer_snapshot(offer_or_event)
        normalized_type = str(event_type or "changed")
        event = ConditionalOfferEvent(
            event_type=normalized_type,
            offer_key=offer.offer_key,
            after=offer,
            observed_from=offer.observed_from,
        )
    titles = {
        "started": "Nova Oferta Condicional",
        "changed": "Oferta Condicional Alterada",
        "ended": "Oferta Condicional Encerrada",
        "state_changed": (
            "Oferta Condicional Confirmada"
            if offer and offer.state.value == "confirmed"
            else "Oferta Condicional Alterada"
        ),
    }
    condition_label = offer.condition_label if offer else "Oferta condicional"
    payload = {
        "type": "conditional_offer_update",
        "event_type": normalized_type,
        "offer_key": event.offer_key,
        "condition_label": condition_label,
        "before": event.before.model_dump(mode="json") if event.before else None,
        "after": event.after.model_dump(mode="json") if event.after else None,
        "observed_from": event.observed_from,
        "observed_until": event.observed_until,
    }
    sent = websocket(payload, job_id) if job_id is not None else websocket(payload)
    if inspect.isawaitable(sent):
        await sent
    notified = notifier(
        title=titles[normalized_type],
        message=f"{condition_label}: {titles[normalized_type].lower()}.",
        tab="monitor",
    )
    if inspect.isawaitable(notified):
        await notified



class PriceMonitorService:
    def __init__(self):
        self.monitors: Dict[str, PriceMonitorConfig] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        # Não chamamos _load_monitors aqui para evitar loop de importação se models mudar,
        # chamaremos explicitamente na inicialização do app.

    async def load_monitors_async(self):
        from core.db import db
        try:
            records = await db.price_monitors.find_many()
            for record in records:
                history_records = await db.price_history.find_many(
                    where={"monitor_id": record.id},
                    order={"observed_at": "desc"},
                    take=100
                )
                brand_record = await db.brands.find_first(where={"id": record.brand_id})
                brand_name = brand_record.brand_key if brand_record else record.source_key
                
                history_list = []
                for hr in reversed(history_records):
                    history_list.append({
                        "timestamp": hr.observed_at.isoformat() if hr.observed_at else None,
                        "price": float(hr.price) if hr.price else 0.0,
                        "price_original": float(hr.price_original) if hr.price_original else None,
                        "last_price_discount": float(hr.last_price_discount) if hr.last_price_discount else None,
                        "available": hr.available,
                        "available_colors": hr.available_colors,
                        "available_sizes": hr.available_sizes
                    })
                
                co_current = record.conditional_offers_current
                co_diag = record.conditional_offers_diagnostics
                co_hist = record.conditional_offer_history
                
                payload = {
                    "job_id": record.job_id,
                    "brand": brand_name,
                    "url": record.product_url,
                    "interval_minutes": record.interval_minutes,
                    "duration_hours": record.duration_hours,
                    "start_time": record.started_at.isoformat() if record.started_at else None,
                    "active": record.status == "active",
                    "status": PriceMonitorStatus.ACTIVE if record.status == "active" else PriceMonitorStatus.PAUSED,
                    "last_price": float(record.last_price) if record.last_price is not None else None,
                    "last_price_original": float(record.last_price_original) if record.last_price_original is not None else None,
                    "last_price_discount": float(record.last_price_discount) if record.last_price_discount is not None else None,
                    "last_status": record.last_check_state,
                    "last_error": record.last_error,
                    "last_checked_at": record.last_checked_at.isoformat() if record.last_checked_at else None,
                    "product_name": record.product_name,
                    "image_url": record.image_url,
                    "available_colors": record.available_colors,
                    "available_sizes": record.available_sizes,
                    "history": history_list,
                    "conditional_offers_current": json.loads(co_current) if isinstance(co_current, str) else (co_current or []),
                    "conditional_offers_status": record.conditional_offers_status,
                    "conditional_offers_checked_at": record.conditional_offers_checked_at.isoformat() if record.conditional_offers_checked_at else None,
                    "conditional_offers_diagnostics": json.loads(co_diag) if isinstance(co_diag, str) else (co_diag or []),
                    "conditional_offer_history": json.loads(co_hist) if isinstance(co_hist, str) else (co_hist or []),
                }
                
                config = PriceMonitorConfig.model_validate(payload)
                if config.next_check_due_at is None:
                    config.next_check_due_at = _parse_iso_datetime(config.start_time)
                self.monitors[record.job_id] = config
        except Exception as e:
            logger.error(f"Erro ao carregar monitores do BD: {e}")

    def _ensure_monitor_loop(self, job_id: str) -> None:
        """Create the single background task owning `job_id`, if not already running.

        An orphaned `active_cycle_id` (D-05/D-06) is reclaimed and finished
        first via `_resume_then_loop`, which only falls through to the normal
        recurring `_monitor_loop` once that resume's terminal CAS transition
        clears the pointer — a normal tick never races the resume for the
        same logical cycle (JOB-02).
        """
        if job_id in self.tasks and not self.tasks[job_id].done():
            return
        config = self.monitors.get(job_id)
        if config is None:
            return
        if config.active_cycle_id is not None:
            self.tasks[job_id] = asyncio.create_task(self._resume_then_loop(job_id))
        else:
            self.tasks[job_id] = asyncio.create_task(self._monitor_loop(job_id))

    def start_loaded_monitor_loops(self) -> None:
        """Start one background task per hydrated ACTIVE monitor (D-01/D-02).

        Must run only after boot-time reconciliation for the other job kinds
        (search/cross, category_scan) has completed, so `app.py`'s lifespan
        never lets a monitor tick race the shared reconciliation pass. D-02/
        D-05/D-06: rehydrates strictly off `status` (`PriceMonitorStatus.ACTIVE`),
        never off the legacy `active` bool — a monitor stopped via the new
        field but still carrying a stale `active: true` from an older write
        must not be recreated.
        """
        for job_id, config in self.monitors.items():
            if config.status != PriceMonitorStatus.ACTIVE:
                continue
            self._ensure_monitor_loop(job_id)


    async def _save_monitors_async(self):
        from core.db import db
        for job_id, config in self.monitors.items():
            try:
                brand_record = await db.brands.find_unique(where={"brand_key": config.brand.lower()})
                if not brand_record:
                    brand_record = await db.brands.create(
                        data={
                            "brand_key": config.brand.lower(),
                            "brand_name": config.brand.title(),
                            "domain": f"{config.brand.lower()}.com.br",
                        }
                    )
                
                def date_or_none(dt_str):
                    if not dt_str: return None
                    from datetime import datetime, timezone
                    parsed = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                    
                co_history = getattr(config, "conditional_offer_history", [])
                
                monitor_data = {
                    "brand_id": brand_record.id,
                    "source_key": config.brand.lower(),
                    "product_url": config.url,
                    "normalized_url": config.url,
                    "status": config.status.value,
                    "interval_minutes": config.interval_minutes,
                    "duration_hours": config.duration_hours,
                    "started_at": date_or_none(config.start_time) or datetime.utcnow(),
                    "last_price": config.last_price,
                    "last_price_original": config.last_price_original,
                    "last_price_discount": config.last_price_discount,
                    "last_check_state": config.last_status,
                    "last_error": config.last_error,
                    "last_checked_at": date_or_none(config.last_checked_at),
                    "product_name": config.product_name,
                    "image_url": config.image_url,
                    "available_colors": config.available_colors,
                    "available_sizes": config.available_sizes,
                    "conditional_offers_current": json.dumps([o.model_dump(mode="json") for o in config.conditional_offers_current]),
                    "conditional_offers_status": config.conditional_offers_status,
                    "conditional_offers_checked_at": date_or_none(config.conditional_offers_checked_at),
                    "conditional_offers_diagnostics": json.dumps(config.conditional_offers_diagnostics),
                    "conditional_offer_history": json.dumps([o.model_dump(mode="json") if hasattr(o, "model_dump") else dict(o) for o in co_history]),
                }
                
                existing = await db.price_monitors.find_unique(where={"job_id": job_id})
                if existing:
                    await db.price_monitors.update(where={"job_id": job_id}, data=monitor_data)
                else:
                    monitor_data["job_id"] = job_id
                    await db.price_monitors.create(data=monitor_data)
            except Exception as e:
                logger.error(f"Erro ao salvar monitor {job_id} no BD: {e}")



    async def start_monitor(self, job_id: str, url: str, brand: str, interval: int, duration: int):
        from services.url_utils import normalize_url
        norm_url = normalize_url(url)
        for existing_id, config in self.monitors.items():
            if normalize_url(config.url) == norm_url and config.brand.lower() == brand.lower():
                if config.active:
                    return config, "already_active"
                else:
                    await self.resume_monitor(existing_id)
                    return self.monitors[existing_id], "reactivated"

        config = PriceMonitorConfig(
            job_id=job_id,
            url=url,
            brand=brand,
            interval_minutes=interval,
            duration_hours=duration,
            active=True,
            status=PriceMonitorStatus.ACTIVE,
        )
        # Seed the durable cursor at start_time for the first cycle (D-01/D-02).
        config.next_check_due_at = _parse_iso_datetime(config.start_time)
        self.monitors[job_id] = config
        await self._save_monitors_async()

        # Inicia a task de background
        task = asyncio.create_task(self._monitor_loop(job_id))
        self.tasks[job_id] = task
        return config, "created"

    async def stop_monitor(self, job_id: str):
        task = self.tasks.pop(job_id, None)
        if task is not None and not task.done():
            task.cancel()
            # Await cleanup (JOB-02): the cancelled cycle must finish writing
            # INTERRUPTED + clearing active_cycle_id before we persist
            # PAUSED below, or a concurrent full-dict save could race it.
            with suppress(asyncio.CancelledError):
                await task

        if job_id in self.monitors:
            self.monitors[job_id].active = False
            self.monitors[job_id].status = PriceMonitorStatus.PAUSED
            await self._save_monitors_async()

    async def delete_monitor(self, job_id: str):
        # Para se estiver rodando
        await self.stop_monitor(job_id)

        # Remove dos registros
        if job_id in self.monitors:
            del self.monitors[job_id]
            await self._save_monitors_async()
            
        from core.db import db
        try:
            await db.price_monitors.delete(where={"job_id": job_id})
        except Exception as e:
            logger.error(f"Erro ao remover monitor {job_id} do BD: {e}")

    async def delete_monitors_by_brand(self, brand_key: str):
        """Para e remove todos os monitores associados a uma marca."""
        to_delete = [job_id for job_id, config in self.monitors.items() if config.brand.lower() == brand_key.lower()]
        for job_id in to_delete:
            await self.delete_monitor(job_id)

    async def resume_monitor(self, job_id: str):
        if job_id in self.monitors:
            config = self.monitors[job_id]
            config.active = True
            config.status = PriceMonitorStatus.ACTIVE
            self._ensure_monitor_loop(job_id)
            await self._save_monitors_async()

    async def _execute_price_check(
        self, job_id: str, config: PriceMonitorConfig
    ) -> tuple[PriceCheckState, Optional[str]]:
        """Run one PDP read + conditional-offer refresh, returning its terminal state.

        Never raises except `asyncio.CancelledError`, which it re-raises
        after propagating (the caller finalizes the run as INTERRUPTED).
        """
        failure_reason = None
        offers_checked_this_cycle = False
        try:
            config.last_checked_at = datetime.now(timezone.utc).isoformat()
            # Resolve o motor dinamicamente via factory
            engine = engine_factory.get_engine(config.brand)
            # get_pdp_product retorna o PRODUTO COMPLETO (compatível com
            # RawProductBronze). Para VTEX/Shopify/SFCC/Wake/Zara delega para
            # get_product_details; para os marketplaces (ML/Amazon/Netshoes)
            # usa o parser de PDP dedicado — get_product_details desses engines
            # devolve só {"seller": ...} (fluxo cross-marketplace) e quebraria
            # a validação.
            product_data = await engine.get_pdp_product(config.url)

            # _marketplace_read_state/next_retry_at vêm de
            # ResilientMarketplaceEngine._dict_call (Fase 8, 08-01) só para os
            # 3 marketplaces (ML/Amazon/Netshoes); ausentes (None) para marcas
            # próprias, que não passam pelo wrapper — comportamento idêntico
            # ao de hoje nesse caso. O carimbo de next_retry_at acontece em
            # TODA leitura que devolva dict, inclusive nas degradadas.
            marketplace_read_state = (
                product_data.get("_marketplace_read_state")
                if isinstance(product_data, dict) else None
            )
            if isinstance(product_data, dict):
                config.next_retry_at = product_data.get("next_retry_at")

            # Converte para objeto se vier como dict (para facilitar acesso).
            # A imagem não é obrigatória para resolver preço no monitor: removemos
            # image_url se vier ausente/inválida para não derrubar a validação
            # (RawProductBronze.image_url_must_be_present rejeitaria None/"").
            from core.models import RawProductBronze
            product = None
            # Guard crítico de MKT-03: "stale" é um dict COMPLETO e válido
            # reaproveitado do cache do coordinator — sem este guard ele
            # passaria despercebido por RawProductBronze.model_validate e
            # seria tratado como sucesso novo (observação de preço fantasma).
            # "blocked"/"unsupported" já falhariam a validação abaixo (o dict
            # de erro só tem 3 chaves, sem raw_title/price_full); mantê-los
            # aqui também é redundante mas intencional — robusto a mudanças
            # futuras no shape do dict de erro.
            if product_data and marketplace_read_state not in {"stale", "blocked", "unsupported", "network_error"}:
                payload = dict(product_data)
                img = payload.get("image_url")
                if not img or not str(img).strip() or str(img) == "None":
                    payload.pop("image_url", None)
                try:
                    product = RawProductBronze.model_validate(payload)
                except ValidationError as exc:
                    # Payload incompleto/ inválido: registra o motivo (visível nos
                    # logs do servidor, não só no WebSocket) e segue para o próximo
                    # ciclo. Sem isso o monitor ficava "Pendente" sem qualquer pista.
                    logger.warning(
                        "Monitor %s (%s): produto inválido em %s — %s",
                        job_id, config.brand, config.url, exc,
                    )
                    failure_reason = "Dados do produto incompletos ou inválidos."
                    await manager.send_message(
                        {"type": "error", "message": "Não foi possível extrair os dados do produto."},
                        job_id,
                    )
                    product = None
            elif marketplace_read_state == "stale":
                # Leitura externa indisponível, mas o coordinator reaproveitou
                # o último payload de sucesso do cache — o preço já resolvido
                # anteriormente permanece intacto em config.last_price* (D-01).
                failure_reason = (
                    "Leitura externa indisponível no momento; o último preço "
                    "conhecido foi preservado."
                )
            elif marketplace_read_state == "unsupported":
                failure_reason = "Esta leitura não é suportada pelo marketplace."
            elif marketplace_read_state == "network_error":
                failure_reason = (
                    product_data.get("error_message")
                    if isinstance(product_data, dict) and product_data.get("error_message")
                    else "Falha temporária de rede ou DNS ao acessar o site."
                )
            elif marketplace_read_state == "blocked":
                failure_reason = (
                    "Não foi possível ler o produto — o site pode estar bloqueando "
                    "automação (anti-bot)."
                )
            else:
                failure_reason = (
                    product_data.get("error_message")
                    if isinstance(product_data, dict) and product_data.get("error_message")
                    else "Falha ao obter dados do produto na página."
                )

            # A linha temporal de ofertas Ã© independente da comparaÃ§Ã£o de preÃ§o
            # público. Toda rodada consulta o runtime, cujo cache impede repetir
            # probes caros; apenas a primeira rodada permite confirmaÃ§Ã£o automÃ¡tica.
            await refresh_conditional_offers_for_monitor(config, job_id=job_id)
            offers_checked_this_cycle = True

            if product:
                config.last_status = PriceCheckState.OK.value
                config.last_error = None
                config.last_success_at = config.last_checked_at

                current_price = resolve_effective_price(
                    product.price_full,
                    product.price_discount,
                    product.price_discount_is_delta,
                )
                current_original_price = resolve_original_price(
                    product.price_full,
                    product.price_discount,
                    product.price_discount_is_delta,
                )
                if (
                    current_original_price is not None
                    and current_price is not None
                    and current_original_price <= current_price
                ):
                    current_original_price = None
                # price_discount e um DELTA (valor positivo do desconto), nao um
                # preco final — convencao dos engines VTEX/ML/Shopify
                # (list_price - sale_price). Normaliza 0/None para None.
                current_discount = (
                    current_original_price - current_price
                    if current_original_price is not None and current_price is not None
                    else None
                )
                available = product.stock_availability

                # Atualiza metadados (imagem e nome) na config se ainda não tiver ou se a atual for inválida/placeholder
                def _is_invalid_img(u: Optional[str]) -> bool:
                    if not u or not str(u).strip() or str(u) == "None":
                        return True
                    u_str = str(u).lower()
                    return u_str.endswith(".svg") or "update-brands" in u_str or "placeholder" in u_str or "empty-basket" in u_str

                if product.image_url:
                    if not config.image_url or _is_invalid_img(config.image_url):
                        config.image_url = product.image_url
                    elif not _is_invalid_img(product.image_url) and product.image_url != config.image_url:
                        config.image_url = product.image_url

                if product.raw_title and (not config.product_name or config.product_name.upper() in {"PRIVALIA", "PRODUTO"}):
                    config.product_name = product.raw_title

                current_colors = product.available_colors or []
                current_sizes = product.available_sizes or []

                # Verifica se houve mudança de preço, disponibilidade, cores ou tamanhos
                has_change = False
                old_price = config.last_price

                if config.last_price is None or config.last_price != current_price:
                    has_change = True

                if config.last_price_original != current_original_price:
                    has_change = True

                # Mudanca apenas no desconto (price_full inalterado) tambem conta
                # como mudanca — sem isso, uma promocao (D-01) era ignorada em
                # silencio quando o preco efetivo nao mudava.
                if config.last_price_discount != current_discount:
                    has_change = True

                # Atualiza numerações/cores disponíveis silenciosamente (não força hit no histórico)
                if sorted(config.available_colors) != sorted(current_colors):
                    config.available_colors = current_colors

                if sorted(config.available_sizes) != sorted(current_sizes):
                    config.available_sizes = current_sizes

                # Registra no histórico se houve mudança
                if has_change:
                    entry = PriceHistoryEntry(
                        price=current_price,
                        price_original=current_original_price,
                        last_price_discount=current_discount,
                        available=bool(available),
                        available_colors=current_colors,
                        available_sizes=current_sizes,

                    )
                    config.history.append(entry)
                    config.last_price = current_price
                    config.last_price_original = current_original_price
                    config.last_price_discount = current_discount

                    # Notifica o frontend via WebSocket
                    await manager.send_message({
                        "type": "price_update",
                        "price": current_price,
                        "price_full": current_original_price or current_price,
                        "price_discount": current_discount,
                        "price_original": current_original_price,
                        "available": available,
                        "available_colors": current_colors,
                        "available_sizes": current_sizes,
                        "history": [e.model_dump() for e in config.history],

                        "message": f"Mudança detectada! Preço: R$ {current_price:.2f} | Tamanhos: {len(current_sizes)}"
                    }, job_id)

                    # Mudanças no preço original/desconto ainda atualizam o
                    # histórico e o WebSocket, mas não são uma alteração do
                    # preço efetivo exibido ao usuário.
                    if old_price is not None and old_price != current_price:
                        display_name = config.product_name or job_id[:8]
                        # Limita o tamanho do nome do produto para nao estourar o layout
                        if len(display_name) > 40:
                            display_name = display_name[:37] + "..."
                        
                        notification_store.add_notification(
                            title="Alteração de Preço",
                            message=(
                                f"{config.brand}: {display_name} mudou de R$ {old_price:.2f} "
                                f"para R$ {current_price:.2f}"
                            ),
                            tab="monitor",
                            route_kind="monitor",
                            route_monitor_id=config.job_id,
                        )
                    else:
                        logger.info(f"Monitor {job_id} [{config.url}]: Primeira leitura concluida. R$ {current_price:.2f}")
                else:
                    logger.info(f"Monitor {job_id} [{config.url}]: Sem mudancas.")

                notification_store.set_last_price_run()
                await manager.send_message({
                        "type": "info",

                        "message": f"Checagem realizada às {datetime.now().strftime('%H:%M:%S')}. Sem alterações."
                    }, job_id)
                return PriceCheckState.OK, None
            else:
                # Falha ao resolver o produto. Marca o estado para o front exibir
                # algo claro (Stale/Bloqueado/Erro) em vez de "Pendente" eterno.
                # "unsupported" mapeia para ERROR (Assumption A1 do RESEARCH):
                # PriceCheckState não tem um quinto valor UNSUPPORTED.
                if marketplace_read_state == "stale":
                    terminal_state = PriceCheckState.STALE
                elif marketplace_read_state in ("unsupported", "network_error"):
                    terminal_state = PriceCheckState.ERROR
                elif marketplace_read_state == "blocked":
                    terminal_state = PriceCheckState.BLOCKED
                else:
                    terminal_state = PriceCheckState.ERROR
                config.last_status = terminal_state.value
                config.last_error = failure_reason or "Falha ao acessar o produto."
                await manager.send_message({"type": "error", "message": config.last_error}, job_id)
                return terminal_state, config.last_error

        except asyncio.CancelledError:
            # Cancelamento (stop/delete) deve propagar — nunca virar "erro do monitor".
            raise
        except Exception as e:
            # Loga com traceback no servidor (antes só ia para o WebSocket, invisível
            # sem cliente conectado — o que escondia a causa do "Pendente" eterno).
            logger.exception("Erro inesperado no monitor %s (%s): %s", job_id, config.brand, e)
            config.last_status = PriceCheckState.ERROR.value
            config.last_error = f"Erro: {str(e)[:200]}"
            await manager.send_message({"type": "error", "message": f"Erro no monitor: {e}"}, job_id)
            if not offers_checked_this_cycle:
                await refresh_conditional_offers_for_monitor(config, job_id=job_id)
            return PriceCheckState.ERROR, config.last_error

    async def _run_claimed_price_cycle(
        self,
        job_id: str,
        config: PriceMonitorConfig,
        run: PriceCheckRun,
        claim: Any,
    ) -> str:
        """Execute an already-claimed run to its terminal state (D-02).

        Only this terminal transition clears `active_cycle_id` and advances
        `next_check_due_at` — from the run's own `scheduled_for` + the
        monitor's `interval_minutes`, never from wall-clock "now".
        """
        due_at = run.scheduled_for
        cancelled: asyncio.CancelledError | None = None
        try:
            run.started_at = run.started_at or datetime.now(timezone.utc)
            state, error = await self._execute_price_check(job_id, config)
        except asyncio.CancelledError as exc:
            cancelled = exc
            state, error = PriceCheckState.INTERRUPTED, "cancelled"

        try:
            run.status = state
            run.completed_at = datetime.now(timezone.utc)
            run.error = error
            # WR-04: do not clobber config.last_status/last_error on interruption.
            # `deriveIndividualLifecycle` (frontend) has no mapping for
            # 'interrupted' and silently falls back to "Pendente", losing the
            # user's last known real status (ok/stale/blocked/error) the moment
            # a monitor is stopped mid-cycle. The interruption itself is still
            # recorded faithfully on `run.status`/`check_runs` above; only the
            # monitor's terminal-status summary is left untouched here.
            config.active_cycle_id = None
            config.next_check_due_at = _advance_due_at(
                due_at, config.interval_minutes
            )
            await self._save_monitors_async()
        finally:
            await claim.release()

        if cancelled is not None:
            raise cancelled
        return state.value

    async def run_price_cycle(
        self, job_id: str, *, manager: Optional["JobManager"] = None
    ) -> str:
        """Claim `price_check:{job_id}:{next_check_due_at}` before any I/O (D-01/D-02).

        A monitor with an in-flight `active_cycle_id` (own tick already
        running, or an orphan awaiting `resume_price_cycle`) is observed,
        never claimed here — only one caller ever advances a given cycle.
        """
        job_manager_instance = manager or default_job_manager
        config = self.monitors.get(job_id)
        if config is None:
            return "missing_monitor"
        if config.active_cycle_id is not None:
            return "observing"

        due_at = config.next_check_due_at or _parse_iso_datetime(config.start_time)
        config.next_check_due_at = due_at
        logical_cycle_id = f"price_check:{job_id}:{due_at.isoformat()}"

        if any(
            existing.logical_cycle_id == logical_cycle_id and existing.status is not None
            for existing in config.check_runs
        ):
            return "already_terminal"

        claim = await job_manager_instance.try_claim(logical_cycle_id)
        if claim is None:
            return "already_claimed"

        run = next(
            (item for item in config.check_runs if item.logical_cycle_id == logical_cycle_id),
            None,
        )
        if run is None:
            run = PriceCheckRun(logical_cycle_id=logical_cycle_id, scheduled_for=due_at)
            config.check_runs.append(run)
            # Bounded ledger: keep the newest runs and drop the tail, so a
            # long-lived monitor cannot grow its history without limit (the
            # detail view reads this same list).
            if len(config.check_runs) > CHECK_RUNS_HISTORY_LIMIT:
                config.check_runs = config.check_runs[-CHECK_RUNS_HISTORY_LIMIT:]
        config.active_cycle_id = logical_cycle_id
        try:
            await self._save_monitors_async()
        except Exception:
            config.active_cycle_id = None
            with suppress(Exception):
                await claim.release()
            raise

        return await self._run_claimed_price_cycle(job_id, config, run, claim)

    async def resume_price_cycle(
        self, job_id: str, *, manager: Optional["JobManager"] = None
    ) -> str:
        """Reclaim an orphaned in-flight cycle surviving a prior crash (D-05/D-06).

        Unlike `run_price_cycle`, this targets the monitor's persisted
        `active_cycle_id` directly instead of deriving a fresh due_at — the
        orphaned attempt is finished under its original identity, never
        silently replaced by a new one.
        """
        job_manager_instance = manager or default_job_manager
        config = self.monitors.get(job_id)
        if config is None or config.active_cycle_id is None:
            return "nothing_to_resume"

        logical_cycle_id = config.active_cycle_id
        run = next(
            (item for item in config.check_runs if item.logical_cycle_id == logical_cycle_id),
            None,
        )
        if run is None or run.status is not None:
            # Nothing orphaned (already terminal, or the pointer predates any
            # persisted run) — clear the stale pointer so the normal tick is
            # not blocked forever waiting for a resume that will never come.
            config.active_cycle_id = None
            await self._save_monitors_async()
            return "nothing_to_resume"

        claim = await job_manager_instance.try_claim(logical_cycle_id)
        if claim is None:
            return "already_claimed"

        return await self._run_claimed_price_cycle(job_id, config, run, claim)

    async def _resume_then_loop(self, job_id: str) -> None:
        """Boot task for an orphaned monitor: finish the interrupted cycle,
        then fall through to the normal recurring loop if still ACTIVE."""
        try:
            await self.resume_price_cycle(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Falha ao retomar ciclo interrompido do monitor %s", job_id)
        config = self.monitors.get(job_id)
        if config is not None and config.status == PriceMonitorStatus.ACTIVE:
            await self._monitor_loop(job_id)

    async def _monitor_loop(self, job_id: str):
        config = self.monitors.get(job_id)
        if not config:
            return
        if config.next_check_due_at is None:
            config.next_check_due_at = _parse_iso_datetime(config.start_time)
            await self._save_monitors_async()

        while True:
            config = self.monitors.get(job_id)
            if config is None or config.status != PriceMonitorStatus.ACTIVE:
                break

            due_at = config.next_check_due_at or _parse_iso_datetime(config.start_time)
            wait_seconds = (due_at - datetime.now(timezone.utc)).total_seconds()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            # Re-read after waking: pause/stop/delete may have landed while asleep.
            config = self.monitors.get(job_id)
            if config is None or config.status != PriceMonitorStatus.ACTIVE:
                break

            result = await self.run_price_cycle(job_id)
            if result in _DEFERRED_TICK_RESULTS:
                # Another owner holds this exact cycle (e.g. a resume task) —
                # back off briefly rather than hot-looping; the cursor only
                # advances once that owner's terminal transition clears it.
                await asyncio.sleep(_CLAIM_RETRY_BACKOFF_SECONDS)
            else:
                logger.info(f"Monitor {job_id} concluído ({result}). Próxima checagem agendada.")

        # Fim do monitoramento manual (status != ACTIVE)
        await self._save_monitors_async()
        await manager.send_message({"type": "done", "message": "Monitoramento pausado/desativado."}, job_id)

# Singleton
monitor_service = PriceMonitorService()


async def _resume_price_check_job(job_id: str, _persisted_input: Dict[str, Any]) -> Any:
    """Resume-from-zero factory registered with the JobManager (kind="price_check").

    Delegates to the singleton service's own orphan-resume path, reusing the
    persisted `active_cycle_id` rather than deriving a new one.
    """
    return await monitor_service.resume_price_cycle(job_id)


default_job_manager.register_resume_factory("price_check", _resume_price_check_job)
