import asyncio
from core.db import db
from collections import defaultdict
import re

async def run():
    await db.connect()
    
    rows = await db.query_raw('''
        SELECT b.brand_key, b.brand_name, b.domain, bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.is_active = true
        ORDER BY b.brand_name, bcm.label
    ''')
    
    by_brand = defaultdict(list)
    for r in rows:
        by_brand[(r['brand_key'], r['brand_name'], r['domain'])].append(r)
        
    core_cats = {
        'polos': ['polo'],
        'camisas': ['camisa'],
        'camisetas': ['camiseta', 't-shirt', 'tshirt'],
        'calcas': ['calca', 'calça', 'jeans'],
        'bermudas': ['bermuda', 'short'],
        'casacos_jaquetas': ['casaco', 'jaqueta', 'moletom', 'tricot', 'trico', 'sueter', 'blazer', 'blusao'],
        'calcados': ['calcado', 'calçado', 'tenis', 'tênis', 'sapato', 'bota', 'chinelo', 'drive'],
        'acessorios': ['acessorio', 'acessório', 'cinto', 'carteira', 'bone', 'boné', 'mochila', 'meia', 'cueca', 'sunga', 'oculos', 'óculos', 'relogio', 'relógio', 'perfume', 'gravata'],
    }
    
    print("=== SUMMARY AUDIT PER BRAND ===")
    for (bk, bname, domain), items in sorted(by_brand.items(), key=lambda x: x[0][1]):
        labels_lower = [it['label'].lower() + " " + it['source_path'].lower() for it in items]
        all_text = " ".join(labels_lower)
        
        missing = []
        for cname, kws in core_cats.items():
            if not any(kw in all_text for kw in kws):
                missing.append(cname)
                
        # Count duplicates
        seen_p = set()
        seen_l = set()
        dup_p = 0
        dup_l = 0
        for it in items:
            p = it['source_path'].lower().rstrip('/')
            l = it['label'].lower().strip()
            if p in seen_p:
                dup_p += 1
            seen_p.add(p)
            if l in seen_l:
                dup_l += 1
            seen_l.add(l)
            
        print(f"\n[{bname} ({bk})] - Total Mappings: {len(items)}")
        print(f"  Domain: {domain}")
        print(f"  Missing Core Menswear: {missing if missing else 'NENHUMA (cobertura completa)'}")
        print(f"  Duplicate Paths: {dup_p} | Duplicate Labels: {dup_l}")

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
