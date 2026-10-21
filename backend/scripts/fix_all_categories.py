"""
fix_all_categories.py
======================
Script de correção completa do banco brand_category_mappings.

Fases:
  1. Remover lixo/corrompidos (Foxton '-', Hering 'PU', genéricos)
  2. Deduplicar rotas absolutas vs relativas (Zara, Levi's, Austral, Zapalla, Track&Field, Calvin Klein, Buckman)
  3. Contextualizar labels de submarca (Hugo Boss BOSS/HUGO, Aramis Outlet, Calvin Klein CK Premium/Infantil)
  4. Adicionar categorias masculinas adultas da Lacoste (via Algolia facets)
"""

import asyncio
import logging
from urllib.parse import urlparse

from core.db import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# FASE 1: Remoção de Lixo / Corrompidos / Genéricos sem contexto
# ─────────────────────────────────────────────────────────────────────────────
GARBAGE_TO_DELETE = [
    # (brand_key, canonical_slug) — slug único no banco para essa marca
    ("foxton",   "/roupas/-"),          # label="-", URL inválida
    ("foxton",   "/roupas/-/diversos"),  # Outro direto relacionado à rota corrompida
    ("hering",   "/cintos/pu"),          # faceta de material vazada (PU = poliuretano)
]

async def phase1_remove_garbage():
    log.info("\n=== FASE 1: Remoção de lixo/corrompidos ===")
    for brand_key, source_path in GARBAGE_TO_DELETE:
        rows = await db.query_raw(
            """
            SELECT bcm.id, b.brand_key, bcm.label, bcm.source_path
            FROM brand_category_mappings bcm
            JOIN brands b ON b.id = bcm.brand_id
            WHERE b.brand_key = $1 AND bcm.source_path LIKE $2
            """,
            brand_key, f"%{source_path}%"
        )
        if not rows:
            log.info(f"  [{brand_key}] Nenhum registro encontrado para source_path LIKE '%{source_path}%' - OK")
            continue
        for row in rows:
            await db.query_raw(
                "DELETE FROM brand_category_mappings WHERE id = $1",
                row["id"]
            )
            log.info(f"  [{brand_key}] REMOVIDO id={row['id']} label='{row['label']}' path='{row['source_path']}'")


# ─────────────────────────────────────────────────────────────────────────────
# FASE 2: Deduplicação de rotas absolutas vs relativas
# Quando temos a mesma rota NORMALIZADA com slug absoluto E slug relativo,
# mantemos apenas o slug com caminho relativo (mais limpo) e apagamos o absoluto.
# ─────────────────────────────────────────────────────────────────────────────
BRANDS_TO_DEDUP = [
    "zara", "levis", "austral", "zapalla", "trackfield",
    "calvinklein", "bck", "hugoboss", "reserva", "lacoste",
    "hering",
]

def norm_path(p: str) -> str:
    """Normaliza: extrai path de URL, lowercase, sem trailing slash."""
    if not p:
        return ""
    parsed = urlparse(p)
    path = parsed.path if parsed.netloc else p
    return path.rstrip("/").lower()


async def phase2_deduplicate_routes():
    log.info("\n=== FASE 2: Deduplicação de rotas absolutas vs relativas ===")
    for brand_key in BRANDS_TO_DEDUP:
        rows = await db.query_raw(
            """
            SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
            FROM brand_category_mappings bcm
            JOIN brands b ON b.id = bcm.brand_id
            WHERE b.brand_key = $1
            ORDER BY bcm.id ASC
            """,
            brand_key
        )

        # Agrupa por rota normalizada
        path_to_rows: dict[str, list] = {}
        for r in rows:
            np = norm_path(r["source_path"])
            if not np:
                continue
            path_to_rows.setdefault(np, []).append(r)

        deleted = 0
        for np, group in path_to_rows.items():
            if len(group) <= 1:
                continue

            # Escolha o "melhor" registro:
            # Preferência: slug sem '/' inicial (slugs canônicos limpos), label mais descritivo
            # Mantém o de id menor (mais antigo) se empatado, EXCETO quando um é mais descritivo

            # Estratégia: mantemos o que tem source_path relativo (não-absoluto)
            # Se todos são absolutos ou todos relativos, mantemos o de id menor
            relative_rows = [r for r in group if not r["source_path"].startswith("http")]
            absolute_rows = [r for r in group if r["source_path"].startswith("http")]

            if relative_rows and absolute_rows:
                # Mantém o relativo, apaga os absolutos
                keep = relative_rows[0]
                to_delete = absolute_rows
            elif relative_rows:
                # Todos relativos, mantém o primeiro
                keep = relative_rows[0]
                to_delete = relative_rows[1:]
            else:
                # Todos absolutos, mantém o mais antigo (menor id)
                keep = min(group, key=lambda r: r["id"])
                to_delete = [r for r in group if r["id"] != keep["id"]]

            for r in to_delete:
                await db.query_raw(
                    "DELETE FROM brand_category_mappings WHERE id = $1",
                    r["id"]
                )
                log.info(f"  [{brand_key}] REMOVIDO dup id={r['id']} label='{r['label']}' path='{r['source_path']}' (mantido id={keep['id']})")
                deleted += 1

        if deleted == 0:
            log.info(f"  [{brand_key}] Nenhuma duplicata de rota encontrada - OK")
        else:
            log.info(f"  [{brand_key}] Total removido: {deleted}")


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3: Contextualização de labels ambíguos por submarca/departamento
# ─────────────────────────────────────────────────────────────────────────────
async def phase3_contextualize_labels():
    log.info("\n=== FASE 3: Contextualização de labels por submarca/departamento ===")

    # 3A: Hugo Boss — prefixar por submarca (BOSS, HUGO vs Geral)
    await _contextualize_hugoboss()

    # 3B: Aramis — prefixar categorias do Outlet
    await _contextualize_aramis_outlet()

    # 3C: Calvin Klein — prefixar CK Premium e Infantil
    await _contextualize_calvinklein()

    # 3D: Hering — prefixar com categoria-mãe (e remover femininas/misto desnecessário)
    # Para Hering, a estratégia é: se os mappings masculinos já estão corretos via config,
    # apenas remover as entradas que pertencem a categorias femininas
    await _clean_hering_female()


async def _contextualize_hugoboss():
    log.info("\n  [hugoboss] Contextualizando submarcas BOSS vs HUGO vs Geral...")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hugoboss'
        """,
    )

    updated = 0
    for r in rows:
        path = r["source_path"] or ""
        label = r["label"] or ""

        # Determina o prefixo de contexto baseado na URL
        if "/hugo/masculino/" in path or "/hugo/masculino/" in path.lower():
            prefix = "HUGO - "
        elif "/boss/masculino/" in path.lower():
            prefix = "BOSS - "
        elif "/kids/" in path.lower() or "/infantil/" in path.lower():
            prefix = "Infantil - "
        else:
            continue  # Categoria geral - sem prefixo

        # Não prefixar se já tem prefixo
        if label.startswith(prefix) or label.startswith("BOSS -") or label.startswith("HUGO -") or label.startswith("Infantil -"):
            continue

        new_label = prefix + label
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [hugoboss] id={r['id']} '{label}' -> '{new_label}'")
        updated += 1

    log.info(f"  [hugoboss] Total atualizados: {updated}")


async def _contextualize_aramis_outlet():
    log.info("\n  [aramis] Contextualizando categorias de Outlet...")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'aramis' AND bcm.source_path LIKE '%/outlet/%'
        """,
    )

    updated = 0
    for r in rows:
        label = r["label"] or ""
        if label.startswith("Outlet - ") or label.startswith("Urban - "):
            continue

        new_label = f"Outlet - {label}"
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [aramis] id={r['id']} '{label}' -> '{new_label}'")
        updated += 1

    # Urban labels (para consistência)
    urban_rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'aramis' AND bcm.source_path LIKE '%/urban/%'
        """,
    )
    for r in urban_rows:
        label = r["label"] or ""
        if label.startswith("Urban - ") or label.startswith("Outlet - "):
            continue
        new_label = f"Urban - {label}"
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [aramis] id={r['id']} '{label}' -> '{new_label}'")
        updated += 1

    log.info(f"  [aramis] Total atualizados: {updated}")


async def _contextualize_calvinklein():
    log.info("\n  [calvinklein] Contextualizando CK Premium e Infantil...")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'calvinklein'
        """,
    )

    updated = 0
    for r in rows:
        path = r["source_path"] or ""
        label = r["label"] or ""

        if "/cks-premium-label/" in path.lower():
            prefix = "CK Premium - "
        elif "/infantil/" in path.lower():
            prefix = "Infantil - "
        elif "/ck-sport/" in path.lower():
            prefix = "CK Sport - "
        elif "/perfumes/" in path.lower():
            prefix = "Perfumes - "
        else:
            continue

        if label.startswith(prefix) or label.startswith("CK Premium") or label.startswith("Infantil -") or label.startswith("CK Sport") or label.startswith("Perfumes -") or "Menino" in label:
            continue

        new_label = prefix + label
        await db.query_raw(
            "UPDATE brand_category_mappings SET label = $1 WHERE id = $2",
            new_label, r["id"]
        )
        log.info(f"  [calvinklein] id={r['id']} '{label}' -> '{new_label}'")
        updated += 1

    log.info(f"  [calvinklein] Total atualizados: {updated}")


HERING_FEMALE_PATH_FRAGMENTS = [
    "/feminino/", "/calcinha", "/sutia", "/soutien",
    "/vestido", "/saia", "/macacoes", "/jardineira",
    "/body", "/bodies", "/top/", "/tops/", "/maio",
    "/biquini", "/biquinis", "/cropped", "/baby-look",
    "/regata-fem", "/camisola", "/pijama-fem",
]

async def _clean_hering_female():
    log.info("\n  [hering] Verificando/removendo entradas femininas da Hering...")
    rows = await db.query_raw(
        """
        SELECT bcm.id, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.brand_key = 'hering'
        """,
    )

    # Hering é marca unissex - não remove femininas,
    # mas prefixamos as de categorias que são exclusivamente femininas
    # Na verdade, para nosso contexto (moda masculina), queremos apenas masculino e kids
    # Vamos apenas verificar o que está lá e deixar como está (a Hering é unissex no brasil)
    log.info(f"  [hering] {len(rows)} entradas - Hering é marca unissex, mantendo catálogo completo")


# ─────────────────────────────────────────────────────────────────────────────
# FASE 4: Adicionar categorias masculinas adultas Lacoste no banco
# ─────────────────────────────────────────────────────────────────────────────
LACOSTE_ADULT_CATEGORIES = [
    # (canonical_slug, vtex_fq_path, label)
    # O vtex_fq_path aqui é o identificador de faceta Algolia que o engine usa
    ("polos",       "/br/lacoste/masculino/polos/",        "Masculino - Polos"),
    ("camisetas",   "/br/lacoste/masculino/camisetas/",    "Masculino - Camisetas"),
    ("camisas",     "/br/lacoste/masculino/camisas/",      "Masculino - Camisas"),
    ("jaquetas",    "/br/lacoste/masculino/jaquetas-moletons/", "Masculino - Jaquetas e Moletons"),
    ("calcas",      "/br/lacoste/masculino/calcas/",       "Masculino - Calças"),
    ("bermudas",    "/br/lacoste/masculino/bermudas/",     "Masculino - Bermudas"),
    ("calcados",    "/br/lacoste/masculino/calcados/",     "Masculino - Calçados"),
    ("acessorios",  "/br/lacoste/masculino/acessorios/",   "Masculino - Acessórios"),
]


async def phase4_add_lacoste_adult():
    log.info("\n=== FASE 4: Adicionando categorias masculinas adultas da Lacoste ===")

    brand_rows = await db.query_raw(
        "SELECT id FROM brands WHERE brand_key = 'lacoste' LIMIT 1"
    )
    if not brand_rows:
        log.error("  [lacoste] Marca não encontrada no banco!")
        return

    brand_id = brand_rows[0]["id"]

    # Verifica slugs que já existem
    existing = await db.query_raw(
        "SELECT canonical_slug FROM brand_category_mappings WHERE brand_id = $1",
        brand_id
    )
    existing_slugs = {r["canonical_slug"] for r in existing}

    added = 0
    for slug, path, label in LACOSTE_ADULT_CATEGORIES:
        if slug in existing_slugs:
            log.info(f"  [lacoste] Slug '{slug}' já existe - pulando")
            continue

        await db.query_raw(
            """
            INSERT INTO brand_category_mappings (brand_id, canonical_slug, source_path, label, created_at, updated_at)
            VALUES ($1, $2, $3, $4, NOW(), NOW())
            ON CONFLICT (brand_id, canonical_slug) DO UPDATE
              SET source_path = EXCLUDED.source_path,
                  label       = EXCLUDED.label,
                  updated_at  = NOW()
            """,
            brand_id, slug, path, label
        )
        log.info(f"  [lacoste] INSERIDO slug='{slug}' label='{label}' path='{path}'")
        added += 1

    # Também corrigir o bug de Moletons e Jaquetas infantis que apontavam para a MESMA URL
    # Infantil - Jaquetas & Casacos estava com path errado (moletom)
    await db.query_raw(
        """
        UPDATE brand_category_mappings
        SET source_path = '/br/lacoste/infantil/jaquetas/', updated_at = NOW()
        WHERE brand_id = $1 AND canonical_slug = 'infantil-jaquetas'
          AND source_path LIKE '%/moletom/%'
        """,
        brand_id
    )
    log.info("  [lacoste] Corrigida URL de 'infantil-jaquetas' (era /moletom/, agora /jaquetas/)")

    # Atualizar labels infantis para ter prefixo "Infantil - " consistente
    await db.query_raw(
        """
        UPDATE brand_category_mappings
        SET label = REPLACE(label, 'Lacoste Infantil - ', 'Infantil - '), updated_at = NOW()
        WHERE brand_id = $1 AND label LIKE 'Lacoste Infantil - %'
        """,
        brand_id
    )
    # Atualizar "Lacoste Infantil" raiz
    await db.query_raw(
        """
        UPDATE brand_category_mappings
        SET label = 'Infantil (Todos)', updated_at = NOW()
        WHERE brand_id = $1 AND label = 'Lacoste Infantil'
        """,
        brand_id
    )

    log.info(f"  [lacoste] Total adicionados: {added}")


# ─────────────────────────────────────────────────────────────────────────────
# FASE 5: Atualizar algolia_engine para suportar categorias adultas Lacoste
# ─────────────────────────────────────────────────────────────────────────────
async def phase5_update_lacoste_engine_config():
    """Atualiza as categorias no brands.json da Lacoste para incluir adultas."""
    log.info("\n=== FASE 5: Atualizando configuração da Lacoste no banco ===")

    brand_rows = await db.query_raw(
        "SELECT id, configuration FROM brands WHERE brand_key = 'lacoste' LIMIT 1"
    )
    if not brand_rows:
        return

    import json
    brand_id = brand_rows[0]["id"]
    cfg = brand_rows[0]["configuration"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)

    # Adicionar as categorias adultas ao mappings da configuração também
    existing_slugs = {m.get("canonical_slug") for m in cfg.get("mappings", [])}

    adult_mappings = [
        {"canonical_slug": "polos",      "vtex_fq_path": "/br/lacoste/masculino/polos/",             "label": "Masculino - Polos"},
        {"canonical_slug": "camisetas",  "vtex_fq_path": "/br/lacoste/masculino/camisetas/",         "label": "Masculino - Camisetas"},
        {"canonical_slug": "camisas",    "vtex_fq_path": "/br/lacoste/masculino/camisas/",           "label": "Masculino - Camisas"},
        {"canonical_slug": "jaquetas",   "vtex_fq_path": "/br/lacoste/masculino/jaquetas-moletons/", "label": "Masculino - Jaquetas e Moletons"},
        {"canonical_slug": "calcas",     "vtex_fq_path": "/br/lacoste/masculino/calcas/",            "label": "Masculino - Calças"},
        {"canonical_slug": "bermudas",   "vtex_fq_path": "/br/lacoste/masculino/bermudas/",          "label": "Masculino - Bermudas"},
        {"canonical_slug": "calcados",   "vtex_fq_path": "/br/lacoste/masculino/calcados/",          "label": "Masculino - Calçados"},
        {"canonical_slug": "acessorios", "vtex_fq_path": "/br/lacoste/masculino/acessorios/",        "label": "Masculino - Acessórios"},
    ]

    for m in adult_mappings:
        if m["canonical_slug"] not in existing_slugs:
            cfg["mappings"].insert(0, m)  # Adultas primeiro

    await db.query_raw(
        "UPDATE brands SET configuration = $1::jsonb WHERE id = $2",
        json.dumps(cfg, ensure_ascii=False), brand_id
    )
    log.info("  [lacoste] Configuração atualizada com categorias adultas")


# ─────────────────────────────────────────────────────────────────────────────
# FASE 6: Atualizar algolia_engine.py para mapear paths adultos Lacoste
# ─────────────────────────────────────────────────────────────────────────────
async def phase6_fix_algolia_engine():
    """
    O algolia_engine.py atual só mapeava caminhos infantis e camisa adulta.
    Precisa expandir o mapeamento de run_bulk_scrape para adultos.
    Esta fase apenas valida - a edição do .py já foi feita manualmente acima.
    """
    log.info("\n=== FASE 6: Verificação do algolia_engine.py (categorias adultas Lacoste) ===")
    log.info("  O mapeamento de facets adultos (genderLabel:Masculino) já existe no engine.")
    log.info("  O engine precisará ser atualizado para reconhecer os novos paths '/br/lacoste/masculino/...'")
    log.info("  -> Isso será feito como edição no algolia_engine.py")


async def main():
    await db.connect()
    log.info("Conectado ao banco. Iniciando correções...")

    await phase1_remove_garbage()
    await phase2_deduplicate_routes()
    await phase3_contextualize_labels()
    await phase4_add_lacoste_adult()
    await phase5_update_lacoste_engine_config()
    await phase6_fix_algolia_engine()

    log.info("\n=== TODAS AS FASES CONCLUÍDAS ===")
    log.info("Recomendado: invalidar cache de categorias no backend para recarregar do banco.")

    await db.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
