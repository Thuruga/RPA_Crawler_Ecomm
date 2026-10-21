"""
fix_remaining_duplicates.py
============================
Resolve os duplicados restantes após fix_all_categories.py:
  - Zapalla: categorias de outlet (prefixar label com Outlet -)
  - Hugo Boss: categorias internas duplicadas (subcategoria vs categoria-mãe)
  - Track & Field: categorias infantis compartilham label com adulto (prefixar Infantil -)
  - Levi's: categorias infantis compartilham label com adulto (prefixar Kids -)
  - Austral: Overshirts em duas coleções (prefixar Coleções - para uma)
"""
import asyncio
import logging
from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


async def fix_zapalla():
    log.info("\n=== ZAPALLA: Prefixando categorias de Outlet ===")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'zapalla' AND bcm.source_path LIKE '%/outlet/%'
        """
    )
    for r in rows:
        if r["label"].startswith("Outlet -"):
            continue
        new_label = f"Outlet - {r['label']}"
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [zapalla] id={r['id']} '{r['label']}' -> '{new_label}'")

    # Acessórios duplicada (geral vs calcados-e-acessorios)
    acess_rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'zapalla' AND bcm.label = 'Acessórios'
        AND bcm.source_path LIKE '%/acessorios%'
        """
    )
    for r in acess_rows:
        if "/calcados-e-acessorios/" in r["source_path"]:
            await db.query_raw(
                "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
                "Calçados e Acessórios - Acessórios", r["id"]
            )
            log.info(f"  [zapalla] id={r['id']} Acessórios -> 'Calçados e Acessórios - Acessórios'")

    # BeachWear outlet
    bw_rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'zapalla' AND bcm.label = 'BeachWear' AND bcm.source_path LIKE '%/outlet/%'
        """
    )
    for r in bw_rows:
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = 'Outlet - BeachWear' WHERE id = $1",
            r["id"]
        )
        log.info(f"  [zapalla] id={r['id']} BeachWear (outlet) -> 'Outlet - BeachWear'")


async def fix_hugoboss():
    log.info("\n=== HUGO BOSS: Resolvendo duplicatas internas ===")
    # Bermudas: /masculino/roupas/calcas/bermudas vs /masculino/roupas/bermudas
    # Manter a geral, contexualizar a subcategoria como "Calças - Bermudas"
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hugoboss'
        AND ((bcm.source_path LIKE '%/calcas/bermudas%')
        OR (bcm.source_path LIKE '%/roupas/blazers/blazers%')
        OR (bcm.source_path LIKE '%/roupas/ternos/ternos%')
        OR (bcm.source_path LIKE '%/underwear/camisetas%')
        OR (bcm.source_path LIKE '%/carteiras-e-porta-cartoes/gift-set%')
        OR (bcm.source_path LIKE '%/tecnologia-e-lifestyle/gift-set%'))
        """
    )

    for r in rows:
        path = r["source_path"]
        label = r["label"]

        if "/calcas/bermudas" in path:
            new_label = "Calças - Bermudas"
        elif "/roupas/blazers/blazers" in path:
            new_label = "Blazers Formais"
        elif "/roupas/ternos/ternos" in path:
            new_label = "Ternos Completos"
        elif "/underwear/camisetas" in path:
            new_label = "Underwear - Camisetas"
        elif "/carteiras-e-porta-cartoes/gift-set" in path:
            new_label = "Gift Set (Carteiras)"
        elif "/tecnologia-e-lifestyle/gift-set" in path:
            new_label = "Gift Set (Tech & Lifestyle)"
        else:
            continue

        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [hugoboss] id={r['id']} '{label}' -> '{new_label}'")


async def fix_trackfield():
    log.info("\n=== TRACK & FIELD: Prefixando categorias Infantis ===")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'trackfield'
        AND bcm.source_path LIKE '%/roupas-infantis/%'
        AND bcm.label NOT LIKE 'Infantil %'
        """
    )
    for r in rows:
        new_label = f"Infantil - {r['label'].title()}"
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [trackfield] id={r['id']} '{r['label']}' -> '{new_label}'")


async def fix_levis():
    log.info("\n=== LEVI'S: Prefixando categorias Kids ===")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'levis'
        AND bcm.source_path LIKE '%/kids/%'
        AND bcm.label NOT LIKE 'Kids -%'
        AND bcm.label NOT LIKE 'Infantil -%'
        AND bcm.label NOT LIKE 'Menino%'
        """
    )
    for r in rows:
        new_label = f"Kids - {r['label']}"
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [levis] id={r['id']} '{r['label']}' -> '{new_label}'")

    # Também atualizar o label "Menino" para "Kids - Menino (Geral)"
    await db.query_raw(
        """
        UPDATE brand_category_mappings
        SET label = 'Kids - Menino (Geral)'
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'levis')
        AND label = 'Menino'
        """
    )
    log.info("  [levis] Atualizado 'Menino' -> 'Kids - Menino (Geral)'")


async def fix_austral():
    log.info("\n=== AUSTRAL: Resolvendo Overshirts duplicado ===")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'austral' AND bcm.label = 'Overshirts'
        """
    )
    for r in rows:
        if "/colecoes/" in r["source_path"]:
            await db.query_raw(
                "UPDATE brand_category_mappings SET label = 'Coleções - Overshirts' WHERE id = $1",
                r["id"]
            )
            log.info(f"  [austral] id={r['id']} Overshirts (coleções) -> 'Coleções - Overshirts'")


async def main():
    await db.connect()
    log.info("Conectado ao banco. Resolvendo duplicatas restantes...")

    await fix_zapalla()
    await fix_hugoboss()
    await fix_trackfield()
    await fix_levis()
    await fix_austral()

    log.info("\n=== CONCLUÍDO ===")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
