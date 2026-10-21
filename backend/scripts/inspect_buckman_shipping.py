import asyncio
import re
from curl_cffi.requests import AsyncSession

async def main():
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get("https://buckmanbck.com.br/products.json?limit=1")
        p = r.json()["products"][0]
        handle = p["handle"]
        variant_id = p["variants"][0]["id"]
        print(f"Product handle: {handle}, Variant: {variant_id}")
        
        r2 = await s.get(f"https://buckmanbck.com.br/products/{handle}")
        html = r2.text
        print(f"HTML length: {len(html)}")
        
        # Check script tags
        scripts = re.findall(r'<script[^>]*src=[\'"]([^\'"]+)[\'"]', html)
        for sc in scripts:
            if any(k in sc.lower() for k in ["frete", "ship", "cep", "yampi", "cartpanda", "frenet", "melhor", "kangu"]):
                print("App script found:", sc)
                
        # Check inline script mentions of shipping endpoints
        endpoints = re.findall(r'[\'"](https?://[^\'"]*(?:frete|shipping|calc|cep)[^\'"]*)[\'"]', html, re.I)
        print("Endpoints mentioned:", set(endpoints))
        
        # Check form action or checkout buttons
        actions = re.findall(r'action=[\'"]([^\'"]+)[\'"]', html)
        print("Form actions:", actions)

asyncio.run(main())
