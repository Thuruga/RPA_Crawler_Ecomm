"""
Category Scheduler Jobs — Jobs assíncronos do APScheduler (Phase 14).

Mantém as definições dos cron jobs fora do app.py para preservar
a separação de responsabilidades: app.py orquestra o lifecycle,
este módulo contém a lógica dos jobs agendados.
"""

import logging

logger = logging.getLogger("CategorySchedulerJobs")


async def category_biweekly_enqueue_job(brand: str | None = None) -> None:
    """Publica uma mensagem RabbitMQ por monitor ativo a cada 15 dias.

    Para cada monitor com status ACTIVE (opcionalmente filtrado por brand), 
    publica um payload {"brand": "<brand>", "monitor_id": "<uuid>"} 
    na fila category_scans.

    Se o broker estiver indisponível, loga ERROR por monitor e
    continua para o próximo — não levanta exceção para não travar
    o APScheduler (decisão D-07).
    """
    from services.category_monitor_service import load_monitored_categories
    from services.rabbitmq_service import rabbitmq_service

    monitors = load_monitored_categories()
    if brand:
        monitors = [m for m in monitors if m.get("brand", "").lower() == brand.lower()]

    if not monitors:
        logger.info("category_biweekly_enqueue_job: nenhum monitor ativo encontrado%s.", f" para a marca {brand}" if brand else "")
        return

    enqueued = 0
    for monitor in monitors:
        try:
            await rabbitmq_service.publish({
                "brand": monitor["brand"],
                "monitor_id": monitor["id"],
            })
            enqueued += 1
        except Exception:
            logger.error(
                "Falha ao publicar mensagem para monitor %s (%s). "
                "Continuando para o próximo monitor.",
                monitor.get("id"),
                monitor.get("brand"),
                exc_info=True,
            )

    logger.info(
        "category_biweekly_enqueue_job: %d/%d mensagens enfileiradas.",
        enqueued,
        len(monitors),
    )
