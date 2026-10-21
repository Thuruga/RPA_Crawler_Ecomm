"""
Script para sincronizar e persistir todos os mapeamentos de categorias infantis
de brands.json na tabela brand_category_mappings no banco de dados Postgres.
"""
import asyncio
import sys
import os
import json
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.db import db

async def sync():
    try:
        await db.connect()
        print("Connected to database.")
        
        brands_json_path = os.path.join(os.path.dirname(__file__), "..", "data", "brands.json")
        with open(brands_json_path, encoding="utf-8") as f:
            brands_data = json.load(f)
            
        synced_count = 0
        for brand_key in ["reserva", "lacoste", "hering"]:
            brand_info = brands_data.get(brand_key)
            if not brand_info:
                continue
                
            brand_row = await db.query_first(
                "SELECT id FROM brands WHERE brand_key = $1",
                brand_key
            )
            if not brand_row:
                print(f"[SKIP] Brand not in DB brands table: {brand_key}")
                continue
                
            brand_id = brand_row["id"]
            
            for m in brand_info.get("mappings", []):
                slug = m.get("canonical_slug", "")
                if slug == "infantil" or slug.startswith("infantil-"):
                    await db.execute_raw(
                        """
                        INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
                        VALUES ($1, $2, $3, $4, NOW(), NOW())
                        ON CONFLICT (brand_id, canonical_slug)
                        DO UPDATE SET
                            source_path = EXCLUDED.source_path,
                            label = EXCLUDED.label,
                            updated_at = NOW()
                        """,
                        brand_id,
                        slug,
                        m.get("vtex_fq_path"),
                        m.get("label")
                    )
                    synced_count += 1
                    print(f"  [SYNCED] {brand_key:8s} | {slug:22s} -> {m.get('vtex_fq_path')}")

        print(f"\nTotal infantil mappings synced to DB: {synced_count}")
    except Exception as e:
        print(f"[ERROR/WARNING] DB sync failed: {e}")
    finally:
        try:
            await db.disconnect()
        except:
            pass

if __name__ == "__main__":
    asyncio.run(sync())
