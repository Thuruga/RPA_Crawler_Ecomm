import asyncio
import re
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

async def inspect_dafiti():
    print("=== INSPECTING DAFITI ===")
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get("https://www.dafiti.com.br/calcados-masculinos/")
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select(".product-box")
        if not cards:
            print("No cards found on Dafiti catalog")
            return
        link = cards[0].select_one("a")
        url = link.get("href") if link else None
        print("Dafiti Sample URL:", url)
        if url:
            pdp = await s.get(url)
            print("PDP Status:", pdp.status_code)
            pdp_soup = BeautifulSoup(pdp.text, "html.parser")
            # Look for scripts referencing product-delivery-time
            scripts = pdp_soup.find_all("script")
            for sc in scripts:
                txt = sc.text or ""
                if "delivery-time" in txt or "delivery_time" in txt or "shipping" in txt.lower():
                    for line in txt.split("\n"):
                        if any(k in line.lower() for k in ["delivery", "shipping", "frete", "postal"]):
                            print("   Script line:", line.strip()[:150])
            
            # Check for shipping widget or container
            containers = pdp_soup.select("[class*='frete'], [id*='frete'], [class*='shipping'], [id*='shipping'], [class*='delivery'], [id*='delivery']")
            print(f"Found {len(containers)} shipping-related containers:")
            for c in containers[:8]:
                print("  Container:", c.name, c.get("class"), c.get("id"), c.get_text(" ", strip=True)[:100])

async def inspect_lacoste():
    print("\n=== INSPECTING LACOSTE ===")
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get("https://www.lacoste.com/br/")
        print("Lacoste Home Status:", r.status_code)

async def inspect_privalia():
    print("\n=== INSPECTING PRIVALIA ===")
    async with AsyncSession(impersonate="chrome120") as s:
        r = await s.get("https://brandsplace.privalia.com/")
        print("Privalia Brandsplace Status:", r.status_code)

async def main():
    await inspect_dafiti()
    await inspect_lacoste()
    await inspect_privalia()

if __name__ == "__main__":
    asyncio.run(main())
