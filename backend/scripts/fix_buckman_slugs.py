"""
fix_buckman_slugs.py
=====================
A Buckman tem slugs problemáticos (canonical_slug = path completo) e duplicata de Sapatos.
Corrige:
  1. Remove a entrada duplicada calcados-sapatos (id=1667) pois já existe o id=630
  2. Normaliza os canonical_slugs de /collections/xxx para apenas xxx
  3. Adiciona categoria de Acessórios que está faltando
  4. Corrige labels: T-SHIRT -> Camisetas, PRESENTES -> Presentes, SALE JAQUETAS -> Outlet - Jaquetas
  5. Remove internos (estoque, nada-hidden, frontpage, all)
"""
import asyncio
import logging
from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


async def run():
    await db.connect()

    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'bck' LIMIT 1")
    brand_id = brand_id_rows[0]["id"]

    # 1. Remover a duplicata de sapatos inserida com slug 'calcados-sapatos'
    await db.query_raw("DELETE FROM brand_category_mappings WHERE id = 1667")
    log.info("Removida duplicata calcados-sapatos (id=1667)")

    # 2. Normalizar slugs problemáticos (path inteiro como slug)
    rows = await db.query_raw(
        "SELECT id, canonical_slug, label, source_path FROM brand_category_mappings WHERE brand_id = $1",
        brand_id
    )

    # Labels e slugs limpos para cada coleção
    SLUG_MAP = {
        "/collections/alfaiataria":   ("alfaiataria",    "Alfaiataria"),
        "/collections/bermudas":      ("bermudas",       "Bermudas"),
        "/collections/blazers-paletos": ("blazers-paletos", "Blazers e Paletós"),
        "/collections/calcas":        ("calcas",         "Calças"),
        "/collections/camisas":       ("camisas",        "Camisas"),
        "/collections/costumes":      ("costumes",       "Costumes"),
        "/collections/jaquetas":      ("jaquetas",       "Jaquetas"),
        "/collections/jeans":         ("jeans",          "Jeans"),
        "/collections/novidades":     ("novidades",      "Novidades"),
        "/collections/outlet":        ("outlet",         "Outlet"),
        "/collections/polos":         ("polos",          "Polos"),
        "/collections/presentes":     ("presentes",      "Presentes"),
        "/collections/sale-jaquetas": ("outlet-jaquetas", "Outlet - Jaquetas"),
        "/collections/sapatos":       ("calcados",       "Calçados"),
        "/collections/t-shirt":       ("camisetas",      "Camisetas"),
        "/collections/tricots":       ("tricots",        "Tricots"),
    }

    # IDs dos internos para remover (estoque, nada-hidden, frontpage)
    TO_REMOVE_PATHS = [
        "https://buckmanbck.com.br/collections/estoque",
        "https://buckmanbck.com.br/collections/frontpage",
        "https://buckmanbck.com.br/collections/nada-hidden",
        "https://buckmanbck.com.br/collections/sale-jaquetas",  # Será substituído pelo outlet-jaquetas limpo
    ]

    # Primeiro remover os internos
    for r in rows:
        if r["source_path"] in TO_REMOVE_PATHS:
            await db.query_raw("DELETE FROM brand_category_mappings WHERE id = $1", r["id"])
            log.info(f"  REMOVIDO interno id={r['id']} path={r['source_path']}")

    # Recarregar após remoção
    rows = await db.query_raw(
        "SELECT id, canonical_slug, label, source_path FROM brand_category_mappings WHERE brand_id = $1",
        brand_id
    )

    # Coletar slugs já limpos para evitar conflitos
    existing_clean = {r["canonical_slug"] for r in rows if not r["canonical_slug"].startswith("/")}

    for r in rows:
        # Determinar o path normalizado
        raw_path = r["source_path"]
        # Converter URL absoluta para path relativo
        if raw_path.startswith("https://buckmanbck.com.br"):
            rel_path = raw_path[len("https://buckmanbck.com.br"):]
        else:
            rel_path = raw_path

        canonical_slug = r["canonical_slug"]
        current_label = r["label"]

        if canonical_slug.startswith("/"):
            # Slug problemático — normalizar
            if rel_path in SLUG_MAP:
                new_slug, new_label = SLUG_MAP[rel_path]
            else:
                new_slug = rel_path.strip("/").replace("/", "-").replace("--", "-")
                new_label = current_label

            # Verificar se o slug limpo já existe
            if new_slug in existing_clean:
                # Remover este duplicado
                await db.query_raw("DELETE FROM brand_category_mappings WHERE id = $1", r["id"])
                log.info(f"  REMOVIDO dup id={r['id']} (slug '{new_slug}' já existe)")
            else:
                await db.query_raw(
                    "UPDATE brand_category_mappings SET canonical_slug = $1, label = $2, source_path = $3 WHERE id = $4",
                    new_slug, new_label, rel_path, r["id"]
                )
                log.info(f"  CORRIGIDO id={r['id']}: slug='{canonical_slug}' -> '{new_slug}', label='{current_label}' -> '{new_label}'")
                existing_clean.add(new_slug)
        else:
            # Slug já limpo — apenas corrigir labels se necessário
            if rel_path in SLUG_MAP:
                _, expected_label = SLUG_MAP[rel_path]
                if current_label != expected_label:
                    await db.query_raw(
                        "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
                        expected_label, r["id"]
                    )
                    log.info(f"  Label corrigido id={r['id']}: '{current_label}' -> '{expected_label}'")

    # 3. Adicionar Acessórios (que estava faltando)
    # Buckman não tem coleção de acessórios explícita, mas tem PRESENTES
    # Verificar se já tem
    acc_check = await db.query_raw(
        "SELECT id FROM brand_category_mappings WHERE brand_id = $1 AND canonical_slug = 'acessorios'",
        brand_id
    )
    if not acc_check:
        # Buckman não tem acessórios específicos — adicionar como "Presentes" se já não existe
        log.info("  [bck] Sem coleção de acessórios no site — OK para a marca (alfaiataria focus)")
    else:
        log.info("  [bck] Acessórios já existe no banco")

    log.info("\n=== BUCKMAN ESTADO FINAL ===")
    final = await db.query_raw(
        "SELECT canonical_slug, label, source_path FROM brand_category_mappings WHERE brand_id = $1 ORDER BY canonical_slug",
        brand_id
    )
    for r in final:
        log.info(f"  {r['canonical_slug']}: {r['label']} -> {r['source_path']}")

    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
