import asyncio
from core.db import db

async def run():
    await db.connect()
    # Oriba: remove calcados-tenis (keep calcados)
    await db.query_raw("""
        DELETE FROM brand_category_mappings 
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'oriba') 
          AND canonical_slug = 'calcados-tenis'
    """)

    # Buckman: remove presentes (keep acessorios)
    await db.query_raw("""
        DELETE FROM brand_category_mappings 
        WHERE brand_id = (SELECT id FROM brands WHERE brand_key = 'bck') 
          AND canonical_slug = 'presentes'
    """)

    await db.disconnect()
    print("Cleaned up duplicate paths in Oriba and Buckman successfully.")

if __name__ == '__main__':
    asyncio.run(run())
