"""
add_calcados_categories.py
===========================
Busca categorias de calçados reais dos sites de TF, Oriba e Buckman
e insere no banco brand_category_mappings.

Marca por marca:
  - Track & Field: tem CALÇADOS MASCULINOS com Tênis, Meias, Chinelos
  - Guess Brasil: NÃO tem calçados masculinos (confirmado via API)
  - Buckman: é marca de calçados – tem site próprio
  - Oriba: é marca de moda casual – verificar
  - Levi's: não tem calçados no site BR (apenas roupas)
"""
import asyncio
import logging
import httpx
from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


async def fetch_vtex_category_tree(base_url: str) -> list:
    """Busca a árvore de categorias via API VTEX."""
    url = f"{base_url.rstrip('/')}/api/catalog_system/pub/category/tree/3"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        log.error(f"Erro ao buscar {url}: {e}")
    return []


def find_by_keyword(cats: list, *keywords) -> list:
    """Busca recursivamente categorias que contenham qualquer keyword."""
    result = []
    for cat in cats:
        name = cat.get("name", "").lower()
        url = cat.get("url", "").lower()
        if any(kw.lower() in name or kw.lower() in url for kw in keywords):
            result.append(cat)
        result += find_by_keyword(cat.get("children", []), *keywords)
    return result


async def get_brand_id(brand_key: str) -> int | None:
    rows = await db.query_raw(
        "SELECT id FROM brands WHERE brand_key = $1 LIMIT 1",
        brand_key
    )
    if rows:
        return rows[0]["id"]
    return None


async def insert_category(brand_id: int, brand_key: str, canonical_slug: str, source_path: str, label: str):
    """Insere ou atualiza uma categoria no banco."""
    await db.query_raw(
        """
        INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW(), NOW())
        ON CONFLICT (brand_id, canonical_slug) DO UPDATE
          SET source_path = EXCLUDED.source_path,
              label       = EXCLUDED.label,
              updated_at  = NOW()
        """,
        brand_id, canonical_slug, source_path, label
    )
    log.info(f"  [{brand_key}] INSERIDO/ATUALIZADO slug='{canonical_slug}' label='{label}' path='{source_path}'")


def url_to_relative_path(url: str, base_domain: str) -> str:
    """Converte URL absoluta em path relativo."""
    for prefix in [f"https://www.{base_domain}", f"https://{base_domain}", f"http://www.{base_domain}", f"http://{base_domain}"]:
        if url.startswith(prefix):
            return url[len(prefix):] or "/"
    return url


async def process_trackfield():
    log.info("\n=== TRACK & FIELD: Buscando calçados ===")
    brand_id = await get_brand_id("trackfield")
    if not brand_id:
        log.error("[trackfield] Marca não encontrada!")
        return

    cats = await fetch_vtex_category_tree("https://www.tf.com.br")
    if not cats:
        # Fallback: inserir manualmente com URLs conhecidos da screenshot e site
        calcados_masculinos = [
            ("/calcados-masculinos", "calcados-masculinos", "CALÇADOS MASCULINOS"),
            ("/calcados-masculinos/tenis", "calcados-masculinos-tenis", "Tênis Masculino"),
            ("/calcados-masculinos/meias", "calcados-masculinos-meias", "Meias Masculinas"),
            ("/calcados-masculinos/chinelos", "calcados-masculinos-chinelos", "Chinelos Masculinos"),
        ]
        for path, slug, label in calcados_masculinos:
            await insert_category(brand_id, "trackfield", slug, path, label)
        return

    # Procura a seção de calçados no site
    calc_cats = find_by_keyword(cats, "calçados", "calcados", "calcado", "tênis", "tenis", "chinelo", "meia")

    # Filtrar apenas masculinos
    masc_cats = [c for c in calc_cats if "masculin" in c.get("url", "").lower() or "masculin" in c.get("name", "").lower()]
    all_cats = calc_cats if not masc_cats else masc_cats

    log.info(f"  [trackfield] Encontradas {len(all_cats)} categorias de calçados")
    for cat in all_cats:
        url_full = cat.get("url", "")
        path = url_to_relative_path(url_full, "tf.com.br")
        slug_parts = path.strip("/").replace("/", "-")
        label_name = cat.get("name", slug_parts)
        # Prefixar com "Infantil - " se for infantil
        if "infantis" in path.lower() or "infantil" in path.lower():
            label_name = f"Infantil - {label_name.title()}"
        await insert_category(brand_id, "trackfield", slug_parts, path, label_name)


async def process_oriba():
    log.info("\n=== ORIBA: Verificando calçados ===")
    brand_id = await get_brand_id("oriba")
    if not brand_id:
        log.error("[oriba] Marca não encontrada!")
        return

    # Oriba usa Shopify, não VTEX — verificamos via URL direta
    # A Oriba é marca de moda casual/streetwear masculino
    # Verificar se têm calçados no catálogo
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get("https://www.oriba.com.br/collections/calcados")
            if r.status_code == 200 and "calcados" in r.url.path.lower():
                await insert_category(brand_id, "oriba",
                    "calcados", "/collections/calcados", "Calçados")
                log.info("  [oriba] Coleção /collections/calcados existe!")
            else:
                log.info(f"  [oriba] /collections/calcados retornou {r.status_code} - sem calçados nessa URL")

            # Tentar /colecoes/calcados
            r2 = await c.get("https://www.oriba.com.br/collections/tenis")
            if r2.status_code == 200:
                await insert_category(brand_id, "oriba",
                    "calcados-tenis", "/collections/tenis", "Tênis")
                log.info("  [oriba] Coleção /collections/tenis existe!")
    except Exception as e:
        log.error(f"  [oriba] Erro: {e}")


async def process_buckman():
    log.info("\n=== BUCKMAN: Verificando calçados ===")
    brand_id = await get_brand_id("bck")
    if not brand_id:
        log.error("[bck] Marca não encontrada!")
        return

    # Buckman BCK é marca de calçados/tênis — deve ter calçados!
    # Site usa VTEX mas com domínio diferente
    cats = await fetch_vtex_category_tree("https://buckmanbck.com.br")
    if not cats:
        # Tentar via URL direta (Buckman usa URLs conhecidas de tênis)
        known_calcados = [
            ("/masculino/tenis", "calcados-tenis", "Tênis Masculino"),
            ("/masculino/calcados", "calcados", "Calçados Masculinos"),
            ("/tenis", "calcados-tenis", "Tênis"),
            ("/calcados", "calcados", "Calçados"),
        ]
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            for path, slug, label in known_calcados:
                try:
                    r = await c.get(f"https://buckmanbck.com.br{path}")
                    if r.status_code == 200 and "404" not in r.text[:200]:
                        await insert_category(brand_id, "bck", slug, path, label)
                        log.info(f"  [bck] URL válida: {path}")
                        break
                except Exception:
                    pass
        return

    calc_cats = find_by_keyword(cats, "calçados", "calcados", "tênis", "tenis", "sapato", "bota", "sandalia")
    for cat in calc_cats:
        url_full = cat.get("url", "")
        # Buckman usa buckmanbck.com.br
        path = url_to_relative_path(url_full, "buckmanbck.com.br")
        slug = path.strip("/").replace("/", "-")
        label = cat.get("name", slug)
        await insert_category(brand_id, "bck", slug, path, label)


async def process_levis():
    log.info("\n=== LEVI'S: Verificando calçados ===")
    brand_id = await get_brand_id("levis")
    if not brand_id:
        log.error("[levis] Marca não encontrada!")
        return

    # Levi's BR é focada em roupas (jeans) — sem calçados próprios
    # Verificar via API
    cats = await fetch_vtex_category_tree("https://www.levi.com.br")
    calc_cats = find_by_keyword(cats, "calçados", "calcados", "tênis", "tenis", "sapato")
    if calc_cats:
        for cat in calc_cats:
            url_full = cat.get("url", "")
            path = url_to_relative_path(url_full, "levi.com.br")
            slug = path.strip("/").replace("/", "-")
            label = cat.get("name", slug)
            await insert_category(brand_id, "levis", slug, path, label)
    else:
        log.info("  [levis] Nenhuma categoria de calçados encontrada no site - confirmado: Levi's BR não vende calçados")


async def main():
    await db.connect()
    log.info("Conectado ao banco. Adicionando calçados...")

    await process_trackfield()
    await process_oriba()
    await process_buckman()
    await process_levis()

    log.info("\n=== CONCLUÍDO ===")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
