import asyncio
from core.db import db
from collections import defaultdict

async def run():
    await db.connect()
    rows = await db.query_raw('''
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hering'
        ORDER BY bcm.label, bcm.id
    ''')
    print(f"=== HERING ({len(rows)} items) ===")
    by_label = defaultdict(list)
    for r in rows:
        by_label[r['label']].append(r)
    for l, items in sorted(by_label.items()):
        if len(items) > 1:
            paths = [it['source_path'] for it in items]
            print(f"  Label '{l}' ({len(items)}x) => Paths: {paths}")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
