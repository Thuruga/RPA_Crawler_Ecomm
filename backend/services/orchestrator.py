"""
Orquestrador de Extração em Lote.

Pipeline: Varredura de categoria → Extração paralela → Consolidação em Excel.
Suporta cancelamento gracioso com salvamento de dados parciais.
"""

import asyncio
from datetime import datetime, timezone
import logging
from typing import Optional, Callable

import pandas as pd

from services.product_contract import (
    build_canonical_export_dataframe,
    extract_category_product_item,
)

logger = logging.getLogger("Orchestrator")


def _product_dict(product):
    if isinstance(product, dict):
        return dict(product)
    if hasattr(product, "model_dump"):
        return product.model_dump(mode="json")
    if hasattr(product, "dict"):
        return product.dict()
    return dict(vars(product))


async def run_orchestrator(
    marca: str,
    url_categoria: str,
    log_callback: Optional[Callable] = None,
    cancel_event: Optional[asyncio.Event] = None,
    job_id: str | None = None,
    zipcode: str | None = None,
):
    started_at = datetime.now(timezone.utc)

    def emit_log(msg):
        if log_callback:
            if isinstance(msg, dict):
                log_callback(msg)
            else:
                log_callback({"type": "info", "message": str(msg)})
        else:
            logger.info(msg)

    def is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    # ── ETAPA 1: VARREDURA E EXTRAÇÃO PAGINADA ─────────────────────────────
    emit_log({
        "type": "category_url",
        "brand": marca,
        "url": url_categoria,
        "message": f"[{marca}] {url_categoria}",
    })

    from services.engines.factory import engine_factory
    
    engine = engine_factory.get_engine(marca)
    produtos_validos = []
    
    # Nome do módulo para arquivo de saída
    module_key = marca.lower().split()[0]
    arquivo_saida = f"dados_{module_key}_categoria.xlsx"

    try:
        async for produto in engine.run_bulk_scrape(
            category_url=url_categoria,
            log_callback=emit_log,
            cancel_event=cancel_event
        ):
            if zipcode:
                from services.category_shipping import enrich_category_shipping
                produto = await enrich_category_shipping(produto, marca, zipcode)
            produtos_validos.append(produto)

            prod_item = extract_category_product_item(produto, default_brand=marca)

            emit_log({
                "type": "category_product_item",
                "brand": marca,
                "product": prod_item,
            })

        if is_cancelled() and not produtos_validos:
            emit_log({"type": "cancelled", "message": "Operação cancelada pelo usuário durante a varredura."})
            return

        # ── ETAPA 2: CONSOLIDAÇÃO E SALVAMENTO ──────────────────────────────────
        emit_log("\n==================================================")
        emit_log(" ETAPA 2: CONSOLIDAÇÃO E SALVAMENTO")
        emit_log("==================================================")

        if job_id is not None:
            scan_id = f"{job_id}:{marca.lower()}"

        if produtos_validos:
            df = build_canonical_export_dataframe(
                [_product_dict(produto) for produto in produtos_validos]
            )

            # Offload da operação de I/O bloqueante (to_excel) para uma thread
            await asyncio.to_thread(df.to_excel, arquivo_saida, index=False)

            # Persistência no banco de dados
            scan_db_id = None
            try:
                from services.category_monitor_service import persist_category_scan_to_db
                scan_db_id = await persist_category_scan_to_db(
                    brand_key=marca,
                    category_url=url_categoria,
                    products=produtos_validos,
                    origin="ad_hoc",
                    started_at=started_at,
                )
                if scan_db_id:
                    emit_log({"type": "info", "message": f"Varredura salva no banco de dados (scan_id={scan_db_id}) com {len(produtos_validos)} produtos."})
            except Exception as db_err:
                logger.error(f"Erro ao persistir varredura de categoria no banco ({marca}): {db_err}", exc_info=True)

            from core.pricing import calculate_price_averages
            averages = calculate_price_averages(produtos_validos)
            brand_averages = averages.get("brand_averages") or {
                marca.lower(): {
                    "average_price": averages.get("average_price"),
                    "average_price_original": averages.get("average_price_original"),
                    "count": averages.get("count", averages.get("total_items", len(produtos_validos))),
                }
            }

            msg_type = "cancelled_done" if is_cancelled() else "done"
            done_payload = {
                "type": msg_type,
                "valid_products": len(produtos_validos),
                "output_file": arquivo_saida,
                "brand_averages": brand_averages,
                "message": f"{'Cancelado. ' if is_cancelled() else ''}Sucesso! Dados de {len(produtos_validos)} produtos salvos em {arquivo_saida}.",
            }
            if scan_db_id:
                done_payload["scan_id"] = scan_db_id
            emit_log(done_payload)
        else:
            if is_cancelled():
                emit_log({"type": "cancelled", "message": "Operação cancelada. Nenhum produto foi coletado."})
            else:
                emit_log({"type": "error_done", "message": "Nenhum produto válido extraído."})
    except Exception as e:
        logger.error(f"Erro no orquestrador ({marca}): {e}")
        emit_log({"type": "error_done", "message": f"Erro crítico: {e}"})

    emit_log("Pipeline de Camada Bronze concluído.")


async def main():
    marca = "Aramis"
    url_categoria = "https://www.aramis.com.br/roupas/polos"
    await run_orchestrator(marca, url_categoria)


if __name__ == "__main__":
    asyncio.run(main())
