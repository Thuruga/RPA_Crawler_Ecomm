import asyncio
import aiohttp
import re

async def get_richards_token():
    async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as s:
        async with s.get("https://www.richards.com.br") as resp:
            text = await resp.text()
            match = re.search(r"""storefrontAccessToken\s*:\s*['"]([^'"]+)['"]""", text, re.I)
            if match:
                return match.group(1)
    return None

async def test_wake(token, product_id, brand_name):
    headers = {"TCS-Access-Token": token}
    pq = """
    query Product($productId: Long!) {
      product(productId: $productId) {
        productVariantId
        productName
      }
    }
    """
    sq = """
    query Shipping($cep: CEP, $productVariantId: Long, $quantity: Int) {
      shippingQuotes(cep: $cep, productVariantId: $productVariantId, quantity: $quantity) {
        shippingQuoteId
        name
        value
        deadline
      }
    }
    """
    async with aiohttp.ClientSession() as s:
        async with s.post("https://storefront-api.fbits.net/graphql", json={"query": pq, "variables": {"productId": product_id}}, headers=headers) as r1:
            data1 = await r1.json()
            if not data1.get("data") or not data1["data"].get("product"):
                print(f"[{brand_name}] Could not get product: {data1}")
                return
            p = data1["data"]["product"]
            v_id = p["productVariantId"]
            print(f"[{brand_name}] Product: {p.get('productName')} (Variant: {v_id})")

        async with s.post("https://storefront-api.fbits.net/graphql", json={"query": sq, "variables": {"cep": "01310100", "productVariantId": v_id, "quantity": 1}}, headers=headers) as r2:
            data2 = await r2.json()
            quotes = data2.get("data", {}).get("shippingQuotes", [])
            print(f"[{brand_name}] Shipping quotes count: {len(quotes)}")
            for q in quotes:
                print(f"   -> {q.get('name')}: R$ {q.get('value')} ({q.get('deadline')} dias)")

async def main():
    print("Testing Tommy...")
    await test_wake("tcs_tommy_49b574bea88c408eb3d5a9163aa6b571", 151510, "Tommy")

    print("\nTesting Oriba...")
    await test_wake("tcs_oriba_65e6e326cbcb4419b4fbc80a1028babf", 151714, "Oriba")

    print("\nTesting Richards (auto-extracting token)...")
    token_r = await get_richards_token()
    print("Richards extracted token:", token_r)
    if token_r:
        # Search 1 product in richards
        headers = {"TCS-Access-Token": token_r}
        sq_search = """
        query Search($q: String!) {
          search(query: $q) {
            products(first: 1) {
              edges { node { productId productName } }
            }
          }
        }
        """
        async with aiohttp.ClientSession() as s:
            async with s.post("https://storefront-api.fbits.net/graphql", json={"query": sq_search, "variables": {"q": "camisa"}}, headers=headers) as r:
                data = await r.json()
                edges = data.get("data", {}).get("search", {}).get("products", {}).get("edges", [])
                if edges:
                    pid = edges[0]["node"]["productId"]
                    await test_wake(token_r, pid, "Richards")

if __name__ == "__main__":
    asyncio.run(main())
