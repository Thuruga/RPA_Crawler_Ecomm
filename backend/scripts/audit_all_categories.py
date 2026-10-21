import asyncio
import json
import re
from typing import Dict, Any, List

from core.db import db
from services.brand_service import brand_service
from api.routes_category import get_categories

CORE_CATEGORIES = {
    "polos": ["polo", "polos"],
    "camisas": ["camisa", "camisas"],
    "camisetas": ["camiseta", "camisetas", "t-shirt", "t-shirts", "tshirt", "tshirts"],
    "calcas": ["calca", "calças", "calcas", "jeans"],
    "bermudas": ["bermuda", "bermudas", "short", "shorts"],
    "casacos_jaquetas": ["casaco", "casacos", "jaqueta", "jaquetas", "moletom", "moletons", "tricot", "tricots", "sueter", "sueteres"],
    "calcados": ["calcado", "calcados", "calçados", "tenis", "tênis", "sapato", "sapatos", "bota", "botas", "chinelo", "sandalia"],
    "acessorios": ["acessorio", "acessorios", "acessórios", "cinto", "cintos", "carteira", "carteiras", "bone", "boné", "mochila"],
}

GENERIC_WORDS = {
    "colecao", "coleção", "promocao", "promoção", "sale", "outlet",
    "home", "todos", "ver todos", "ver tudo", "geral", "novidades",
    "menu", "institucional", "nossas lojas", "lojas"
}

def normalize_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[àáâãä]", "a", t)
    t = re.sub(r"[èéêë]", "e", t)
    t = re.sub(r"[ìíîï]", "i", t)
    t = re.sub(r"[òóôõö]", "o", t)
    t = re.sub(r"[ùúûü]", "u", t)
    t = re.sub(r"[ç]", "c", t)
    return t

async def audit_brand(brand_key: str, brand_name: str) -> Dict[str, Any]:
    try:
        res = await get_categories(brand_key)
        groups = res.get("categories", [])
    except Exception as e:
        return {
            "brand_key": brand_key,
            "brand_name": brand_name,
            "error": str(e),
            "total_categories": 0,
        }

    all_items = []
    for g in groups:
        g_name = g.get("group") or g.get("name") or "Outros"
        for item in g.get("items", []):
            all_items.append({
                "group": g_name,
                "label": item.get("label") or "",
                "path": item.get("path") or "",
            })

    total = len(all_items)
    
    # 1. Duplicates check
    seen_paths = set()
    dup_paths = []
    seen_labels = set()
    dup_labels = []
    
    for it in all_items:
        p = it["path"].strip().rstrip("/").lower()
        l = it["label"].strip().lower()
        if p in seen_paths:
            dup_paths.append(it["path"])
        seen_paths.add(p)
        if l in seen_labels:
            dup_labels.append(it["label"])
        seen_labels.add(l)

    # 2. Suspicious / Strange labels check
    strange_items = []
    for it in all_items:
        label = it["label"].strip()
        path = it["path"].strip()
        norm_l = normalize_text(label)
        
        reasons = []
        if len(label) <= 2:
            reasons.append("label muito curto")
        if "\ufffd" in label or "\ufffd" in path or "?" in label and len(label) > 10:
            reasons.append("caractere corrompido (encoding)")
        if norm_l in GENERIC_WORDS:
            reasons.append("palavra genérica sem contexto")
        if not path or path == "#" or path.startswith("javascript:"):
            reasons.append("path vazio ou inválido")
            
        if reasons:
            strange_items.append({
                "label": label,
                "path": path,
                "reasons": reasons
            })

    # 3. Core category coverage
    combined_corpus = " ".join([normalize_text(it["label"]) + " " + normalize_text(it["path"]) for it in all_items])
    missing_core = []
    for core_key, keywords in CORE_CATEGORIES.items():
        found = any(re.search(rf"\b{kw}\b", combined_corpus) for kw in keywords)
        if not found:
            missing_core.append(core_key)

    return {
        "brand_key": brand_key,
        "brand_name": brand_name,
        "total_categories": total,
        "groups": [g.get("group") or g.get("name") for g in groups],
        "dup_paths_count": len(dup_paths),
        "dup_paths_sample": dup_paths[:5],
        "dup_labels_count": len(dup_labels),
        "dup_labels_sample": dup_labels[:5],
        "strange_count": len(strange_items),
        "strange_sample": strange_items[:8],
        "missing_core": missing_core,
    }

async def main():
    await db.connect()
    await brand_service.load_from_db()
    brands = [b for b in brand_service.list_brands(active_only=True) if not b.is_marketplace]
    
    results = []
    print(f"Auditing {len(brands)} brands...")
    for b in brands:
        res = await audit_brand(b.brand_key, b.brand_name)
        results.append(res)
        print(f"[{b.brand_key}] Total: {res.get('total_categories')} | Strange: {res.get('strange_count', 0)} | DupPaths: {res.get('dup_paths_count', 0)} | MissingCore: {res.get('missing_core', [])}")

    with open("data/categories_audit_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("Salvo em data/categories_audit_results.json")
    await db.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
