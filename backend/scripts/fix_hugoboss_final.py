import asyncio
from core.db import db

async def run():
    await db.connect()
    # Hugo Boss: Ternos - manter o relativo (/masculino/roupas/ternos), apagar o absoluto que é subcategoria
    # /ternos/ternos é a subcategoria de terno completo, /ternos é a categoria mãe
    await db.query_raw(
        "UPDATE brand_category_mappings SET label = 'Ternos (linha completa)' WHERE id = 1134"
    )
    print("Hugo Boss: id=1134 'Ternos' (subcategoria) -> 'Ternos (linha completa)'")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(run())
