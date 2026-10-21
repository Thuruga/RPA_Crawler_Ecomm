"""
Worker de Descoberta e Comparação de Categorias com o Banco de Dados.

Executa o pipeline de descoberta oficial (_discover_and_validate) de todas
as marcas com suporte a catálogo.

GARANTIA ESTRITA:
NENHUMA alteração é feita no banco de dados (a tabela brand_category_mappings
NÃO é modificada). Todos os dados coletados são salvos em:
backend/data/live_worker_extracted_categories.json

Em seguida, compara o que o worker extraiu ao vivo com o que temos mapeado no banco.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Set

# Adiciona o diretório backend ao sys.path se necessário
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.db import db
from services.brand_service import brand_service
from services.engines.factory import engine_factory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DiscoveryWorkerCompare")


def normalize_path(p: str) -> str:
    """Normaliza um path para comparação uniforme."""
    if not p:
        return ""
    p = p.strip().lower()
    # Remove domínio se presente
    if "://" in p:
        p = p.split("://", 1)[1]
        idx = p.find("/")
        p = p[idx:] if idx >= 0 else ""
    # Remove query string para match estrutural
    if "?" in p:
        p = p.split("?", 1)[0]
    return p.strip("/").rstrip("/")


async def run_discovery_for_brand(brand_key: str, engine: Any) -> Dict[str, Any]:
    """Executa o pipeline oficial de descoberta do worker para uma marca."""
    if not hasattr(engine, "_discover_and_validate"):
        return {
            "status": "not_applicable",
            "categories": [],
            "probes": {"has_products": 0, "empty": 0, "blocked": 0},
            "error": None,
        }

    try:
        logger.info(f"[{brand_key}] Executando _discover_and_validate()...")
        outcome = await asyncio.wait_for(engine._discover_and_validate(), timeout=90.0)

        from services.category_discovery import ProbeStatus

        probes = outcome.probes if hasattr(outcome, "probes") else {}
        empty_c = sum(1 for p in probes.values() if getattr(p, "status", None) == ProbeStatus.EMPTY)
        blocked_c = sum(1 for p in probes.values() if getattr(p, "status", None) == ProbeStatus.BLOCKED)
        has_prod_c = sum(1 for p in probes.values() if getattr(p, "status", None) == ProbeStatus.HAS_PRODUCTS)

        cats = []
        for c in (outcome.offered if hasattr(outcome, "offered") else []):
            cats.append({
                "name": getattr(c, "name", ""),
                "path": getattr(c, "path", ""),
                "url": getattr(c, "url", ""),
                "group": getattr(c, "group", ""),
                "ancestors": list(getattr(c, "ancestors", ())),
            })

        logger.info(f"[{brand_key}] Sucesso: {len(cats)} categorias extraídas pelo worker.")
        return {
            "status": "success",
            "categories": cats,
            "probes": {
                "has_products": has_prod_c,
                "empty": empty_c,
                "blocked": blocked_c,
                "total_probed": len(probes),
            },
            "error": None,
        }
    except asyncio.TimeoutError:
        logger.error(f"[{brand_key}] Timeout (60s) ao executar descoberta.")
        return {
            "status": "timeout",
            "categories": [],
            "probes": {},
            "error": "Timeout de 60s excedido",
        }
    except Exception as exc:
        logger.error(f"[{brand_key}] Erro ao executar descoberta: {exc}", exc_info=True)
        return {
            "status": "error",
            "categories": [],
            "probes": {},
            "error": str(exc),
        }


async def main():
    logger.info("=== INICIANDO WORKER DE EXTRAÇÃO DE CATEGORIAS (SOMENTE LEITURA / SEM GRAVAÇÃO NO BANCO) ===")
    await db.connect()
    await brand_service.load_from_db()

    # 1. Carrega todas as marcas ativas
    brands = brand_service.list_brands(active_only=True)
    logger.info(f"Total de marcas ativas registradas: {len(brands)}")

    worker_results: Dict[str, Any] = {}

    output_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "live_worker_extracted_categories.json")
    )

    worker_results = {}
    if os.path.exists(output_path) and "--rescan" not in sys.argv:
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                data_cached = json.load(f)
                worker_results = data_cached.get("brands", {})
        except Exception:
            worker_results = {}

    def _save_current():
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "extracted_at": datetime.now().isoformat(),
                    "brands_count": len(worker_results),
                    "brands": worker_results,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    # 2. Executa a extração para cada marca catalog-capable (apenas se ainda não bem-sucedida ou se --rescan)
    for brand in brands:
        bkey = brand.brand_key.lower()
        eng = engine_factory.get_engine(bkey)
        
        existing_status = worker_results.get(bkey, {}).get("discovery_result", {}).get("status")
        if existing_status == "success" and "--rescan" not in sys.argv:
            cats_count = len(worker_results[bkey]["discovery_result"].get("categories", []))
            logger.info(f"[{bkey}] Usando extração já realizada com sucesso ({cats_count} categorias).")
            continue

        if not hasattr(eng, "_discover_and_validate"):
            worker_results[bkey] = {
                "brand_name": brand.brand_name,
                "engine": eng.__class__.__name__,
                "discovery_result": {
                    "status": "not_applicable",
                    "categories": [],
                    "probes": {"has_products": 0, "empty": 0, "blocked": 0},
                    "error": None,
                },
            }
            _save_current()
            continue

        logger.info(f"--- Processando marca: {bkey} ({brand.brand_name}) [Engine: {eng.__class__.__name__}] ---")
        res = await run_discovery_for_brand(bkey, eng)
        worker_results[bkey] = {
            "brand_name": brand.brand_name,
            "engine": eng.__class__.__name__,
            "discovery_result": res,
        }
        _save_current()
        logger.info(f"[{bkey}] Salvo incrementalmente em {output_path}")
        await asyncio.sleep(0.5)

    logger.info(f"==> TODAS AS CATEGORIAS COLETADAS SALVAS COM SUCESSO EM: {output_path} (NÃO ENVIADAS AO BANCO)")

    # 4. Busca as categorias atualmente mapeadas no banco de dados (PostgreSQL)
    db_mappings_raw = await db.query_raw("""
        SELECT 
            b.brand_key,
            b.brand_name,
            m.canonical_slug,
            m.source_path,
            m.label
        FROM brand_category_mappings m
        JOIN brands b ON m.brand_id = b.id
        WHERE b.is_active = true
        ORDER BY b.brand_key, m.label
    """)

    db_by_brand: Dict[str, List[Dict[str, Any]]] = {}
    for row in db_mappings_raw:
        bk = row["brand_key"].lower()
        if bk not in db_by_brand:
            db_by_brand[bk] = []
        db_by_brand[bk].append(row)

    # 5. Compara o que o Worker extraiu vs o que está Mapeado no Banco
    comparison_report = {}

    print("\n" + "=" * 110)
    print(f"{'COMPARAÇÃO: CATEGORIAS MAPEADAS NO BANCO VS EXTRAÍDAS PELO WORKER':^110}")
    print("=" * 110)
    print(f"{'Marca':<16} | {'Engine':<16} | {'No Banco':<9} | {'No Worker':<10} | {'Batem':<7} | {'Só no Banco':<12} | {'Novo no Site':<12} | {'Paridade':<8}")
    print("-" * 110)

    total_db_all = 0
    total_worker_all = 0
    total_matched_all = 0

    for b in brands:
        bk = b.brand_key.lower()
        eng_name = worker_results[bk]["engine"] if bk in worker_results else "N/A"
        w_data = worker_results.get(bk, {}).get("discovery_result", {})
        w_status = w_data.get("status")
        w_cats = w_data.get("categories", [])
        db_cats = db_by_brand.get(bk, [])

        total_db_all += len(db_cats)
        total_worker_all += len(w_cats)

        if w_status == "not_applicable":
            print(f"{bk:<16} | {eng_name:<16} | {len(db_cats):<9} | {'N/A (busca)':<10} | {'-':<7} | {'-':<12} | {'-':<12} | {'N/A':<8}")
            continue

        if w_status != "success":
            print(f"{bk:<16} | {eng_name:<16} | {len(db_cats):<9} | {f'ERRO ({w_status})':<10} | {'-':<7} | {'-':<12} | {'-':<12} | {'0%':<8}")
            continue

        # Mapeia as categorias do worker por path normalizado e por nome
        worker_by_norm_path: Dict[str, Dict[str, Any]] = {}
        worker_by_norm_name: Dict[str, Dict[str, Any]] = {}
        for c in w_cats:
            np = normalize_path(c["path"])
            if np:
                worker_by_norm_path[np] = c
            nn = c["name"].strip().lower()
            if nn:
                worker_by_norm_name[nn] = c

        # Mapeia do banco
        matched_in_db = []
        only_in_db = []
        matched_worker_paths = set()

        for row in db_cats:
            db_np = normalize_path(row["source_path"])
            db_slug = normalize_path(row["canonical_slug"])
            db_label = (row["label"] or "").strip().lower()

            # Tenta casar por path normalizado, por slug ou por label/name
            match = None
            matched_key = None
            if db_np in worker_by_norm_path:
                match = worker_by_norm_path[db_np]
                matched_key = db_np
            elif db_slug in worker_by_norm_path:
                match = worker_by_norm_path[db_slug]
                matched_key = db_slug
            elif db_label in worker_by_norm_name:
                match = worker_by_norm_name[db_label]
                matched_key = normalize_path(match["path"])

            if match:
                matched_in_db.append({
                    "db_label": row["label"],
                    "db_path": row["source_path"],
                    "worker_name": match["name"],
                    "worker_path": match["path"],
                })
                if matched_key:
                    matched_worker_paths.add(matched_key)
            else:
                only_in_db.append({
                    "db_label": row["label"],
                    "db_path": row["source_path"],
                    "canonical_slug": row["canonical_slug"],
                })

        # Categorias que o worker extraiu mas não temos no banco
        new_in_worker = []
        for np, c in worker_by_norm_path.items():
            if np not in matched_worker_paths:
                new_in_worker.append({
                    "name": c["name"],
                    "path": c["path"],
                    "group": c["group"],
                })

        parity_pct = (len(matched_in_db) / len(db_cats) * 100) if db_cats else 0.0
        total_matched_all += len(matched_in_db)

        print(
            f"{bk:<16} | {worker_results[bk]['engine']:<16} | {len(db_cats):<9} | {len(w_cats):<10} | "
            f"{len(matched_in_db):<7} | {len(only_in_db):<12} | {len(new_in_worker):<12} | {parity_pct:6.1f}%"
        )

        comparison_report[bk] = {
            "brand_name": b.brand_name,
            "engine": worker_results[bk]["engine"],
            "total_in_db": len(db_cats),
            "total_in_worker": len(w_cats),
            "matched_count": len(matched_in_db),
            "only_in_db_count": len(only_in_db),
            "new_in_worker_count": len(new_in_worker),
            "parity_pct": round(parity_pct, 1),
            "matched_samples": matched_in_db[:10],
            "only_in_db_samples": only_in_db[:15],
            "new_in_worker_samples": new_in_worker[:15],
        }

    print("-" * 110)
    print(f"Total Banco: {total_db_all} | Total Extraído pelo Worker: {total_worker_all} | Total que Batem: {total_matched_all}")
    print("=" * 110 + "\n")

    # Salva relatório de comparação em JSON
    report_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "category_parity_comparison_report.json")
    )
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "compared_at": datetime.now().isoformat(),
                "brands": comparison_report,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info(f"Relatório de comparação salvo em: {report_path}")

    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
