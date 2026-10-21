"""
apply_site_corrections.py
=========================
Aplica correções nos mapeamentos com base nos testes ao vivo contra os sites:
1. Aramis: /roupas/tricots -> /roupas/tricot
2. Track & Field: /acessorios-masculinos/meias -> /meias-masculinas
3. Hering: /masculino/camisetas -> /camisetas, etc.
4. Richards: remove 7 URLs desatualizadas sem o prefixo /masculino/
5. Hugo Boss: remove 5 URLs de acessórios/blazers que foram descontinuadas
6. Brooksfield: remove as 22 categorias da antiga loja Junior (loja em manutenção/403)
"""

import asyncio
import logging
from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

async def fix_aramis():
    log.info("\n=== 1. ARAMIS ===")
    await db.query_raw("""
        UPDATE brand_category_mappings 
        SET source_path = '/roupas/tricot', updated_at = NOW()
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'aramis') 
          AND source_path = '/roupas/tricots'
    """)
    log.info("  [aramis] /roupas/tricots -> /roupas/tricot")

async def fix_trackfield():
    log.info("\n=== 2. TRACK & FIELD ===")
    await db.query_raw("""
        UPDATE brand_category_mappings 
        SET source_path = '/meias-masculinas', updated_at = NOW()
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'trackfield') 
          AND canonical_slug = 'acessorios-meias'
    """)
    log.info("  [trackfield] meias -> /meias-masculinas")

async def fix_hering():
    log.info("\n=== 3. HERING ===")
    mappings = [
        ("camisetas", "/camisetas", "Camisetas"),
        ("polos", "/polos", "Polos"),
        ("calcas", "/calcas", "Calças"),
        ("bermudas", "/bermudas", "Bermudas"),
        ("jaquetas", "/jaquetas-e-casacos", "Jaquetas e Casacos"),
        ("moletom", "/moletom", "Moletom"),
    ]
    for slug, path, label in mappings:
        await db.query_raw("""
            UPDATE brand_category_mappings
            SET source_path = $1, label = $2, updated_at = NOW()
            WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'hering')
              AND canonical_slug = $3
        """, path, label, slug)
        log.info(f"  [hering] {slug} -> {path}")

    # Remover /kids/menina/blusas (404)
    await db.query_raw("""
        DELETE FROM brand_category_mappings
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'hering')
          AND canonical_slug = 'infantil-blusas'
    """)

async def fix_richards():
    log.info("\n=== 4. RICHARDS ===")
    # Remove as 7 URLs antigas sem /masculino/ pois já existem as versões ativas com /masculino/
    deleted = await db.query_raw("""
        DELETE FROM brand_category_mappings
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'richards')
          AND source_path IN ('/acessorios', '/blazers', '/praia', '/shorts', '/camisetas', '/tricot', '/intimo')
    """)
    log.info("  [richards] Removidas 7 URLs desatualizadas (mantidas as ativas com /masculino/)")

async def fix_hugoboss():
    log.info("\n=== 5. HUGO BOSS ===")
    dead_paths = [
        "/masculino/roupas/blazers",
        "https://www.hugoboss.com.br/masculino/acessorios/bones-e-chapeus/bones",
        "https://www.hugoboss.com.br/masculino/roupas/blazers/colete-social",
        "https://www.hugoboss.com.br/masculino/acessorios/bones-e-chapeus/gorros",
        "https://www.hugoboss.com.br/masculino/roupas/blazers/paleto-social",
    ]
    for p in dead_paths:
        await db.query_raw("""
            DELETE FROM brand_category_mappings
            WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'hugoboss')
              AND source_path = $1
        """, p)
    log.info("  [hugoboss] Removidas 5 URLs 404")

async def fix_brooksfield():
    log.info("\n=== 6. BROOKSFIELD ===")
    # Remover categorias da antiga loja Junior que está em manutenção / 403
    await db.query_raw("""
        DELETE FROM brand_category_mappings
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'brooksfield')
          AND source_path LIKE '/junior/%'
    """)
    log.info("  [brooksfield] Removidas 22 URLs da loja Junior em manutenção")

async def main():
    await db.connect()
    await fix_aramis()
    await fix_trackfield()
    await fix_hering()
    await fix_richards()
    await fix_hugoboss()
    await fix_brooksfield()
    log.info("\n=== TODAS AS CORREÇÕES APLICADAS COM SUCESSO ===")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
