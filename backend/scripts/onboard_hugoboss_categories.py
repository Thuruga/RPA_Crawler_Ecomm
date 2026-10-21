"""
Descoberta e persistência do de/para de categorias da Hugo Boss (Phase 39, COMP-06-a).

Execução: python -m scripts.onboard_hugoboss_categories

Este script é de descoberta ÚNICA (D-04) — não rodar a cada varredura.
Reutiliza o pipeline VTEX existente: discover_categories (VTEXEngine) +
auto_match + revisão humana (print_and_confirm) + persist_mappings.

NÃO toca nas categorias hardcoded (D-01 — definidas só para aramis/reserva/tommy).
NÃO reimplementa auto_match nem persist_mappings — importa do analog.

Pitfall 3 DEFUSED: item["path"] retornado por VTEXEngine._flatten_vtex_tree é
URL completa; urlparse(...).path extrai o path relativo iniciando com "/".
"""
import asyncio
import sys
import os
from urllib.parse import urlparse

# Garante que o diretório raiz do projeto esteja no sys.path para imports absolutos
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.brand_service import brand_service
from services.engines.vtex_engine import VTEXEngine
from scripts.onboard_vtex_brands import auto_match, persist_mappings, print_and_confirm


# ---------------------------------------------------------------------------
# Constante do domínio — com www. (STATE.md [onboarding-live/2026-06-25])
# hugoboss.com.br sem www. NÃO resolve — usar sempre www.hugoboss.com.br
# ---------------------------------------------------------------------------

HUGOBOSS_KEY = "hugoboss"
HUGOBOSS_DOMAIN = "www.hugoboss.com.br"


# ---------------------------------------------------------------------------
# discover_hugoboss_mappings — VTEXEngine + urlparse path extraction
# ---------------------------------------------------------------------------

async def discover_hugoboss_mappings() -> "tuple[int, list]":
    """Descobre categorias via VTEXEngine("hugoboss") e auto-matcha para slugs canônicos.

    Pitfall 3 DEFUSED: item["path"] é URL completa — extrai path relativo via urlparse.
    Retorna (discovered_count, proposals) para que main distinga falha de descoberta
    (discovered_count == 0) de "descobriu mas nenhum match" (proposals vazia).
    """
    engine = VTEXEngine(HUGOBOSS_KEY)
    raw = await engine.discover_categories()
    for item in raw:
        # item["path"] é URL completa (ex: "https://www.hugoboss.com.br/masculino/roupas")
        # urlparse(...).path extrai "/masculino/roupas" — relativo, começa com "/"
        item["rel_path"] = urlparse(item.get("path") or "").path
    return len(raw), auto_match(raw)


# ---------------------------------------------------------------------------
# main — descoberta + revisão humana + persistência
# ---------------------------------------------------------------------------

async def main() -> None:
    """Orquestra a descoberta e persistência do de/para da Hugo Boss."""
    svc = brand_service  # singleton — dual persistence automática (D-08)

    print(f"\n{'='*60}")
    print(f"Descoberta de categorias: Hugo Boss ({HUGOBOSS_KEY})")
    print(f"Domínio: {HUGOBOSS_DOMAIN}")
    print(f"{'='*60}")

    # Verifica se a marca está cadastrada
    brand = svc.get_brand(HUGOBOSS_KEY)
    if not brand:
        print(
            f"[ERRO] Marca '{HUGOBOSS_KEY}' não encontrada em brands.json. "
            "Cadastre a marca antes de rodar este script."
        )
        return

    print(f"[OK] Marca encontrada: engine={brand.engine!r}, domain={brand.domain!r}")

    # Aviso sobre mappings existentes
    if brand.mappings:
        print(
            f"\n[INFO] {HUGOBOSS_KEY}: {len(brand.mappings)} mappings já existem.\n"
            "Este script é de descoberta ÚNICA (D-04) — continuar substituirá os mappings atuais."
        )

    # Descoberta via VTEXEngine (bate em www.hugoboss.com.br — rede ao vivo)
    print(f"\n[...] Descobrindo categorias via VTEXEngine({HUGOBOSS_KEY!r})...")
    discovered_count, proposals = await discover_hugoboss_mappings()

    if discovered_count == 0:
        print(
            "[WARN] discover_categories retornou 0 categorias — "
            "possível falha de rede ou domínio incorreto. "
            f"Verifique que '{HUGOBOSS_DOMAIN}' está acessível."
        )
        return

    print(f"[OK] {discovered_count} categorias descobertas na árvore VTEX.")

    if not proposals:
        print(
            "[WARN] Nenhum slug canônico obteve match entre as categorias descobertas. "
            "Verifique as keywords em CANONICAL_KEYWORDS (onboard_vtex_brands.py)."
        )
        return

    # Revisão humana do de/para proposto (gate D-09)
    # print_and_confirm exibe as propostas, os slugs SEM match e solicita [s/N]
    confirmed = print_and_confirm(HUGOBOSS_KEY, proposals)

    if not confirmed:
        print(f"[SKIP] {HUGOBOSS_KEY}: mappings não confirmados pelo operador. Nada foi gravado.")
        return

    # Somente após confirmação: persiste via brand_service.update_mappings (D-08)
    persist_mappings(svc, HUGOBOSS_KEY, proposals)

    # Confirmação final
    updated = svc.get_brand(HUGOBOSS_KEY)
    if updated and updated.mappings:
        print(f"\n[OK] {len(updated.mappings)} mappings gravados em brands.json para '{HUGOBOSS_KEY}':")
        for m in updated.mappings:
            print(f"  {m.canonical_slug:12s} <- {m.label!r}  ({m.vtex_fq_path})")
    else:
        print("[WARN] Nenhum mapping foi persistido após a confirmação. Verifique os logs acima.")


if __name__ == "__main__":
    asyncio.run(main())
