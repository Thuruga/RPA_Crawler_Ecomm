import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curl_cffi.requests import AsyncSession
from services.vtex_api_scraper import VtexApiClient

# Domains from brands.json seed
VTEX_BRANDS = [
    ("aramis", "www.aramis.com.br", "roupas/camisas"),
    ("foxton", "www.foxtonbrasil.com.br", "roupas/camisas"),
    ("reserva", "www.usereserva.com", "masculino/camisas"),
    ("ofcreserva", "www.oficinareserva.com", "roupas/camisas"),
    ("hering", "www.hering.com.br", "masculino/camisas"),
    ("levis", "www.levi.com.br", "masculino/camisas"),
    ("calvinklein", "www.calvinklein.com.br", "masculino/camisas"),
    ("zapalla", "www.zapalla.com.br", "roupas/camisas"),
    ("austral", "www.austral.com.br", "roupas/camisas"),
    ("trackfield", "www.tf.com.br", "masculino/camisetas"),
    ("hugoboss", "www.hugoboss.com.br", "masculino/camisas"),
    ("guess", "www.guessbrasil.com.br", "masculino/camisas"),
]

TEST_CEP = "01310100"

async def test_vtex_brand(session, brand_key, domain, path):
    search_url = f"https://{domain}/api/catalog_system/pub/products/search/{path}?_from=0&_to=2"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    try:
        resp = await session.get(search_url, headers=headers)
        if resp.status_code not in (200, 206):
            return {"brand": brand_key, "status": f"SEARCH_HTTP_{resp.status_code}", "error": f"Search returned HTTP {resp.status_code}"}

        data = resp.json()
        if not data or not isinstance(data, list):
            return {"brand": brand_key, "status": "NO_PRODUCTS", "error": "Catalog search returned empty list"}

        prod = data[0]
        items = prod.get("items", [])
        if not items:
            return {"brand": brand_key, "status": "NO_ITEMS", "error": "Product has no items"}

        item = items[0]
        sku_id = str(item.get("itemId"))
        sellers = item.get("sellers", [])
        seller_id = str(sellers[0].get("sellerId", "1")) if sellers else "1"
        prod_name = prod.get("productName", "")

        client = VtexApiClient(brand_name=brand_key)
        client.session = session
        sim = await client.simulate_shipping(sku_id, seller_id, TEST_CEP, domain)

        return {
            "brand": brand_key,
            "product_name": prod_name,
            "sku_id": sku_id,
            "seller_id": seller_id,
            "state": sim.get("state"),
            "options": [
                {
                    "price": getattr(opt, "price", None),
                    "status": getattr(opt, "status", None),
                    "days": getattr(opt, "estimated_delivery_days", None),
                    "service": getattr(opt, "service_name", None),
                }
                for opt in sim.get("shipping_options", [])
            ]
        }
    except Exception as e:
        return {"brand": brand_key, "status": "EXCEPTION", "error": f"{type(e).__name__}: {str(e)}"}

async def main():
    print(f"Testando simulação de frete VTEX para todas as 12 marcas com CEP {TEST_CEP}...")
    results = []
    async with AsyncSession(impersonate="chrome120", timeout=15) as session:
        for bk, dom, path in VTEX_BRANDS:
            print(f"\n---> Testando [{bk}] ({dom})...")
            res = await test_vtex_brand(session, bk, dom, path)
            print(f"     Resultado: {res.get('state') or res.get('status')}")
            if res.get("options"):
                for opt in res["options"][:2]:
                    print(f"        * {opt.get('service')}: R$ {opt.get('price')} ({opt.get('days')} dias)")
            elif res.get("error"):
                print(f"        * Erro: {res['error']}")
            results.append(res)

    print("\n" + "="*80)
    print("RESUMO VTEX:")
    print("="*80)
    for r in results:
        opts_count = len(r.get("options", []))
        st = r.get("state") or r.get("status")
        print(f"[{r['brand']}] {st} - {opts_count} opções de frete")

if __name__ == "__main__":
    asyncio.run(main())
