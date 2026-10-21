import asyncio
from core.db import db
from collections import defaultdict

async def run():
    await db.connect()
    rows = await db.query_raw("""
        SELECT b.brand_key, b.brand_name, b.domain, bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brands b
        LEFT JOIN brand_category_mappings bcm ON b.id = bcm.brand_id
            AND (
                bcm.canonical_slug ILIKE '%calcado%' 
                OR bcm.canonical_slug ILIKE '%tenis%' 
                OR bcm.canonical_slug ILIKE '%sapato%' 
                OR bcm.canonical_slug ILIKE '%bota%' 
                OR bcm.canonical_slug ILIKE '%chinelo%'
                OR bcm.canonical_slug ILIKE '%sneaker%'
                OR bcm.label ILIKE '%calçado%' 
                OR bcm.label ILIKE '%calcado%' 
                OR bcm.label ILIKE '%tênis%' 
                OR bcm.label ILIKE '%tenis%' 
                OR bcm.label ILIKE '%sapato%' 
                OR bcm.label ILIKE '%bota%' 
                OR bcm.label ILIKE '%chinelo%'
                OR bcm.label ILIKE '%sneaker%'
            )
        WHERE b.is_active = true AND b.brand_key NOT IN ('amazon', 'mercado_livre', 'netshoes', 'privalia')
        ORDER BY b.brand_name, bcm.canonical_slug
    """)

    by_brand = defaultdict(list)
    for r in rows:
        by_brand[(r['brand_key'], r['brand_name'], r['domain'])].append(r)

    print("=== STATUS DE CALÇADOS - MARCAS ATIVAS DE MODA ===")
    for (bk, bname, domain), items in sorted(by_brand.items(), key=lambda x: x[0][1]):
        valid_items = [it for it in items if it['id'] is not None]
        if valid_items:
            cats_summary = ", ".join(f"{it['label']} ({it['canonical_slug']})" for it in valid_items[:3])
            more = f" (+{len(valid_items)-3} more)" if len(valid_items) > 3 else ""
            print(f"[OK] {bname:20} ({bk:12}): {len(valid_items)} categoria(s) -> {cats_summary}{more}")
        else:
            print(f"[MISSING] {bname:16} ({bk:12}): NENHUMA categoria de calçados")

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
