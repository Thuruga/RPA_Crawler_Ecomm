import asyncio
from core.db import db
from urllib.parse import urlparse

async def run():
    await db.connect()
    rows = await db.query_raw('''
        SELECT bcm.id, b.brand_key, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        ORDER BY b.brand_key, bcm.id
    ''')
    
    # Normalize paths: strip domain, strip trailing slash, lower
    def norm_p(p):
        if not p:
            return ""
        parsed = urlparse(p)
        path = parsed.path if parsed.netloc else p
        return path.rstrip("/").lower()

    by_brand = {}
    for r in rows:
        bk = r['brand_key']
        by_brand.setdefault(bk, []).append(r)
        
    print("=== ANÁLISE DE DUPLICAÇÃO DE ROTAS (ABSOLUTA vs RELATIVA) ===")
    for bk, items in sorted(by_brand.items()):
        path_map = {}
        duplicates = []
        for it in items:
            np = norm_p(it['source_path'])
            if not np:
                continue
            if np in path_map:
                duplicates.append((path_map[np], it))
            else:
                path_map[np] = it
                
        if duplicates:
            print(f"\nBrand: {bk} -> {len(duplicates)} duplicações de rota:")
            for orig, dup in duplicates[:10]:
                print(f"  Orig: [id={orig['id']}] slug='{orig['canonical_slug']}' label='{orig['label']}' path='{orig['source_path']}'")
                print(f"   Dup: [id={dup['id']}] slug='{dup['canonical_slug']}' label='{dup['label']}' path='{dup['source_path']}'")
            if len(duplicates) > 10:
                print(f"  ... e mais {len(duplicates) - 10} duplicatas")

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
