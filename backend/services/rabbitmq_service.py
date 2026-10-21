"""
RabbitMQ Service — Módulo core de mensageria (Phase 14).

Este módulo é o único ponto de contato com o broker RabbitMQ.
Nenhuma lógica de negócio aqui: apenas conexão, topologia DLQ/DLX,
publish e consumer loop.
"""

import asyncio
import json
import logging
from typing import Callable

import aio_pika
from aio_pika import DeliveryMode

from config import settings

logger = logging.getLogger("RabbitMQService")


class RabbitMQService:
    """Gerencia a conexão com o RabbitMQ e a topologia de filas.

    Singleton: use a instância `rabbitmq_service` exportada no final deste módulo.

    Topologia declarada em `connect()`:
      - DLX  (Dead-Letter Exchange): category_scans.dlx  — fanout, durable
      - DLQ  (Dead-Letter Queue):    category_scans.dlq  — durable, bound to DLX
      - Fila principal:              category_scans       — durable, x-dead-letter-exchange → DLX
    """

    def __init__(self) -> None:
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None
        self._status: str = "disconnected"

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Conecta ao broker e declara a topologia DLX → DLQ → fila principal.

        Em caso de falha: loga ERROR, seta status='error' e propaga a exceção
        para que o caller (lifespan de app.py) decida se interrompe o boot.
        """
        try:
            self._connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
            self._channel = await self._connection.channel()
            await self._channel.set_qos(prefetch_count=1)  # fair dispatch: 1 msg por vez

            # 1) Dead-Letter Exchange (fanout, durable)
            dlx = await self._channel.declare_exchange(
                settings.RABBITMQ_DLX,
                aio_pika.ExchangeType.FANOUT,
                durable=True,
            )

            # 2) Dead-Letter Queue (durable), bound ao DLX
            dlq = await self._channel.declare_queue(
                settings.RABBITMQ_DLQ,
                durable=True,
            )
            await dlq.bind(dlx)

            # 3) Fila principal com x-dead-letter-exchange apontando para o DLX
            self._queue = await self._channel.declare_queue(
                settings.RABBITMQ_QUEUE,
                durable=True,
                arguments={"x-dead-letter-exchange": settings.RABBITMQ_DLX},
            )

            self._status = "connected"
            logger.info(
                "RabbitMQ conectado. Fila: '%s', DLX: '%s', DLQ: '%s'.",
                settings.RABBITMQ_QUEUE,
                settings.RABBITMQ_DLX,
                settings.RABBITMQ_DLQ,
            )
        except Exception:
            self._status = "error"
            
            # Mask credentials before logging
            from urllib.parse import urlparse
            safe_url = settings.RABBITMQ_URL
            try:
                parsed = urlparse(settings.RABBITMQ_URL)
                if parsed.password:
                    safe_url = settings.RABBITMQ_URL.replace(parsed.password, "*****")
            except Exception:
                pass
                
            logger.exception("Falha ao conectar ao RabbitMQ (URL: %s).", safe_url)
            raise

    async def close(self) -> None:
        """Fecha a conexão com o broker de forma limpa."""
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()
        self._status = "disconnected"
        logger.info("Conexão RabbitMQ encerrada.")

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, message: dict) -> None:
        """Publica uma mensagem JSON na fila principal com DeliveryMode.PERSISTENT.

        Mensagens persistentes sobrevivem a reinicializações do broker.
        Em caso de falha: loga ERROR e propaga a exceção para que o caller
        (cron job ou endpoint HTTP) decida como reagir.

        Args:
            message: Dicionário a serializar como JSON UTF-8.
        """
        try:
            body = json.dumps(message, ensure_ascii=False).encode("utf-8")
            await self._channel.default_exchange.publish(
                aio_pika.Message(body=body, delivery_mode=DeliveryMode.PERSISTENT),
                routing_key=settings.RABBITMQ_QUEUE,
            )
            logger.debug("Mensagem publicada em '%s': %s", settings.RABBITMQ_QUEUE, message)
        except Exception:
            logger.exception(
                "Falha ao publicar mensagem em '%s': %s", settings.RABBITMQ_QUEUE, message
            )
            raise

    # ------------------------------------------------------------------
    # Consuming
    # ------------------------------------------------------------------

    async def start_consumer(self, callback: Callable) -> None:
        """Registra o callback como consumer da fila e bloqueia até CancelledError.

        IMPORTANTE: `queue.consume()` é NÃO-BLOQUEANTE — retorna imediatamente
        uma ConsumerTag. Para manter a task viva, usamos asyncio.Future() que
        nunca resolve naturalmente (apenas cancela via CancelledError no shutdown).

        Args:
            callback: Função async a chamar para cada IncomingMessage.
                      Assinatura: async def callback(message: aio_pika.IncomingMessage) -> None
        """
        await self._queue.consume(callback)
        logger.info("Consumer registrado na fila '%s'.", settings.RABBITMQ_QUEUE)
        try:
            await asyncio.Future()  # bloqueia indefinidamente até CancelledError
        except asyncio.CancelledError:
            pass  # shutdown normal — consumer tag é limpada quando o channel fecha

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def get_status(self) -> str:
        """Retorna o status atual da conexão: 'disconnected', 'connected' ou 'error'."""
        return self._status


# ---------------------------------------------------------------------------
# Singleton — use esta instância em toda a aplicação
# ---------------------------------------------------------------------------
rabbitmq_service = RabbitMQService()
