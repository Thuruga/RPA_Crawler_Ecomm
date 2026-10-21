import asyncio
import json
import os
import re
import sys
import time
from typing import List, Dict, Any, Optional
import pandas as pd
from curl_cffi.requests import AsyncSession

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.product_contract import (
    CANONICAL_PRODUCT_COLUMNS,
    build_canonical_product_row,
    build_canonical_export_dataframe,
)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

FEMALE_KEYWORDS = [
    "menina", "meninas", "feminina", "femininas", "feminino", 
    "garota", "garotas", "vestido", "vestidos", "saia", "saias", 
    "sutiã", "sutias", "top menina", "calcinha", "calcinhas", 
    "biquini", "biquinis", "biquíni", "biquínis", "tiara", "tiaras", 
    "laço", "laco", "bailarina", "boneca"
]

NON_CLOTHING_FAMILIES = {
    "SPORT SHOES", "TRAINERS", "HIGH TOPS", "SHOES", "ANKLE BOOT", "RAIN BOOT",
    "SOCKS", "UNDERWEAR", "EAU DE TOILETTE", "HAND BAG-RUCKSACK", "HAT", 
    "NIGHTIE/PYJAMAS", "SWIMSUIT"
}

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = str(text)
    fixes = {
        "algodo": "algodão",
        "clssica": "clássica",
        "clssico": "clássico",
        "orgnico": "orgânico",
        "crianas": "crianças",
        "Cala": "Calça",
        "Bluso": "Blusão",
        "Coleo": "Coleção",
        "Promoo": "Promoção",
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    return re.sub(r'\s+', ' ', text).strip()

def classify_category(name: str, family: str, subfamily: str, seo_kw: str) -> Optional[str]:
    text = f"{name} {family or ''} {subfamily or ''} {seo_kw or ''}".lower()
    
    # 1. Polos
    if "polo" in text:
        return "Polos"
        
    # 2. Camisetas
    if family == "T-SHIRT" or any(w in text for w in ["camiseta", "t-shirt", "tshirt", "regata"]):
        return "Camisetas"
        
    # 3. Camisas
    if family == "SHIRT" or "camisa" in text or "sobrecamisa" in text:
        return "Camisas"
        
    # 4. Bermudas
    if family == "BERMUDA" or any(w in text for w in ["bermuda", "calção", "calcao", "short"]):
        return "Bermudas"
        
    # 5. Calças
    if family == "TROUSERS" or any(w in text for w in ["calça", "calca", "jeans", "jogger", "chino"]):
        return "Calças"
        
    # 6. Casacos
    if family in ("SWEATSHIRT", "OUTERWEAR", "SWEATER", "CARDIGAN", "BLAZER", "WIND-JACKET") or any(
        w in text for w in [
            "casaco", "jaqueta", "moletom", "blusão", "blusao", "suéter", "sueter", 
            "cardigan", "sobretudo", "corta-vento", "blazer", "anorak", "parka", "colete"
        ]
    ):
        return "Casacos"
        
    return None

def is_masculine_product(name: str, url: str = "") -> bool:
    text = f"{name} {url}".lower()
    for kw in FEMALE_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', text):
            return False
    return True

async def fetch_with_akamai_bypass(session: AsyncSession, url: str) -> str:
    r1 = await session.get(url, timeout=20)
    if "bm-verify" not in r1.text:
        return r1.text

    mi = re.search(r'var i = (\d+);\s*var j = i \+ Number\("(\d+)" \+ "(\d+)"\);', r1.text)
    mbm = re.search(r'"bm-verify":\s*"([^"]+)"', r1.text)
    if mi and mbm:
        i_val = int(mi.group(1))
        n_val = int(mi.group(2) + mi.group(3))
        j_val = i_val + n_val
        bm_verify = mbm.group(1)
        
        post_url = "https://www.zara.com/_sec/verify?provider=interstitial"
        payload = {"bm-verify": bm_verify, "pow": j_val}
        post_headers = {
            "Referer": url,
            "Content-Type": "application/json",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        await session.post(post_url, json=payload, headers=post_headers, timeout=15)
        r3 = await session.get(url, timeout=20)
        return r3.text
    return r1.text

async def enrich_pdp(session: AsyncSession, sem: asyncio.Semaphore, product: Dict[str, Any]):
    url = product["url"]
    async with sem:
        try:
            r = await session.get(url, timeout=20)
            if r.status_code != 200:
                return

            m = re.search(r'window\.zara\.viewPayload\s*=\s*(\{.*?\});', r.text, re.DOTALL)
            if not m:
                return

            payload = json.loads(m.group(1))
            prod = payload.get("product", {})
            detail = prod.get("detail", {})
            
            # 1. Composição
            det_comp = detail.get("detailedComposition", {})
            parts = det_comp.get("parts", [])
            materials = []
            for p in parts:
                for c in p.get("components", []):
                    pct = c.get("percentage", "")
                    mat = c.get("material", "")
                    if mat:
                        materials.append(f"{pct} {mat}".strip())
            if materials:
                product["composition"] = clean_text(", ".join(materials))

            # 2. Descrição detalhada
            colors = detail.get("colors", [])
            if colors:
                desc = colors[0].get("description") or colors[0].get("rawDescription") or ""
                if desc:
                    product["product_description"] = clean_text(desc)

            # 3. Grade de tamanhos e tamanhos disponíveis
            sizes_avail = []
            sizes_grid = []
            
            # Extrai das variantes de cores ou tamanhos do payload
            for color in colors:
                for size in color.get("sizes", []):
                    s_name = size.get("name")
                    if not s_name:
                        continue
                    s_name = str(s_name).strip()
                    if s_name not in sizes_grid:
                        sizes_grid.append(s_name)
                    # Checa disponibilidade
                    avail = size.get("availability")
                    if avail and avail.lower() in ("in_stock", "low_on_stock", "back_soon", "true"):
                        if s_name not in sizes_avail:
                            sizes_avail.append(s_name)
                    elif avail is None or avail == "":
                        if s_name not in sizes_avail:
                            sizes_avail.append(s_name)

            # Se não encontrou no payload, tenta nos scripts de JSON-LD
            if not sizes_grid:
                soup_sizes = re.findall(r'"size":\s*"([^"]+)"', r.text)
                for s in soup_sizes:
                    s_clean = s.strip()
                    if s_clean and s_clean not in sizes_grid:
                        sizes_grid.append(s_clean)
                        sizes_avail.append(s_clean)

            if sizes_avail:
                product["available_sizes"] = sizes_avail
                product["raw_sizes"] = list(sizes_avail)
            if sizes_grid:
                product["size_grid"] = sizes_grid
                product["raw_size_grid"] = list(sizes_grid)

            # 4. Código do produto / referência
            disp_ref = detail.get("displayReference")
            if disp_ref:
                product["product_group"] = disp_ref
            
            ref = detail.get("reference")
            if ref:
                product["product_code"] = ref

        except Exception as e:
            # Falha tolerante na PDP: os dados já preenchidos na listagem permanecem
            pass

async def extract_zara_kids() -> List[Dict[str, Any]]:
    print("==================================================")
    print("Iniciando extração da Zara Infantil (Menino)...")
    print("==================================================")
    
    start_time = time.time()
    raw_products_map: Dict[str, Dict[str, Any]] = {}

    async with AsyncSession(impersonate="chrome120", headers=HEADERS) as session:
        # 1. Estabelece bypass Akamai
        print("\n[1/3] Estabelecendo sessão e bypass Akamai na Zara...")
        await fetch_with_akamai_bypass(session, "https://www.zara.com/br/pt/kids-boy-collection-l5413.html")
        print("Bypass Akamai estabelecido com sucesso!")

        # 2. Varredura das páginas da coleção completa
        print("\n[2/3] Varrendo catálogo completo da coleção Menino Infantil...")
        max_pages = 15
        for page in range(1, max_pages + 1):
            url = f"https://www.zara.com/br/pt/kids-boy-collection-l5413.html?page={page}"
            try:
                r = await session.get(url, timeout=20)
                m = re.search(r'window\.zara\.viewPayload\s*=\s*(\{.*?\});', r.text, re.DOTALL)
                if not m:
                    print(f"  Página {page}: sem viewPayload, finalizando paginação.")
                    break
                
                data = json.loads(m.group(1))
                pag = data.get("paginationInfo", {})
                pgs = data.get("productGroups", [])
                
                new_in_page = 0
                for pg in pgs:
                    for el in pg.get("elements", []):
                        for comp in el.get("commercialComponents", []):
                            name = comp.get("name")
                            if not name:
                                continue

                            family = comp.get("familyName")
                            subfamily = comp.get("subfamilyName")
                            if family in NON_CLOTHING_FAMILIES:
                                continue

                            seo = comp.get("seo") or {}
                            kw = seo.get("keyword")
                            pid = seo.get("seoProductId")
                            if not kw or not pid:
                                continue

                            prod_url = f"https://www.zara.com/br/pt/{kw}-p{pid}.html"
                            clean_name = clean_text(name)
                            
                            if not is_masculine_product(clean_name, prod_url):
                                continue

                            category = classify_category(clean_name, family, subfamily, kw)
                            if not category:
                                continue

                            # Preço
                            price_val = comp.get("price")
                            if not price_val or not isinstance(price_val, (int, float)) or price_val <= 0:
                                continue

                            price_full = price_val / 100.0
                            old_price_val = comp.get("oldPrice")
                            price_discount = None
                            if old_price_val and isinstance(old_price_val, (int, float)) and old_price_val > price_val:
                                price_discount = price_full
                                price_full = old_price_val / 100.0

                            # Cores
                            colors = []
                            detail = comp.get("detail") or {}
                            for c in detail.get("colors", []):
                                cname = c.get("name")
                                if cname:
                                    clean_c = clean_text(cname)
                                    if clean_c and clean_c not in colors:
                                        colors.append(clean_c)
                            if not colors and comp.get("availableColors"):
                                for c in comp.get("availableColors"):
                                    cname = c.get("colorName")
                                    if cname:
                                        clean_c = clean_text(cname)
                                        if clean_c and clean_c not in colors:
                                            colors.append(clean_c)

                            ref = comp.get("reference") or str(pid)
                            disp_ref = detail.get("displayReference") or ref

                            if ref not in raw_products_map:
                                raw_products_map[ref] = {
                                    "brand": "Zara",
                                    "url": prod_url,
                                    "price_full": float(price_full),
                                    "price_discount": float(price_discount) if price_discount else None,
                                    "product_name": clean_name,
                                    "product_description": clean_name,
                                    "composition": "",
                                    "gender": "Infantil Masculino",
                                    "age_group": "kids",
                                    "product_group": disp_ref,
                                    "available_colors": colors,
                                    "raw_colors": list(colors),
                                    "available_sizes": [],
                                    "raw_sizes": [],
                                    "size_grid": [],
                                    "raw_size_grid": [],
                                    "product_code": ref,
                                    "category": category,
                                    "rating": None,
                                    "review_count": None,
                                }
                                new_in_page += 1
                            else:
                                for c in colors:
                                    if c not in raw_products_map[ref]["available_colors"]:
                                        raw_products_map[ref]["available_colors"].append(c)
                                        raw_products_map[ref]["raw_colors"].append(c)

                print(f"  Página {page:2d}: {new_in_page:3d} novos produtos das 6 categorias mapeados. (Total acumulado: {len(raw_products_map)})")
                
                if pag.get("isLastPage"):
                    print(f"  Alcançada última página ({page}).")
                    break

                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"  Erro ao varrer página {page}: {e}")
                break

        # 3. Enriquecimento concorrente de PDPs
        products_list = list(raw_products_map.values())
        print(f"\n[3/3] Enriquecendo {len(products_list)} produtos com dados de PDP (composição, tamanhos, descrição)...")
        
        sem = asyncio.Semaphore(10)
        pdp_tasks = [enrich_pdp(session, sem, p) for p in products_list]
        
        chunk_size = 50
        for i in range(0, len(pdp_tasks), chunk_size):
            chunk = pdp_tasks[i:i+chunk_size]
            await asyncio.gather(*chunk)
            done_count = min(i + chunk_size, len(pdp_tasks))
            print(f"  Progresso PDP: {done_count}/{len(pdp_tasks)} produtos enriquecidos...")

    # Formatação final nas 20 colunas canônicas
    canonical_products = []
    for p in products_list:
        row = build_canonical_product_row(p)
        canonical_products.append(row)

    elapsed = time.time() - start_time
    print(f"\nExtração da Zara concluída em {elapsed:.1f} segundos!")
    print(f"Total de produtos infantis masculinos extraídos: {len(canonical_products)}")
    
    return canonical_products

async def main():
    products = await extract_zara_kids()
    
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    exports_dir = os.path.join(base_dir, "data", "exports")
    os.makedirs(exports_dir, exist_ok=True)
    excel_path = os.path.join(exports_dir, "extracao_infantil_zara.xlsx")
    json_path = os.path.join(exports_dir, "extracao_infantil_zara.json")
    summary_path = os.path.join(exports_dir, "resumo_infantil_zara.json")

    df = build_canonical_export_dataframe(products)
    df.to_excel(excel_path, index=False)
    
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    summary = df.groupby(["brand", "category"]).agg(
        qtd=("product_name", "count"),
        preco_medio=("price_full", "mean"),
        preco_min=("price_full", "min"),
        preco_max=("price_full", "max"),
    ).reset_index()

    summary.to_json(summary_path, orient="records", force_ascii=False, indent=2)

    print("\n" + "="*50)
    print("RESUMO DA EXTRAÇÃO ZARA INFANTIL:")
    print("="*50)
    print(summary.to_string(index=False))
    print(f"\nSalvo em Excel: {excel_path}")
    print(f"Salvo em JSON:  {json_path}")
    print(f"Colunas ({len(df.columns)}): {list(df.columns)}")

if __name__ == "__main__":
    asyncio.run(main())
