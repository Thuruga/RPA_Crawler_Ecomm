"""
clean_calcados_labels.py
=========================
Limpa e padroniza as categorias de calçados inseridas pelo add_calcados_categories.py:
  - Levi's: prefixar masculino/feminino/unissex, remover categorias desnecessárias
  - Track & Field: remover duplicatas tf-mall (lojas físicas), manter /calcados, /calcados/tenis, /calcados/chinelos
  - Buckman: buscar via site alternativo
"""
import asyncio
import logging
import httpx
from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


async def fix_levis_calcados():
    log.info("\n=== LEVI'S: Corrigindo labels de calçados ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'levis' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    rows = await db.query_raw(
        "SELECT id, canonical_slug, label, source_path FROM brand_category_mappings WHERE brand_id = $1 AND source_path LIKE '/calcados/%'",
        brand_id
    )

    for r in rows:
        path = r["source_path"]
        label = r["label"]
        slug = r["canonical_slug"]
        new_label = None

        if path == "/calcados/masculino":
            new_label = "Calçados - Masculino (Geral)"
        elif path == "/calcados/masculino/tenis":
            new_label = "Calçados - Tênis Masculino"
        elif path == "/calcados/masculino/bota":
            new_label = "Calçados - Botas Masculinas"
        elif path == "/calcados/masculino/chinelo":
            new_label = "Calçados - Chinelos Masculinos"
        elif "/calcados/feminino" in path:
            # Remover categorias femininas da Levi's (foco masculino)
            await db.query_raw("DELETE FROM brand_category_mappings WHERE id = $1", r["id"])
            log.info(f"  [levis] REMOVIDO feminino id={r['id']} path='{path}'")
            continue
        elif "/calcados/unissex" in path:
            if path == "/calcados/unissex":
                new_label = "Calçados - Unissex (Geral)"
            elif "tenis" in path:
                new_label = "Calçados - Tênis Unissex"
            elif "chinelo" in path:
                new_label = "Calçados - Chinelos Unissex"

        if new_label and new_label != label:
            await db.query_raw(
                "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
                new_label, r["id"]
            )
            log.info(f"  [levis] id={r['id']} '{label}' -> '{new_label}'")

    # Também corrigir o label da categoria raiz de calçados
    await db.query_raw(
        "UPDATE brand_category_mappings SET label = 'Calçados (Geral)' WHERE brand_id = $1 AND canonical_slug = 'calcados' AND source_path = '/calcados'",
        brand_id
    )
    log.info("  [levis] Atualizado 'Calçados' raiz -> 'Calçados (Geral)'")


async def fix_trackfield_calcados():
    log.info("\n=== TRACK & FIELD: Removendo categorias de loja física (tf-mall) ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'trackfield' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    # Remover as categorias tf-mall (são lojas físicas/marketplace, não ecommerce)
    deleted = await db.query_raw(
        "DELETE FROM brand_category_mappings WHERE brand_id = $1 AND (source_path LIKE '/tf-mall/%' OR source_path LIKE '/acessorio/fitness/meias')",
        brand_id
    )

    # Também mover meias de moda-intima para categoria de acessórios principal
    await db.query_raw(
        """
        UPDATE brand_category_mappings
        SET source_path = '/acessorios-masculinos/meias', label = 'Meias', canonical_slug = 'acessorios-meias'
        WHERE brand_id = $1 AND source_path = '/moda-intima/meias'
        """,
        brand_id
    )

    # Padronizar labels
    updates = [
        ("calcados", "Calçados (Geral)"),
        ("calcados-tenis", "Tênis"),
        ("calcados-chinelos", "Chinelos"),
        ("acessorios-meias", "Meias"),
    ]
    for slug, label in updates:
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE brand_id = $2 AND canonical_slug = $3",
            label, brand_id, slug
        )

    log.info("  [trackfield] Removidas categorias tf-mall, meias movida para acessórios")

    # Verificar estado final
    rows = await db.query_raw(
        "SELECT canonical_slug, label, source_path FROM brand_category_mappings WHERE brand_id = $1 AND (source_path LIKE '%calcado%' OR source_path LIKE '%tenis%' OR source_path LIKE '%chinelo%' OR source_path LIKE '%meias%') ORDER BY source_path",
        brand_id
    )
    log.info(f"  [trackfield] Estado final de calçados/meias:")
    for r in rows:
        log.info(f"    {r['canonical_slug']}: {r['label']} -> {r['source_path']}")


async def fix_buckman():
    """Buckman BCK é uma marca de calçados — busca via Shopify ou catalogação manual."""
    log.info("\n=== BUCKMAN: Buscando calçados via site ===")
    brand_id_rows = await db.query_raw("SELECT id FROM brands WHERE brand_key = 'bck' LIMIT 1")
    if not brand_id_rows:
        return
    brand_id = brand_id_rows[0]["id"]

    # Buckman usa Shopify — tentar via collections JSON
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        try:
            r = await c.get("https://buckmanbck.com.br/collections.json?limit=50")
            if r.status_code == 200:
                data = r.json()
                collections = data.get("collections", [])
                log.info(f"  [bck] Encontradas {len(collections)} coleções Shopify")
                for col in collections:
                    handle = col.get("handle", "")
                    title = col.get("title", "")
                    url_path = f"/collections/{handle}"
                    # Filtrar relevantes masculinas (calçados, tênis, acessórios)
                    if any(kw in handle.lower() or kw in title.lower()
                           for kw in ["calcado", "tenis", "sapato", "sneaker", "bota", "chinelo", "sandal", "skateboard"]):
                        # Verificar se é masculino ou geral
                        slug = f"calcados-{handle}".replace("/", "-").replace("--", "-")
                        await db.query_raw(
                            """
                            INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
                            VALUES ($1, $2, $3, $4, NOW(), NOW())
                            ON CONFLICT (brand_id, canonical_slug) DO UPDATE
                              SET source_path = EXCLUDED.source_path, label = EXCLUDED.label, updated_at = NOW()
                            """,
                            brand_id, slug, url_path, title
                        )
                        log.info(f"  [bck] INSERIDO slug='{slug}' label='{title}' path='{url_path}'")
            else:
                log.info(f"  [bck] collections.json retornou {r.status_code}")
                # Tentar endpoint alternativo
                r2 = await c.get("https://buckmanbck.com.br/api/2023-10/collections.json")
                log.info(f"  [bck] API v2 retornou {r2.status_code}")
        except Exception as e:
            log.error(f"  [bck] Erro: {e}")


async def main():
    await db.connect()

    await fix_levis_calcados()
    await fix_trackfield_calcados()
    await fix_buckman()

    log.info("\n=== CONCLUÍDO ===")
    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
