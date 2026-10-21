import asyncio
from core.db import db

async def run():
    await db.connect()
    
    # 1. Lacoste
    lacoste_rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'lacoste'
        ORDER BY bcm.id
    ''')
    print("=== LACOSTE IN DB ===")
    for r in lacoste_rows:
        print(f"  [{r['id']}] {r['label']} -> {r['source_path']} (slug: {r['canonical_slug']})")
    
    # 2. Foxton
    foxton_rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'foxton' AND bcm.label = '-'
    ''')
    print("\n=== FOXTON '-' IN DB ===")
    for r in foxton_rows:
        print(f"  [{r['id']}] {r['label']} -> {r['source_path']}")

    # 3. Hering PU
    hering_rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hering' AND bcm.label = 'PU'
    ''')
    print("\n=== HERING PU IN DB ===")
    for r in hering_rows:
        print(f"  [{r['id']}] {r['label']} -> {r['source_path']}")

    # 4. Buckman Novidades / Outlet
    bck_rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'bck' AND bcm.label IN ('Novidades', 'Outlet')
    ''')
    print("\n=== BCK GENERIC IN DB ===")
    for r in bck_rows:
        print(f"  [{r['id']}] {r['label']} -> {r['source_path']}")

    # 5. Zara Novidades
    zara_rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'zara' AND bcm.label = 'Novidades'
    ''')
    print("\n=== ZARA NOVIDADES IN DB ===")
    for r in zara_rows:
        print(f"  [{r['id']}] {r['label']} -> {r['source_path']}")

    # 6. Check count of all mappings per brand in DB
    counts = await db.query_raw('''
        SELECT b.brand_key, COUNT(*) as cnt
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        GROUP BY b.brand_key
        ORDER BY cnt DESC
    ''')
    print("\n=== BRAND MAPPINGS COUNT IN DB ===")
    for c in counts:
        print(f"  {c['brand_key']}: {c['cnt']} total")

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
