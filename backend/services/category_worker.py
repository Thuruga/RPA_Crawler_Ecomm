"""
Category Worker — Consumer callback do RabbitMQ (Phase 14).

Este módulo converte mensagens da fila `category_scans` em execuções
de scan de categoria. Ele NÃO conhece o broker (sem imports de
rabbitmq_service) — apenas recebe IncomingMessage via callback.

Padrão de ack/nack: MANUAL (sem context manager) para controle total
e evitar risco de double-ack no aio_pika (review fix HIGH).
"""

import json
import logging
from typing import TYPE_CHECKING

import aio_pika
from pydantic import BaseModel, ValidationError

from services.category_monitor_service import (
    CategoryScanOrigin,
    load_monitored_categories,
    run_category_scan,
)

logger = logging.getLogger("CategoryWorker")


# ---------------------------------------------------------------------------
# Schema de validação da mensagem RabbitMQ
# ---------------------------------------------------------------------------

class CategoryScanMessage(BaseModel):
    """Payload esperado em cada mensagem da fila category_scans."""

    brand: str
    monitor_id: str


# ---------------------------------------------------------------------------
# Consumer callback
# ---------------------------------------------------------------------------

async def process_category_scan_message(message: aio_pika.IncomingMessage) -> None:
    """Processa uma mensagem da fila category_scans.

    Fluxo:
      1. Parse + validação do payload JSON.
         - Inválido → nack(requeue=False) → DLQ.
      2. Lookup do monitor pelo monitor_id.
         - Não encontrado → ack() (dado obsoleto, não é falha do broker).
      3. Execução do scan via run_category_scan().
         - Qualquer resultado → ack() com log do resultado.
      4. Exceção inesperada no scan → nack(requeue=False) → DLQ.
         - Exceção NÃO propagada para não derrubar o consumer loop.

    Nota: ack/nack são chamados MANUALMENTE (sem async with message.process())
    para evitar risco de double-ack em aio_pika >= 9 (review fix HIGH).
    """
    # ------------------------------------------------------------------
    # Passo 1 — Parse e validação do payload
    # ------------------------------------------------------------------
    try:
        data = json.loads(message.body.decode("utf-8"))
        msg = CategoryScanMessage.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.error(
            "Mensagem inválida recebida (nack → DLQ). Body: %s | Erro: %s",
            message.body,
            exc,
        )
        await message.nack(requeue=False)
        return

    # ------------------------------------------------------------------
    # Passo 2 — Lookup do monitor
    # ------------------------------------------------------------------
    all_monitors = await load_monitored_categories()
    monitor = next(
        (m for m in all_monitors if m.get("id") == msg.monitor_id),
        None,
    )
    if monitor is None:
        logger.warning(
            "Monitor '%s' não encontrado (pode ter sido deletado após o enfileiramento). "
            "Mensagem descartada com ack.",
            msg.monitor_id,
        )
        await message.ack()
        return

    # ------------------------------------------------------------------
    # Passo 3 — Execução do scan
    # ------------------------------------------------------------------
    try:
        result = await run_category_scan(monitor, origin=CategoryScanOrigin.SCHEDULED)
        logger.info(
            "Scan concluído para monitor '%s' (brand: '%s'): %s",
            msg.monitor_id,
            msg.brand,
            result,
        )
        await message.ack()
    except Exception:
        # ------------------------------------------------------------------
        # Passo 4 — Exceção inesperada: nack → DLQ, sem propagar
        # ------------------------------------------------------------------
        logger.error(
            "Erro inesperado ao processar scan do monitor '%s'. "
            "Mensagem enviada para DLQ.",
            msg.monitor_id,
            exc_info=True,
        )
        await message.nack(requeue=False)
        # NÃO propagar — protege o consumer loop de travar
