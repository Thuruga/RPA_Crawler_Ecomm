import asyncio
import httpx

async def test():
    async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for domain, path in [("www.foxtonbrasil.com.br", "roupas/camisas"), ("www.oriba.com.br", "camisa"), ("www.hugoboss.com.br", "boss/masculino/camisas")]:
            url = f"https://{domain}/_v/api/intelligent-search/product_search/{path}?page=1&count=2"
            r = await c.get(url)
            print(f"{domain} status: {r.status_code}")
            if r.status_code == 200:
                data = r.json()
                print("  keys:", list(data.keys()) if isinstance(data, dict) else len(data))
                if isinstance(data, dict) and "products" in data:
                    prods = data["products"]
                    print(f"  products count: {len(prods)}")
                    if prods:
                        p0 = prods[0]
                        print("  p0 sample:", p0.get("productName"), "| link:", p0.get("linkText"))

if __name__ == '__main__':
    asyncio.run(test())
