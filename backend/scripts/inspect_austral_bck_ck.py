import asyncio
from core.db import db

async def run():
    await db.connect()
    for bk in ['austral', 'bck']:
        rows = await db.query_raw(f'''
            SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
            FROM brand_category_mappings bcm
            JOIN brands b ON b.id = bcm.brand_id
            WHERE b.brand_key = '{bk}'
            ORDER BY bcm.label, bcm.id
        ''')
        print(f"\n=== {bk.upper()} ({len(rows)} items) ===")
        for r in rows:
            print(f"  [{r['id']}] slug='{r['canonical_slug']}' label='{r['label']}' path='{r['source_path']}'")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
