import asyncio
from core.db import db

async def run():
    await db.connect()
    rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key IN ('zara', 'levis')
        ORDER BY b.brand_key, bcm.label, bcm.id
    ''')
    for r in rows:
        print(f"[{r['brand_key']}] id={r['id']} slug='{r['canonical_slug']}' label='{r['label']}' path='{r['source_path']}'")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
