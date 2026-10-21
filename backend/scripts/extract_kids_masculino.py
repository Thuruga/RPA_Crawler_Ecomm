import asyncio
import json
import os
import re
import sys
from typing import List, Dict, Any, Optional
import urllib.parse
import pandas as pd
from curl_cffi.requests import AsyncSession

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.product_contract import (
    CANONICAL_PRODUCT_COLUMNS,
    build_canonical_product_row,
    build_canonical_export_dataframe,
    normalize_specifications_aliases,
)
from services.review_service import get_bulk_reviews

# Termos femininos estritos para exclusão
FEMALE_KEYWORDS = [
    "menina", "meninas", "feminina", "femininas", "feminino", 
    "garota", "garotas", "vestido", "vestidos", "saia", "saias", 
    "sutiã", "sutias", "top menina", "calcinha", "calcinhas", 
    "biquini", "biquinis", "biquíni", "biquínis", "tiara", "tiaras", 
    "laço", "laco", "bailarina", "boneca", "macaquinho menina"
]

def clean_text(text: str) -> str:
    if not text:
        return ""
    fixes = {
        "algodo": "algodão",
        "clssica": "clássica",
        "clssico": "clássico",
        "Tafet": "Tafetá",
        "orgnico": "orgânico",
        "crianas": "crianças",
        "vo": "vão",
        "CALES": "CALÇÕES",
        "Matelass": "Matelassê",
        "Tric": "Tricô",
        "Cala": "Calça",
        "Bluso": "Blusão",
        "Coleo": "Coleção",
        "Promoo": "Promoção",
    }
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    return text.strip()

def is_masculine_product(brand: str, name: str, categories: List[str] = None, url: str = "") -> bool:
    text = f"{name} {' '.join(categories or [])} {url}".lower()
    for kw in FEMALE_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', text):
            return False
    if brand == "Hering":
        # Garante que é produto infantil/menino
        if not re.search(r'\b(menino|infantil|kids|bebe|bebê)\b', text):
            return False
    return True

async def fetch_vtex_category(
    session: AsyncSession,
    brand_name: str,
    domain: str,
    category_name: str,
    vtex_paths: List[str],
    max_pages: int = 4
) -> List[Dict[str, Any]]:
    products = []
    chunk_size = 50
    seen_urls = set()

    for path in vtex_paths:
        for page in range(max_pages):
            _from = page * chunk_size
            _to = _from + chunk_size - 1
            sep = '&' if '?' in path else '?'
            url = f"https://{domain}/api/catalog_system/pub/products/search/{path.strip('/')}{sep}_from={_from}&_to={_to}"
            
            try:
                resp = await session.get(url, timeout=20)
                if resp.status_code not in (200, 206):
                    break
                
                items = resp.json()
                if not items or not isinstance(items, list):
                    break

                # Busca avaliações em lote para todos os produtos da página
                pids = [str(it.get("productId")) for it in items if it.get("productId")]
                reviews_map = {}
                try:
                    reviews_map = await get_bulk_reviews(brand_name.lower(), pids)
                except Exception:
                    pass

                for item in items:
                    name = clean_text(item.get("productName", "").strip())
                    categories = item.get("categories", [])
                    link = item.get("link", "")
                    if not link.startswith("http"):
                        link = f"https://{domain}{link}"

                    if link in seen_urls:
                        continue

                    if not is_masculine_product(brand_name, name, categories, link):
                        continue

                    sku_items = item.get("items", [])
                    if not sku_items:
                        continue

                    # Especificações da VTEX
                    specs = {}
                    for k in item.get("allSpecifications", []):
                        v = item.get(k)
                        if v:
                            specs[k] = v[0] if isinstance(v, list) else str(v)

                    normalized_specs = normalize_specifications_aliases(specs)

                    # Preços, tamanhos e estoque
                    avail_sizes = []
                    size_grid = []
                    price_full = None
                    price_discount = None
                    has_availability = False

                    for sku in sku_items:
                        s_name = sku.get("name", "").strip()
                        if " - " in s_name:
                            s_name = s_name.split(" - ")[-1].strip()
                        if s_name and s_name not in size_grid:
                            size_grid.append(s_name)

                        sellers = sku.get("sellers", [])
                        sku_avail = False
                        if sellers:
                            offer = sellers[0].get("commertialOffer", {})
                            if offer.get("AvailableQuantity", 0) > 0:
                                sku_avail = True
                                has_availability = True
                                if price_full is None:
                                    p = offer.get("Price")
                                    lp = offer.get("ListPrice")
                                    if lp and p and lp > p:
                                        price_full = float(lp)
                                        price_discount = float(p)
                                    else:
                                        price_full = float(p or lp or 0)
                                        price_discount = None

                        if sku_avail and s_name and s_name not in avail_sizes:
                            avail_sizes.append(s_name)

                    if price_full is None and sku_items:
                        offer = sku_items[0].get("sellers", [{}])[0].get("commertialOffer", {})
                        p = offer.get("Price")
                        lp = offer.get("ListPrice")
                        if lp and p and lp > p:
                            price_full = float(lp)
                            price_discount = float(p)
                        else:
                            price_full = float(p or lp or 0)

                    if not price_full or price_full <= 0:
                        continue

                    # Cores disponíveis e brutas
                    colors = []
                    for c_key in ["COR", "Cor", "Color", "Cores", "DESC_COR_ORIGINAL"]:
                        if c_key in specs and specs[c_key]:
                            c_val = clean_text(str(specs[c_key]))
                            if c_val and c_val not in colors:
                                colors.append(c_val)
                                break
                    if not colors:
                        for sku in sku_items:
                            for c_key in ["COR", "Cor", "DESC_COR_ORIGINAL"]:
                                if c_key in sku and sku[c_key]:
                                    c_val = sku[c_key][0] if isinstance(sku[c_key], list) else str(sku[c_key])
                                    c_val = clean_text(str(c_val))
                                    if c_val and c_val not in colors:
                                        colors.append(c_val)
                            if not colors and " - " in sku.get("name", ""):
                                prefix = clean_text(sku.get("name").split(" - ")[0].strip())
                                if prefix and prefix not in colors:
                                    colors.append(prefix)

                    # Composição
                    composition = clean_text(
                        normalized_specs.get("composition")
                        or specs.get("COMPOSIÇÃO")
                        or specs.get("Composição")
                        or specs.get("Composicao")
                        or ""
                    )

                    # Gênero e Faixa etária
                    gender = (
                        normalized_specs.get("gender")
                        or specs.get("GÊNERO")
                        or specs.get("Genero")
                        or "Infantil Masculino"
                    )
                    age_group = (
                        normalized_specs.get("age_group")
                        or specs.get("Idade")
                        or specs.get("Faixa Etária")
                        or (specs.get("SUB CATEGORIA") if specs.get("SUB CATEGORIA") == "Infantil" else None)
                        or "kids"
                    )

                    # Grupo do produto
                    product_group = (
                        normalized_specs.get("product_group")
                        or specs.get("Grupo de Produtos Similares")
                        or specs.get("PRODUTO BASE")
                        or item.get("productReference")
                        or str(item.get("productId", ""))
                    )

                    product_code = str(item.get("productId", ""))
                    description = clean_text(item.get("description", "") or "")

                    # Avaliações do mapa em lote
                    rating, review_count = reviews_map.get(product_code, (None, None))

                    row_input = {
                        "brand": brand_name,
                        "url": link,
                        "price_full": price_full,
                        "price_discount": price_discount,
                        "product_name": name,
                        "product_description": description,
                        "composition": composition,
                        "gender": gender,
                        "age_group": age_group,
                        "product_group": product_group,
                        "available_colors": colors,
                        "raw_colors": list(colors),
                        "available_sizes": avail_sizes,
                        "raw_sizes": list(avail_sizes),
                        "size_grid": size_grid,
                        "raw_size_grid": list(size_grid),
                        "product_code": product_code,
                        "category": category_name,
                        "rating": rating,
                        "review_count": review_count,
                        "specifications": specs,
                    }

                    canonical_row = build_canonical_product_row(row_input)
                    products.append(canonical_row)
                    seen_urls.add(link)

                if len(items) < chunk_size:
                    break

                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"[{brand_name}] Erro na pagina {page} de {category_name} ({path}): {e}")
                break

    print(f"[{brand_name}] {category_name:12s}: {len(products)} produtos masculinos extraídos.")
    return products

async def fetch_lacoste_category(
    session: AsyncSession,
    category_name: str,
    cat_facets: List[str]
) -> List[Dict[str, Any]]:
    products = []
    app_id = "CU0IYSHI42"
    api_key = "7e20c005d05fe8f113d8b7e079bf34a1"
    index_name = "products_br_pt"
    url = f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries?x-algolia-agent=Algolia%20for%20vanilla%20JavaScript&x-algolia-application-id={app_id}&x-algolia-api-key={api_key}"
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://www.lacoste.com",
        "Referer": "https://www.lacoste.com/"
    }

    gender_facets = ["genderLabel:Meninos", "genderLabel:Infantil", "genderLabel:Unisex Infantil"]
    facet_filters = [gender_facets, cat_facets]

    params = urllib.parse.urlencode({
        "query": "",
        "hitsPerPage": 100,
        "facetFilters": json.dumps(facet_filters)
    })

    payload = {
        "requests": [
            {
                "indexName": index_name,
                "params": params
            }
        ]
    }

    try:
        resp = await session.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            hits = data.get("results", [{}])[0].get("hits", [])
            seen_urls = set()

            for item in hits:
                raw_title = item.get("name", "").strip()
                title = clean_text(re.sub(r'<[^>]+>', '', raw_title))

                url_master = item.get("urlMaster", "")
                if url_master and url_master.startswith("/"):
                    prod_url = f"https://www.lacoste.com{url_master}"
                else:
                    prod_url = url_master or ""

                if not is_masculine_product("Lacoste", title, [], prod_url):
                    continue

                if prod_url in seen_urls:
                    continue

                price_dict = item.get("price", {})
                p_min = None
                p_max = None

                if "min" in price_dict:
                    p_min = float(price_dict["min"])
                elif "BRL" in price_dict:
                    p_min = float(price_dict["BRL"])

                if "max" in price_dict and price_dict["max"]:
                    p_max = float(price_dict["max"])
                elif "initialPriceMax" in price_dict and price_dict["initialPriceMax"]:
                    p_max = float(price_dict["initialPriceMax"])

                if not p_min and not p_max:
                    continue

                if p_max and p_min and p_max > p_min:
                    price_full = p_max
                    price_discount = p_min
                else:
                    price_full = p_min if p_min is not None else p_max
                    price_discount = None

                # Cores
                colors = [clean_text(c.get("label")) for c in item.get("colors", []) if isinstance(c, dict) and c.get("label")]
                if not colors and item.get("colorLabel"):
                    colors = [clean_text(item.get("colorLabel"))]

                # Tamanhos
                sizes = [str(s).strip() for s in item.get("sizes", []) if s]

                # Descrição e Composição
                description = clean_text(item.get("description", "") or "")
                composition = clean_text(item.get("bulletpoint1") or item.get("composition") or "")

                # Gênero e Faixa etária
                gender_raw = item.get("genderLabel", [])
                gender = gender_raw[0] if isinstance(gender_raw, list) and gender_raw else str(gender_raw or "Meninos")
                age_group = "kids"

                # Código do produto e grupo
                pid = str(item.get("productId") or item.get("objectID") or "")
                product_group = pid.rsplit("-", 1)[0] if "-" in pid else pid

                row_input = {
                    "brand": "Lacoste",
                    "url": prod_url,
                    "price_full": price_full,
                    "price_discount": price_discount,
                    "product_name": title,
                    "product_description": description,
                    "composition": composition,
                    "gender": gender,
                    "age_group": age_group,
                    "product_group": product_group,
                    "available_colors": colors,
                    "raw_colors": list(colors),
                    "available_sizes": sizes,
                    "raw_sizes": list(sizes),
                    "size_grid": sizes,
                    "raw_size_grid": list(sizes),
                    "product_code": pid,
                    "category": category_name,
                    "rating": item.get("rating"),
                    "review_count": item.get("review_count"),
                }

                canonical_row = build_canonical_product_row(row_input)
                products.append(canonical_row)
                seen_urls.add(prod_url)

    except Exception as e:
        print(f"[Lacoste] Erro na categoria {category_name}: {e}")

    print(f"[Lacoste] {category_name:12s}: {len(products)} produtos masculinos extraídos.")
    return products

async def main():
    print("Iniciando extração infantil masculina para Hering, Reserva e Lacoste...")
    all_products = []

    categories = [
        "Camisetas",
        "Polos",
        "Bermudas",
        "Calças",
        "Casacos",
        "Camisas"
    ]

    hering_paths = {
        "Camisetas": ["camisetas?ft=menino", "kids/menino/camisetas-e-regatas"],
        "Polos": ["polos?ft=menino", "kids/menino/polos"],
        "Bermudas": ["bermudas?ft=menino", "shorts?ft=menino", "kids/menino/shorts-e-bermudas"],
        "Calças": ["calcas?ft=menino", "kids/menino/calcas"],
        "Casacos": ["jaquetas?ft=menino", "blusoes?ft=menino", "kids/menino/blusoes-e-moletons"],
        "Camisas": ["camisas?ft=menino", "kids/menino/camisas"]
    }

    reserva_paths = {
        "Camisetas": ["mini/infantil/camisetas"],
        "Polos": ["mini/infantil/polos"],
        "Bermudas": ["mini/infantil/bermudas"],
        "Calças": ["mini/infantil/calcas"],
        "Casacos": ["mini/infantil/casacos"],
        "Camisas": ["mini/infantil/camisas"]
    }

    lacoste_facets = {
        "Camisetas": ["categoryLabel:TEE-SHIRTS & GOLAS ALTAS"],
        "Polos": ["categoryLabel:POLOS"],
        "Bermudas": ["categoryLabel:CAL\ufffdES", "categoryLabel:CALÇÕES", "categoryLabel:BERMUDAS"],
        "Calças": ["categoryLabel:CALCAS"],
        "Casacos": ["categoryLabel:SWEATS", "categoryLabel:PULLOVER"],
        "Camisas": ["categoryLabel:CAMISA"]
    }

    async with AsyncSession(impersonate="chrome", timeout=25) as session:
        # 1. HERING
        print("\n--- HERING KIDS (MASCULINO) ---")
        for cat in categories:
            paths = hering_paths[cat]
            prods = await fetch_vtex_category(session, "Hering", "www.hering.com.br", cat, paths, max_pages=3)
            all_products.extend(prods)

        # 2. RESERVA MINI
        print("\n--- RESERVA MINI (MASCULINO) ---")
        for cat in categories:
            paths = reserva_paths[cat]
            prods = await fetch_vtex_category(session, "Reserva", "www.usereserva.com", cat, paths, max_pages=4)
            all_products.extend(prods)

        # 3. LACOSTE
        print("\n--- LACOSTE INFANTIL (MASCULINO) ---")
        for cat in categories:
            facets = lacoste_facets[cat]
            prods = await fetch_lacoste_category(session, cat, facets)
            all_products.extend(prods)

    # 4. ZARA
    print("\n--- ZARA INFANTIL (MASCULINO) ---")
    try:
        from scripts.extract_kids_zara import extract_zara_kids
        zara_prods = await extract_zara_kids()
        all_products.extend(zara_prods)
    except Exception as e:
        print(f"[Zara] Erro na extração da Zara: {e}")

    print(f"\n==========================================")
    print(f"Total de produtos extraídos (4 marcas): {len(all_products)}")
    print(f"==========================================")

    # DataFrame canônico padronizado
    df = build_canonical_export_dataframe(all_products)
    
    # Cria pasta de dados se não existir
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    exports_dir = os.path.join(base_dir, "data", "exports")
    os.makedirs(exports_dir, exist_ok=True)
    
    excel_path = os.path.join(exports_dir, "extracao_infantil_masculino.xlsx")
    json_path = os.path.join(exports_dir, "extracao_infantil_masculino.json")
    all_brands_excel = os.path.join(exports_dir, "extracao_infantil_todas_marcas.xlsx")

    df.to_excel(excel_path, index=False)
    df.to_excel(all_brands_excel, index=False)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_products, f, ensure_ascii=False, indent=2)

    print(f"Salvo em Excel: {excel_path}")
    print(f"Salvo em JSON:  {json_path}")
    print(f"Colunas presentes ({len(df.columns)}): {list(df.columns)}")

    # Estatísticas
    summary = df.groupby(["brand", "category"]).agg(
        qtd=("product_name", "count"),
        preco_medio=("price_full", "mean"),
        preco_min=("price_full", "min"),
        preco_max=("price_full", "max"),
    ).reset_index()

    summary_path = os.path.join(exports_dir, "resumo_infantil_masculino.json")
    summary.to_json(summary_path, orient="records", force_ascii=False, indent=2)

    print("\nResumo por Marca e Categoria:")
    print(summary.to_string(index=False))

if __name__ == "__main__":
    asyncio.run(main())
