"""Serviço de espelho local / armazenamento de banners.
Versão V2 desacoplada de nuvens proprietárias.
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger("banner_blob_mirror")

class BannerBlobMirrorService:
    """Implementação local limpa sem dependência de Azure Blob."""
    def __init__(self):
        self.enabled = False

    def schedule(self, run_id: str) -> None:
        logger.debug("Banner run %s mantido no armazenamento local.", run_id)

    async def submit_retry(self):
        return {"status": "ok", "message": "Armazenamento local em uso."}

banner_blob_mirror_service = BannerBlobMirrorService()
