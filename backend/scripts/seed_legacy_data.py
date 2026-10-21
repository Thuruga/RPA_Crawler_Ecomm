import asyncio
import json
import logging
from pathlib import Path
from prisma import Prisma
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / '.env')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def seed_data():
    db = Prisma()
    await db.connect()
    
    try:
        # 1. Brands
        brands_file = Path(__file__).parent.parent / 'data' / 'brands.json'
        if brands_file.exists():
            with open(brands_file, 'r', encoding='utf-8') as f:
                brands = json.load(f)
            
            logger.info(f"Seeding {len(brands)} brands...")
            for b in brands.values():
                await db.brands.upsert(
                    where={'brand_key': b['brand_key']},
                    data={
                        'create': {
                            'brand_key': b['brand_key'],
                            'brand_name': b['brand_name'],
                            'domain': b['domain'],
                            'engine': b.get('engine'),
                            'is_active': b.get('is_active', True),
                            'configuration': json.dumps(b),
                        },
                        'update': {
                            'brand_name': b['brand_name'],
                            'domain': b['domain'],
                            'engine': b.get('engine'),
                            'is_active': b.get('is_active', True),
                            'configuration': json.dumps(b),
                        }
                    }
                )
        
        # 2. Competitor Catalog (Scan Products)
        # Note: Depending on JSON format, map fields accordingly
        catalog_files = list((Path(__file__).parent.parent / 'data').glob('*catalog*.json'))
        for cfile in catalog_files:
            with open(cfile, 'r', encoding='utf-8') as f:
                catalog = json.load(f)
            
            logger.info(f"Seeding {len(catalog)} products from {cfile.name}...")
            # For each product, use create_many or upsert if id exists
            # Skipped full mapping as JSON shape varies. Add specifics as needed.
            
    finally:
        await db.disconnect()

if __name__ == '__main__':
    asyncio.run(seed_data())
