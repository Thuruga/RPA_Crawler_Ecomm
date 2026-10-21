import asyncio
import json
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

from prisma import Prisma

async def seed_data():
    db = Prisma()
    await db.connect()
    print("Iniciando o Seeding...")

    CHUNK_SIZE = 1000  # marcas

    try:
        # Lendo marcas
        brands_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'brands.json')
        with open(brands_path, 'r', encoding='utf-8') as f:
            brands_dict = json.load(f)
            brands = list(brands_dict.values())

        print(f"Lidas {len(brands)} marcas do JSON.")

        # Inserção massiva de marcas
        for i in range(0, len(brands), CHUNK_SIZE):
            chunk = brands[i:i+CHUNK_SIZE]
            args = []
            values = []
            for idx, b in enumerate(chunk):
                offset = idx * 6
                values.append(f"(${offset+1}, ${offset+2}, ${offset+3}, ${offset+4}, ${offset+5}::jsonb, ${offset+6})")
                args.extend([b.get('brand_key'), b.get('brand_name'), b.get('domain'), b.get('engine'), json.dumps(b), b.get('is_active', True)])
            
            query = f"""
                INSERT INTO scraping.brands (brand_key, brand_name, domain, engine, configuration, is_active)
                VALUES {','.join(values)}
                ON CONFLICT (brand_key) 
                DO UPDATE SET brand_name = EXCLUDED.brand_name, domain = EXCLUDED.domain, engine = EXCLUDED.engine, configuration = EXCLUDED.configuration, is_active = EXCLUDED.is_active;
            """
            await db.execute_raw(query, *args)
        
        print("Marcas seedadas.")

        # Inserção em chunks de mappings de categorias
        rows = await db.query_raw("SELECT id, brand_key FROM scraping.brands;")
        brand_id_map = {r["brand_key"]: r["id"] for r in rows}

        all_mappings = []
        for b in brands:
            brand_id = brand_id_map.get(b.get("brand_key"))
            if not brand_id:
                continue
            for m in b.get("mappings", []):
                slug = m.get("canonical_slug")
                if not slug:
                    continue
                all_mappings.append((brand_id, slug, m.get("vtex_fq_path"), m.get("label")))

        for i in range(0, len(all_mappings), 200):
            chunk = all_mappings[i:i+200]
            values = []
            args = []
            for idx, item in enumerate(chunk):
                offset = idx * 4
                values.append(f"(${offset+1}, ${offset+2}, ${offset+3}, ${offset+4}, NOW(), NOW())")
                args.extend([item[0], item[1], item[2], item[3]])
            query = f"""
                INSERT INTO scraping.brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
                VALUES {','.join(values)}
                ON CONFLICT (brand_id, canonical_slug)
                DO UPDATE SET source_path = EXCLUDED.source_path, label = EXCLUDED.label, updated_at = NOW();
            """
            await db.execute_raw(query, *args)

        print(f"{len(all_mappings)} mappings sincronizados com sucesso.")

    except Exception as e:
        print(f"Erro no seeding: {e}")
    finally:
        await db.disconnect()

if __name__ == "__main__":
    asyncio.run(seed_data())
