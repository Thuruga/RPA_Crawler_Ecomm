import asyncio
import re
import json
from curl_cffi.requests import AsyncSession

async def main():
    async with AsyncSession(impersonate="chrome120") as s:
        target = "https://www.brooksfield.com.br/paleto-xadrez-gold-ptg-plrecfa25010-p2501977"
        r = await s.get(target)
        idx = r.text.find('var product =')
        if idx != -1:
            end = r.text.find('</script>', idx)
            line = r.text[idx + len('var product ='):end].strip().rstrip(';')
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(line)
            print("Product keys:", list(data.keys()))
            for it in (data.get("Items") or []):
                stock = float(it.get("StockBalance") or 0)
                pid = it.get("ProductID")
                print(f"Item ProductID: {pid} | SKU: {it.get('SKU')} | Name: {it.get('Name')} | Stock: {stock}")
                if stock > 0 and pid != data.get("ProductID"):
                    print(f"--> Testing shipping on in-stock variation ProductID={data.get('ProductID')} SkuID={pid}...")
                    params = {
                        "ProductID": data.get("ProductID"),
                        "SkuID": pid,
                        "PostalCode": "01310-100",
                        "Template": "wd.product.deliveryfee.result",
                        "nocache": "123",
                    }
                    r_fee = await s.get("https://www.brooksfield.com.br/widget/product_deliveryfee", params=params, headers={"X-Requested-With": "XMLHttpRequest", "Referer": target})
                    print("FULL FEE RESPONSE:\n", r_fee.text)
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r_fee.text, 'html.parser')
                    for item in soup.find_all(class_='item'):
                        print("OPTION FOUND:", ' '.join(item.get_text(' ', strip=True).split()))
                    break

asyncio.run(main())
