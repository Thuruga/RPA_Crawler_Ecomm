import asyncio
from core.db import db

async def run():
    await db.connect()
    rows = await db.query_raw('''
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hugoboss'
        ORDER BY bcm.label, bcm.id
    ''')
    print(f"=== HUGO BOSS ({len(rows)} items) ===")
    from collections import defaultdict
    by_label = defaultdict(list)
    for r in rows:
        by_label[r['label']].append(r)
    for l, items in sorted(by_label.items()):
        if len(items) > 1:
            print(f"  Label '{l}' ({len(items)}x):")
            for it in items:
                print(f"    [id={it['id']}] slug='{it['canonical_slug']}' path='{it['source_path']}'")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
