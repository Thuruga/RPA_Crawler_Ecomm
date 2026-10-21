from fastapi import APIRouter, HTTPException
from typing import List

from core.models import SearchHistory
from services.search_history_service import search_history_service

router = APIRouter(prefix="/history", tags=["history"])


class SearchHistorySnapshot(SearchHistory):
    """Snapshot durável de um job de busca (D-08): mesmo contrato de
    `SearchHistory`, mais a dica de polling consumida pelo cliente."""

    poll_after_ms: int = 1500


@router.get("", response_model=List[SearchHistory])
async def list_history():
    """Lista o histórico de pesquisas recentes."""
    return search_history_service.list_jobs()

@router.get("/{job_id}", response_model=SearchHistorySnapshot)
async def get_history(job_id: str):
    """Obtém o snapshot canônico de uma busca — progresso, timestamps e
    resultado somente quando disponível (status terminal)."""
    job = search_history_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Histórico não encontrado")
    return SearchHistorySnapshot(**job.model_dump(mode="json"))

@router.delete("/{job_id}", status_code=204)
async def delete_history(job_id: str):
    """Exclui uma pesquisa do histórico."""
    success = search_history_service.delete_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Histórico não encontrado")
    return None
