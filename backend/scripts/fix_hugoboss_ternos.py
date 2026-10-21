import asyncio
from core.db import db

async def run():
    await db.connect()
    rows = await db.query_raw('''
        SELECT bcm.id, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hugoboss' AND bcm.label = 'Ternos Completos'
    ''')
    for r in rows:
        path_part = r['source_path'].split('/')[-1]
        if path_part == 'ternos':
            label = 'Ternos'
        elif 'colete' in path_part:
            label = 'Ternos com Colete'
        elif 'mix' in path_part:
            label = 'Ternos Mix e Match'
        else:
            continue
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            label, r['id']
        )
        print(f"Hugo Boss id={r['id']} -> {label}")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
