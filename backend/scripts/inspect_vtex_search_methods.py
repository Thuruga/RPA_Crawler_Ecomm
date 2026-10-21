import asyncio
import httpx

async def run():
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        # Test Aramis:
        # Method 1: catalog_system/pub/products/search/roupas/camisas
        r1 = await c.get("https://www.aramis.com.br/api/catalog_system/pub/products/search/roupas/camisas?_from=0&_to=5")
        print("Aramis Method 1 (/roupas/camisas in path):", r1.status_code, len(r1.json()) if r1.status_code == 200 else "err")

        # Method 2: catalog_system/pub/products/search?ft=camisa
        r2 = await c.get("https://www.aramis.com.br/api/catalog_system/pub/products/search?ft=camisa&_from=0&_to=5")
        print("Aramis Method 2 (?ft=camisa):", r2.status_code, len(r2.json()) if r2.status_code == 200 else "err")

        # Method 3: catalog_system/pub/products/search?fq=C:/480/507/
        r3 = await c.get("https://www.aramis.com.br/api/catalog_system/pub/products/search?fq=C:/480/507/&_from=0&_to=5")
        print("Aramis Method 3 (?fq=C:/480/507/):", r3.status_code, len(r3.json()) if r3.status_code == 200 else "err")

        # Method 4: intelligent-search/product_search/roupas/camisas
        r4 = await c.get("https://www.aramis.com.br/_v/api/intelligent-search/product_search/roupas/camisas?count=5")
        print("Aramis Method 4 (intelligent-search):", r4.status_code, len(r4.json().get('products', [])) if r4.status_code == 200 else "err")

        # Test Track & Field:
        # Method 1: catalog_system /calcados/tenis
        rt1 = await c.get("https://www.tf.com.br/api/catalog_system/pub/products/search/calcados/tenis?_from=0&_to=5")
        print("\nTF Method 1 (/calcados/tenis in path):", rt1.status_code, len(rt1.json()) if rt1.status_code == 200 else "err")

        # Method 2: catalog_system ?fq=C:/calcados/tenis
        rt2 = await c.get("https://www.tf.com.br/api/catalog_system/pub/products/search?ft=tenis&_from=0&_to=5")
        print("TF Method 2 (?ft=tenis):", rt2.status_code, len(rt2.json()) if rt2.status_code == 200 else "err")

        # Method 4: intelligent-search
        rt4 = await c.get("https://www.tf.com.br/_v/api/intelligent-search/product_search/calcados/tenis?count=5")
        print("TF Method 4 (intelligent-search):", rt4.status_code, len(rt4.json().get('products', [])) if rt4.status_code == 200 else "err")

if __name__ == '__main__':
    asyncio.run(run())
