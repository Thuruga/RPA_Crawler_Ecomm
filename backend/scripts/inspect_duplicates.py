import asyncio
from core.db import db
from collections import defaultdict

async def run():
    await db.connect()
    
    rows = await db.query_raw('''
        SELECT b.brand_key, b.brand_name, bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        ORDER BY b.brand_key, bcm.label
    ''')
    
    by_brand = defaultdict(list)
    for r in rows:
        by_brand[r['brand_key']].append(r)
        
    print("=== ANÁLISE DETALHADA DE DUPLICADOS E AMBIGUIDADES ===")
    for brand_key, items in sorted(by_brand.items()):
        label_groups = defaultdict(list)
        path_groups = defaultdict(list)
        for it in items:
            label_groups[it['label'].strip().lower()].append(it)
            path_groups[it['source_path'].strip().lower()].append(it)
            
        dup_labels = {k: v for k, v in label_groups.items() if len(v) > 1}
        dup_paths = {k: v for k, v in path_groups.items() if len(v) > 1}
        
        if dup_labels or dup_paths:
            print(f"\n--- Brand: {brand_key} (Total items: {len(items)}) ---")
            if dup_paths:
                print(f"  [DUP PATHS - MESMA URL ({len(dup_paths)})]:")
                for p, v in dup_paths.items():
                    labels = [x['label'] for x in v]
                    print(f"    URL: {p} => Labels: {labels}")
            if dup_labels:
                print(f"  [DUP LABELS - MESMO NOME ({len(dup_labels)})]:")
                for l, v in list(dup_labels.items())[:8]:
                    paths = [x['source_path'] for x in v]
                    print(f"    Label: '{v[0]['label']}' ({len(v)}x) => Paths: {paths}")
                if len(dup_labels) > 8:
                    print(f"    ... e mais {len(dup_labels) - 8} labels duplicados")
        else:
            print(f"--- Brand: {brand_key} (Total: {len(items)}) => CLEAN (sem duplicados de label ou path) ---")

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
