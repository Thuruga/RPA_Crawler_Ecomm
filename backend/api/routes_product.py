import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional

from core.models import PriceMonitorConfig
from services.price_monitor_service import monitor_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class MonitorStartRequest(BaseModel):
    url: str
    brand: str
    interval: int = 10  # minutos
    duration: int = 24  # horas


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/monitor/start")
async def start_monitoring(request: MonitorStartRequest):
    """Inicia o monitoramento de um produto."""
    job_id = str(uuid.uuid4())

    # Inicia o monitoramento em background
    try:
        config, status = await monitor_service.start_monitor(
            job_id=job_id,
            url=request.url,
            brand=request.brand,
            interval=request.interval,
            duration=request.duration
        )
        # Para already_active/reactivated o job_id existente é o da config já cadastrada;
        # para created usamos o uuid recém-gerado (que é o próprio config.job_id).
        return {"job_id": config.job_id, "status": status, "config": config.model_dump()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/monitor/stop/{job_id}")
async def stop_monitoring(job_id: str):
    """Interrompe um monitoramento ativo."""
    await monitor_service.stop_monitor(job_id)
    return {"status": "stopped"}


@router.post("/monitor/resume/{job_id}")
async def resume_monitoring(job_id: str):
    """Retoma um monitoramento pausado."""
    if job_id not in monitor_service.monitors:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await monitor_service.resume_monitor(job_id)
    return {"status": "resumed"}


@router.get("/monitor/history/{job_id}", response_model=PriceMonitorConfig)
async def get_monitor_history(job_id: str):
    """Retorna o histórico de um monitor específico.

    Tipado como PriceMonitorConfig (JOB-01): expõe check_runs integralmente
    -- cada PriceCheckRun com run_id/status/progresso/timestamps/erro
    sanitizado -- preservando OK/BLOCKED/ERROR/STALE/INTERRUPTED distintos,
    sem aplicar retenção implícita ao ledger.
    """
    if job_id not in monitor_service.monitors:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor_service.monitors[job_id]


@router.get("/monitors", response_model=List[PriceMonitorConfig])
async def list_monitors():
    """Lista todos os monitores (ativos e inativos).

    Tipado como List[PriceMonitorConfig] (JOB-01): mesma garantia de
    check_runs completo do detalhe acima, para a listagem.
    """
    return list(monitor_service.monitors.values())


@router.delete("/monitor/{job_id}")
async def delete_monitor(job_id: str):
    """Remove permanentemente um monitoramento."""
    await monitor_service.delete_monitor(job_id)
    return {"status": "deleted"}



