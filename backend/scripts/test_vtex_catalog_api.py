import asyncio
import httpx
from core.db import db

async def test_vtex_brands():
    await db.connect()
    rows = await db.query_raw("""
        SELECT b.brand_key, b.brand_name, b.domain, bcm.source_path
        FROM brands b
        JOIN brand_category_mappings bcm ON b.id = bcm.brand_id
        WHERE b.is_active = true AND b.engine = 'vtex'
        ORDER BY b.brand_name, bcm.canonical_slug
    """)

    from collections import defaultdict
    by_brand = defaultdict(list)
    for r in rows:
        by_brand[(r['brand_key'], r['brand_name'], r['domain'])].append(r['source_path'])

    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for (bk, bname, domain), paths in by_brand.items():
            raw_p = paths[0] or ""
            if "://" in raw_p:
                raw_p = raw_p.split("://", 1)[1]
                raw_p = raw_p.split("/", 1)[1] if "/" in raw_p else ""
            test_path = raw_p.strip("/")
            api_url = f"https://{domain}/api/catalog_system/pub/products/search/{test_path}?_from=0&_to=5"
            try:
                r = await c.get(api_url)
                if r.status_code in (200, 206):
                    try:
                        data = r.json()
                        slice_count = len(data) if isinstance(data, list) else 0
                        resources = r.headers.get("resources", "")
                        total_count = resources.split("/")[-1] if "/" in resources else str(slice_count)
                        print(f"[{bk}] {bname}: Catalog System returns {slice_count} in slice ({total_count} total produtos) (HTTP {r.status_code}) [path: /{test_path}]")
                    except Exception:
                        print(f"[{bk}] {bname}: Returns {r.status_code} but not JSON (HTML redirect)")
                else:
                    print(f"[{bk}] {bname}: Catalog System returns HTTP {r.status_code} [path: /{test_path}]")
            except Exception as e:
                print(f"[{bk}] {bname}: Error {e}")

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(test_vtex_brands())
