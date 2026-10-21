"""Core — Modelos, WebSocket e Gerenciamento de Jobs."""

from core.models import RawProductBronze
from core.websocket import ConnectionManager
from core.job_manager import JOB_CANCEL_FLAGS

__all__ = ["RawProductBronze", "ConnectionManager", "JOB_CANCEL_FLAGS"]
