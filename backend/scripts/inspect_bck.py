import asyncio, httpx
from core.db import db

async def run():
    await db.connect()

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get("https://buckmanbck.com.br/collections.json?limit=50")
        data = r.json()

    collections = data.get("collections", [])
    print("=== BCK COLEÇÕES ===")
    for col in collections:
        print(f"  {col['title']} -> /collections/{col['handle']} ({col['products_count']} produtos)")

    rows = await db.query_raw("""
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'bck'
        ORDER BY bcm.source_path
    """)
    print("\n=== BCK NO BANCO ATUAL ===")
    for r in rows:
        print(f"  id={r['id']} slug={r['canonical_slug']} label={r['label']} path={r['source_path']}")

    await db.disconnect()

if __name__ == "__main__":
    asyncio.run(run())
