"""
Script to synchronize all brands from backend/data/brands.json into PostgreSQL scraping.brands.
Fixes:
- NULL engines for aramis, mercado_livre, amazon, zara, ricardoalmeida, reserva
- Sets proper domains
- Fixes richards wake token
- Fixes domain collisions (zara_infantil)
"""
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

from core.db import db

async def sync():
    seed_file = BACKEND_DIR / "data" / "brands.json"
    with open(seed_file, encoding="utf-8") as f:
        seed_brands = json.load(f)

    await db.connect()
    print("Conectado ao PostgreSQL...")

    # 1. Update zara_infantil domain if present to avoid domain collision on www.zara.com
    await db.execute_raw("UPDATE scraping.brands SET domain = 'kids.zara.com', engine = 'zara' WHERE brand_key = 'zara_infantil';")

    # 2. Iterate each brand in seed and upsert cleanly
    for brand_key, b in seed_brands.items():
        name = b.get("brand_name")
        domain = b.get("domain")
        engine = b.get("engine")
        is_active = b.get("is_active", True)
        config_json = json.dumps(b)

        # Upsert
        query = """
            INSERT INTO scraping.brands (brand_key, brand_name, domain, engine, configuration, is_active)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            ON CONFLICT (brand_key)
            DO UPDATE SET
                brand_name = EXCLUDED.brand_name,
                domain = EXCLUDED.domain,
                engine = EXCLUDED.engine,
                configuration = EXCLUDED.configuration,
                is_active = EXCLUDED.is_active;
        """
        try:
            await db.execute_raw(query, brand_key, name, domain, engine, config_json, is_active)
            print(f"  [OK] Synced {brand_key} (engine={engine}, domain={domain})")
        except Exception as e:
            print(f"  [ERROR] Failed to sync {brand_key}: {e}")

    await db.disconnect()
    print("Sincronização concluída com sucesso!")

if __name__ == "__main__":
    asyncio.run(sync())
