import asyncio
from core.db import db
from collections import defaultdict

async def run():
    await db.connect()
    for bk in ['aramis', 'foxton']:
        rows = await db.query_raw(f'''
            SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
            FROM brand_category_mappings bcm
            JOIN brands b ON b.id = bcm.brand_id
            WHERE b.brand_key = '{bk}'
            ORDER BY bcm.label, bcm.id
        ''')
        print(f"\n=== {bk.upper()} ({len(rows)} items) ===")
        by_label = defaultdict(list)
        for r in rows:
            by_label[r['label']].append(r)
        dups = {k: v for k, v in by_label.items() if len(v) > 1}
        print(f"  Total duplicates: {len(dups)}")
        for l, items in dups.items():
            print(f"    '{l}' ({len(items)}x): {[it['source_path'] for it in items]}")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
