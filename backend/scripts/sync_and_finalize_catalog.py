"""
sync_and_finalize_catalog.py
============================
Ajustes finais de catálogo e categorias:
1. Oriba: Adiciona 'calcados' e 'calcados-tenis' apontando para '/tenis-cariuma'
2. Track & Field: Limpa entradas legadas com URLs absolutas e duplicadas, mantendo catálogo limpo e 100% funcional com calçados
3. Buckman: Mapeia 'acessorios' para '/collections/presentes' ("Presentes e Acessórios") para cobertura menswear completa
4. Hering: Remove as categorias legadas femininas/desestruturadas (biquinis, tiaras, bodies, etc.) mantendo o catálogo masculino + kids + calçados limpo e sem duplicatas
"""

import asyncio
import logging
from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

async def fix_oriba():
    log.info("\n=== 1. ORIBA: Adicionando Calçados (Tênis Cariuma) ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'oriba' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    await db.query_raw("""
        INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
        VALUES 
            ($1, 'calcados', '/tenis-cariuma', 'Calçados (Tênis)', NOW(), NOW()),
            ($1, 'calcados-tenis', '/tenis-cariuma', 'Tênis Cariuma', NOW(), NOW())
        ON CONFLICT (brand_id, canonical_slug) DO UPDATE
            SET source_path = EXCLUDED.source_path,
                label = EXCLUDED.label,
                updated_at = NOW()
    """, brand_id)
    log.info("  [oriba] Inseridas categorias de calçados: /tenis-cariuma")

async def fix_trackfield():
    log.info("\n=== 2. TRACK & FIELD: Limpando entradas legadas e padronizando ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'trackfield' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    # Remover as duplicatas legadas que conflitam com as categorias limpas já existentes
    # (bermudas, shorts, casacos, jaquetas)
    await db.query_raw("""
        DELETE FROM brand_category_mappings 
        WHERE brand_id = $1 AND canonical_slug IN (
            '/roupas-masculinas/bermudas',
            '/roupas-masculinas/shorts',
            '/roupas-masculinas/casacos',
            '/roupas-masculinas/jaquetas'
        )
    """, brand_id)

    # Para as categorias legadas de beach e roupas íntimas restantes, normalizar canonical_slug e source_path
    remaining_legacy = await db.query_raw("""
        SELECT id, canonical_slug, label, source_path 
        FROM brand_category_mappings 
        WHERE brand_id = $1 AND source_path LIKE 'https://%'
    """, brand_id)

    for r in remaining_legacy:
        old_slug = r["canonical_slug"]
        path = r["source_path"].replace("https://www.tf.com.br", "")
        clean_slug = path.strip("/").replace("/", "-")
        # Criar label limpo se estiver em ALL CAPS
        label = r["label"].title() if r["label"].isupper() else r["label"]
        await db.query_raw("""
            UPDATE brand_category_mappings
            SET canonical_slug = $1, source_path = $2, label = $3, updated_at = NOW()
            WHERE id = $4
        """, clean_slug, path, label, r["id"])
        log.info(f"  [trackfield] Normalizado id={r['id']}: slug='{clean_slug}', path='{path}', label='{label}'")

async def fix_buckman():
    log.info("\n=== 3. BUCKMAN: Garantindo categoria Acessórios ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'bck' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    # Adicionar acessorios -> /collections/presentes
    await db.query_raw("""
        INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
        VALUES ($1, 'acessorios', '/collections/presentes', 'Acessórios e Presentes', NOW(), NOW())
        ON CONFLICT (brand_id, canonical_slug) DO UPDATE
            SET source_path = EXCLUDED.source_path,
                label = EXCLUDED.label,
                updated_at = NOW()
    """, brand_id)
    log.info("  [bck] Mapeado 'acessorios' -> '/collections/presentes'")

async def fix_hering():
    log.info("\n=== 4. HERING: Limpando entradas legadas do dump antigo ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'hering' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    # Hering já possui as categorias oficiais masculinas, calçados e kids (1419 a 1438)
    # As 292 entradas legadas com 'https://www.hering.com.br' contêm biquinis, tiaras,
    # cortes duplicados (regular, manga longa, slim em múltiplos departamentos), gerando 137 duplicatas de label.
    # Excluir entradas legadas com URL absoluta que não são do catálogo limpo:
    res = await db.query_raw("""
        DELETE FROM brand_category_mappings
        WHERE brand_id = $1 AND source_path LIKE 'https://www.hering.com.br%'
    """, brand_id)
    log.info(f"  [hering] Removidas categorias legadas desestruturadas")

    # Garantir calçados masculinos limpos da Hering
    calcados_hering = [
        ("calcados", "/calcados", "Calçados (Geral)"),
        ("calcados-tenis", "/calcados/tenis", "Tênis"),
        ("calcados-chinelos", "/calcados/chinelos", "Chinelos"),
        ("calcados-sapatos", "/calcados/sapatos", "Sapatos"),
        ("calcados-mocassins", "/calcados/mocassins", "Mocassins"),
    ]
    for slug, path, label in calcados_hering:
        await db.query_raw("""
            INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
            VALUES ($1, $2, $3, $4, NOW(), NOW())
            ON CONFLICT (brand_id, canonical_slug) DO UPDATE
                SET source_path = EXCLUDED.source_path,
                    label = EXCLUDED.label,
                    updated_at = NOW()
        """, brand_id, slug, path, label)
    log.info("  [hering] Categorias limpas de calçados garantidas")

async def main():
    await db.connect()
    await fix_oriba()
    await fix_trackfield()
    await fix_buckman()
    await fix_hering()
    log.info("\n=== SINCRONIZAÇÃO CONCLUÍDA ===")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
