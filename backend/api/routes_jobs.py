"""
Rotas de gerenciamento de Jobs.

DELETE /jobs/{job_id} — Cancela um job em andamento.
"""

from fastapi import APIRouter, HTTPException

from core.job_manager import JOB_CANCEL_FLAGS

router = APIRouter()


@router.delete("/jobs/{job_id}")
async def cancel_job(job_id: str):
    """Sinaliza ao orquestrador para interromper a extração em lote."""
    event = JOB_CANCEL_FLAGS.get(job_id)
    if event is None:
        raise HTTPException(
            status_code=404, detail="Job não encontrado ou já finalizado."
        )
    event.set()
    return {"job_id": job_id, "message": "Sinal de cancelamento enviado."}
