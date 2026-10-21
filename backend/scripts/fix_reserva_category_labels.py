import asyncio
import re
from prisma import Prisma

TRANSLATIONS = {
    "bermudas-e-shorts": "Bermudas e Shorts",
    "calcas": "Calças",
    "camisas": "Camisas",
    "camisetas": "Camisetas",
    "casacos-e-jaquetas": "Casacos e Jaquetas",
    "coletes": "Coletes",
    "cuecas": "Cuecas",
    "meias": "Meias",
    "polos": "Polos",
    "sungas": "Sungas",
    "chinelos": "Chinelos",
    "sandalias-e-papetes": "Sandálias e Papetes",
    "tenis": "Tênis",
    "social": "Social",
    "botas": "Botas",
    "acessorios": "Acessórios",
    "moletom": "Moletons",
    "moletons": "Moletons",
    "tricots": "Tricots e Suéteres",
    "tricots-e-sueteres": "Tricots e Suéteres",
    "underwear": "Underwear",
}

def derive_clean_label(path: str, current_label: str) -> str:
    if current_label not in ("Coleção", "Promoção", "Colecao", "Promocao"):
        return current_label

    parts = [p.strip().lower() for p in path.split("/") if p.strip()]
    if not parts:
        return current_label

    suffix = "Coleção" if "colec" in parts[-1] else ("Promoção" if "promoc" in parts[-1] else current_label)
    parent_slug = parts[-2] if len(parts) >= 2 else parts[-1]
    
    parent_name = TRANSLATIONS.get(parent_slug, parent_slug.replace("-", " ").title())
    return f"{parent_name} ({suffix})"

async def run():
    db = Prisma()
    await db.connect()
    try:
        rows = await db.query_raw(
            "SELECT id, canonical_slug, source_path, label FROM scraping.brand_category_mappings WHERE label IN ('Coleção', 'Promoção', 'Colecao', 'Promocao');"
        )
        print(f"Encontrados {len(rows)} mapeamentos genéricos.")
        updated_count = 0
        for r in rows:
            path = r.get("source_path") or r.get("canonical_slug") or ""
            old_label = r.get("label") or ""
            new_label = derive_clean_label(path, old_label)
            if new_label != old_label:
                await db.execute_raw(
                    "UPDATE scraping.brand_category_mappings SET label = $1, updated_at = NOW() WHERE id = $2;",
                    new_label,
                    r["id"]
                )
                updated_count += 1
                print(f"ID {r['id']}: '{old_label}' -> '{new_label}' ({path})")
        print(f"{updated_count} registros atualizados no banco.")
    finally:
        await db.disconnect()

if __name__ == "__main__":
    asyncio.run(run())
