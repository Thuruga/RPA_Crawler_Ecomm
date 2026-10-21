"""
Onboarding idempotente das 5 marcas concorrentes VTEX (Phase 26, COMP-01).

Execucao: python scripts/onboard_vtex_brands.py
Re-executavel sem duplicar marcas ou sobrescrever mappings corretos.

Este script orquestra o fluxo EXISTENTE:
  1. create_brand-equivalente com reconfirmacao de engine via detect_engine
  2. discover_categories (VTEXEngine) + auto-match para slugs canonicos
  3. Revisao humana do de/para proposto (D-09)
  4. persist_mappings via brand_service.update_mappings (dual dev/prod — D-08)

Nao inventa logica de negocio nova — delega totalmente a camada service/engine.
"""
import asyncio
import unicodedata
import sys
import os
from typing import NamedTuple, Optional
from urllib.parse import urlparse

# Garante que o diretório raiz do projeto esteja no sys.path para imports absolutos funcionarem
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.brand_service import brand_service
from services.engines.vtex_engine import VTEXEngine
from services.engines.factory import engine_factory
from services.category_policy import (
    CategoryCandidate,
    FEMININE_ALIASES,
    MASCULINE_ADULT_ALIASES,
    MASCULINE_CHILD_ALIASES,
    category_matches_keywords,
)
from core.models import DynamicBrandCreate, DynamicBrand, CategoryMapping


# ---------------------------------------------------------------------------
# Contrato de retorno de onboard_brand (WR-04) — explicito em vez de dunder
# ---------------------------------------------------------------------------

class OnboardResult(NamedTuple):
    """Resultado de onboard_brand.

    - brand=None              -> marca nao onboardada (engine != 'vtex').
    - skip_mappings=True      -> mappings ja existem e operador recusou
                                 sobrescrever; main deve PULAR discovery/persist
                                 e manter os mappings atuais (D-06 idempotency).
    """
    brand: "Optional[DynamicBrand]"
    skip_mappings: bool = False

# ---------------------------------------------------------------------------
# Constantes — D-01 (dominios verbatim, sem esquema)
# ---------------------------------------------------------------------------

BRAND_TABLE = [
    ("levis",       "Levi's",       "www.levi.com.br"),
    ("calvinklein", "Calvin Klein", "www.calvinklein.com.br"),
    ("zapalla",     "Zapalla",      "www.zapalla.com.br"),
    ("austral",     "Austral",      "www.austral.com.br"),   # D-02: testar www. antes de secure.
    ("trackfield",  "Track & Field", "www.tf.com.br"),
]

# D-02 / D-11: variantes de dominio para Austral (www. primeiro, conforme RESEARCH)
AUSTRAL_DOMAIN_CANDIDATES = [
    "www.austral.com.br",
    "austral.com.br",
    "secure.austral.com.br",
]

# ---------------------------------------------------------------------------
# Auto-match — slugs canonicos ancorados em _RAW_CATEGORIES (D-04)
# ---------------------------------------------------------------------------

# Regra do operador: o de/para deve conter SOMENTE categorias masculinas (nunca
# femininas) e, para "infantil", somente a linha do menino. Os marcadores de
# genero (feminino/masculino/infantil) agora vivem em category_policy.py (Wave 0,
# D-02/D-06) e sao testados por TOKEN (word boundary real) — nunca substring —
# via category_matches_keywords, o que corrige o bypass 'calcas' vs 'calcados'.
_INACTIVE_MARKERS = ("inativo", "inativa")             # categorias arquivadas/mortas

CANONICAL_KEYWORDS = {
    # slugs adultos — match por TOKEN (nao substring) no nome/path; por isso cada
    # forma flexionada (singular/plural) e listada explicitamente.
    "camisas":   ["camisa", "camisas"],
    "polos":     ["polo", "polos"],
    "camisetas": ["camiseta", "camisetas"],
    "calcas":    ["calca", "calcas", "jeans", "denim"],
    "bermudas":  ["bermuda", "bermudas", "short", "shorts"],
    "jaquetas":  ["jaqueta", "jaquetas", "casaco", "casacos", "moletom", "moleton", "sueter", "tricot"],
    # infantil — tratado a parte (somente menino/masculino). "mini" e palavra
    # valida de infantil (algumas marcas rotulam kids como "Mini"), porem casa
    # por TOKEN (palavra inteira) para NAO casar dentro de "feminino"/"femininas".
    "infantil":  ["infantil", "infantis", "kids", "mini", "teen", "menino"],
}


def normalize(text: str) -> str:
    """Lowercase + remove acentos (stdlib unicodedata)."""
    text = text.lower()
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _blob(item) -> str:
    """normalize('<rel_path> <name>') — base de todos os marcadores."""
    return normalize(f"{item.get('rel_path', '')} {item.get('name', '')}")


def _is_feminine(blob: str) -> bool:
    return category_matches_keywords(blob, FEMININE_ALIASES)


def _is_inactive(blob: str) -> bool:
    return any(m in blob for m in _INACTIVE_MARKERS)


def _is_masculine(blob: str) -> bool:
    return category_matches_keywords(blob, MASCULINE_ADULT_ALIASES | MASCULINE_CHILD_ALIASES)


def _is_child(blob: str) -> bool:
    """Categoria infantil: keyword de infantil presente como TOKEN (palavra
    inteira) — corrige o bug de 'mini' casando dentro de 'feminino'."""
    return category_matches_keywords(blob, CANONICAL_KEYWORDS["infantil"])


def _adult_rank(cand, primary, keywords):
    _it, path, blob = cand
    last = normalize(path.rstrip("/").split("/")[-1])
    hits = sum(1 for kw in keywords if kw in blob)
    return (
        0 if _is_masculine(blob) else 1,        # masculino antes de neutro
        0 if last.startswith(primary) else 1,   # nome canonico antes de variante
        path.count("/"),                        # mais raso antes de mais profundo (evita sub-subcategoria)
        -hits,                                  # entre mesma profundidade, prefere a mais abrangente
        len(path),
    )


def _child_rank(cand):
    _it, path, blob = cand
    return (0 if _is_masculine(blob) else 1, path.count("/"), len(path))


def auto_match(categories):
    """De/para genero-consciente -> lista de (canonical_slug, rel_path, label).

    Regras (D-09 + operador):
      - Exclui categorias femininas (feminin/menina/mulher) e inativas (inativo).
      - Slugs adultos (todos menos 'infantil'): escolhe categoria ADULTA (nunca
        infantil); prefere segmento masculino; aceita neutra (sem genero) quando
        a marca nao segmenta por genero. De-duplica: 1 categoria por slug.
      - 'infantil': escolhe categoria infantil NAO feminina; prefere a do menino
        (masculino). Omitida se a marca nao tiver categoria infantil elegivel.
    """
    enriched = []
    for it in categories:
        path = it.get("rel_path") or ""
        if not path.startswith("/"):
            continue
        enriched.append((it, path, _blob(it)))

    proposals = []

    # --- slugs adultos (somente masculino/neutro; nunca feminino/infantil/inativo) ---
    for slug, keywords in CANONICAL_KEYWORDS.items():
        if slug == "infantil":
            continue
        cands = []
        for it, path, blob in enriched:
            if _is_feminine(blob) or _is_inactive(blob) or _is_child(blob):
                continue
            # Matching por TOKEN (nao substring) — evita 'calcas' casando dentro
            # de 'calcados' (mesmo prefixo, tokens distintos).
            if category_matches_keywords(CategoryCandidate(name=it.get("name", ""), path=path), keywords):
                cands.append((it, path, blob))
        if not cands:
            continue
        primary = keywords[0]
        it, path, _blob_sel = min(cands, key=lambda c: _adult_rank(c, primary, keywords))
        proposals.append((slug, path, it.get("name", "")))

    # --- infantil (somente menino/masculino; nunca feminino/inativo) ---
    child_cands = [
        c for c in enriched
        if not _is_inactive(c[2]) and not _is_feminine(c[2]) and _is_child(c[2])
    ]
    if child_cands:
        it, path, _blob_sel = min(child_cands, key=_child_rank)
        proposals.append(("infantil", path, it.get("name", "")))

    return proposals


# ---------------------------------------------------------------------------
# Austral domain resolution — D-11 (retry; "unknown" nao e estado final)
# ---------------------------------------------------------------------------

async def resolve_austral_domain(svc) -> "str | None":
    """Tenta AUSTRAL_DOMAIN_CANDIDATES ate detect_engine reconfirmar 'vtex'.

    Retorna o dominio confirmado ou None se nenhuma variante retornou 'vtex'.
    NUNCA hardcoda engine='vtex' manualmente — apenas aceita reconfirmacao.
    """
    from api.routes_brands import detect_engine

    for candidate in AUSTRAL_DOMAIN_CANDIDATES:
        print(f"  [Austral] Tentando {candidate!r}...", end=" ", flush=True)
        detected_engine, _home_html = await detect_engine(candidate)
        print(detected_engine)
        if detected_engine == "vtex":
            return candidate

    print(
        "[FAIL] Austral: nenhuma variante detectou vtex — investigar manualmente"
    )
    return None


# ---------------------------------------------------------------------------
# onboard_brand — add_brand + engine reconfirmation/fix + idempotency
# ---------------------------------------------------------------------------

async def onboard_brand(svc, brand_key: str, brand_name: str, domain: str) -> OnboardResult:
    """Cadastra/atualiza a marca e garante engine e is_active corretos.

    Sequencia:
      1. DynamicBrandCreate(engine='auto') -> add_brand
      2. DEFUSE UPSERT BUG: detect_engine reconfirma e corrige engine + _save
      3. Austral: se engine != 'vtex', tenta variantes de dominio
      4. engine != 'vtex' -> set_active(False), OnboardResult(brand=None)
      5. engine == 'vtex' -> set_active(True)
      6. Mappings idempotency: se ja populados, pede confirmacao antes de
         sobrescrever; recusa -> OnboardResult(brand, skip_mappings=True) (WR-04)
    """
    from api.routes_brands import detect_engine

    data = DynamicBrandCreate(
        brand_key=brand_key,
        brand_name=brand_name,
        domain=domain,
        engine="auto",
    )

    # 1. add_brand: upsert que NAO atualiza engine/is_active no modo update (landmine)
    brand = svc.add_brand(data)
    print(f"[{brand_key}] add_brand: domain={brand.domain!r}, engine={brand.engine!r}")

    # WR-05 fail-safe: add_brand persiste engine='auto' e is_active=True por
    # default. Se o script abortar entre add_brand e a reconfirmacao do engine,
    # uma marca ficaria ativa com engine nao reconfirmado. Inativa-la agora
    # garante que so volta a ativa apos detect_engine confirmar 'vtex' (passo 5).
    svc.set_active(brand_key, False)

    # 2. DEFUSE THE UPSERT BUG — detectar e corrigir engine stale
    detected, _home_html = await detect_engine(brand.domain)
    print(f"[{brand_key}] detect_engine({brand.domain!r}) -> {detected!r}")
    if brand.engine != detected:
        print(f"[{brand_key}] Corrigindo engine: {brand.engine!r} -> {detected!r}")
        brand.engine = detected
        svc._save(brand)
    # Re-ler para garantir que o objeto inspecionado e o persistido
    brand = svc.get_brand(brand_key)

    # 3. Austral: se engine != 'vtex', tentar variantes de dominio (D-11)
    if brand_key == "austral" and brand.engine != "vtex":
        print(f"[austral] engine={brand.engine!r} — tentando variantes de dominio...")
        working_domain = await resolve_austral_domain(svc)
        if working_domain is not None:
            # detect_engine ja reconfirmou 'vtex' para este dominio — nao e override manual
            brand.domain = working_domain
            brand.engine = "vtex"
            svc._save(brand)
            brand = svc.get_brand(brand_key)
            print(f"[austral] Dominio resolvido: {working_domain!r}, engine={brand.engine!r}")
        else:
            svc.set_active(brand_key, False)
            return OnboardResult(brand=None)

    # 4. engine != 'vtex' -> inativar e retornar None
    if brand.engine != "vtex":
        print(f"[WARN] {brand_key}: engine={brand.engine!r} — nao onboardada")
        svc.set_active(brand_key, False)
        return OnboardResult(brand=None)

    # 5. engine == 'vtex' -> ativar
    svc.set_active(brand_key, True)
    brand = svc.get_brand(brand_key)

    # 6. Mappings idempotency: se ja populados, perguntar antes de sobrescrever (D-06)
    if brand.mappings:
        print(
            f"[INFO] {brand_key}: {len(brand.mappings)} mappings ja existem. "
            f"Sobrescrever? [s/N] ",
            end="",
            flush=True,
        )
        ans = input().strip().lower()
        if ans != "s":
            # WR-04: operador recusou sobrescrever — sinaliza para main PULAR
            # discovery/persist, evitando a segunda confirmacao que reabria o
            # gate de overwrite e contradizia a decisao do operador (D-06).
            return OnboardResult(brand=brand, skip_mappings=True)

    return OnboardResult(brand=brand)


# ---------------------------------------------------------------------------
# discover_and_match — VTEXEngine + urlparse path extraction
# ---------------------------------------------------------------------------

async def discover_and_match(svc, brand_key: str) -> "tuple[int, list]":
    """Descobre categorias via VTEXEngine e auto-matcha para slugs canonicos.

    Pitfall 3/5 DEFUSED: item["path"] e URL completa — extrai path relativo via urlparse.
    Apenas instancia VTEXEngine APOS add_brand garantido (Pitfall 4).

    Retorna (discovered_count, proposals) para que o chamador distinga uma
    falha de descoberta (discovered_count == 0) de "descobriu categorias mas
    nenhuma deu match" (discovered_count > 0, proposals vazias) — WR-03.
    """
    engine = VTEXEngine(brand_key)
    raw = await engine.discover_categories()
    for item in raw:
        # Defensivo: nodes malformados (path ausente/None) nao devem abortar
        # o onboarding inteiro. urlparse("") -> path "" e persist_mappings ja
        # filtra rel_path que nao comeca com "/".
        item["rel_path"] = urlparse(item.get("path") or "").path
    return len(raw), auto_match(raw)


# ---------------------------------------------------------------------------
# print_and_confirm — D-09 human review gate
# ---------------------------------------------------------------------------

def print_and_confirm(brand_key: str, proposals: list) -> bool:
    """Exibe o de/para proposto e solicita confirmacao do operador.

    Mostra tambem quais slugs canonicos nao tiveram match (Open Question 2).
    """
    print(f"\n--- {brand_key} --- de/para proposto ---")
    for slug, path, label in proposals:
        print(f"  {slug:12s} <- {label!r}  ({path})")

    matched_slugs = {slug for slug, _, _ in proposals}
    unmatched = sorted(CANONICAL_KEYWORDS.keys() - matched_slugs)
    if unmatched:
        print(f"  [SEM MATCH] slugs sem categoria encontrada: {unmatched}")

    print("Confirmar? [s/N] ", end="", flush=True)
    return input().strip().lower() == "s"


# ---------------------------------------------------------------------------
# persist_mappings — CategoryMapping list + update_mappings (D-08)
# ---------------------------------------------------------------------------

def persist_mappings(svc, brand_key: str, proposals: list) -> None:
    """Constroi CategoryMapping list e persiste via update_mappings (dual dev/prod).

    Guarda: ignora proposals cujo rel_path nao comeca com '/' (invariante relativo).
    """
    valid_proposals = [
        (slug, rel_path, label)
        for slug, rel_path, label in proposals
        if rel_path.startswith("/")
    ]
    if len(valid_proposals) < len(proposals):
        skipped = len(proposals) - len(valid_proposals)
        print(f"[WARN] {brand_key}: {skipped} proposals ignoradas (vtex_fq_path nao relativo)")

    mappings = [
        CategoryMapping(canonical_slug=slug, vtex_fq_path=rel_path, label=label)
        for slug, rel_path, label in valid_proposals
    ]
    svc.update_mappings(brand_key, mappings)
    print(f"[{brand_key}] {len(mappings)} mappings persistidos.")


# ---------------------------------------------------------------------------
# main — loop sobre BRAND_TABLE + per-brand live smoke (D-10a)
# ---------------------------------------------------------------------------

async def main() -> None:
    svc = brand_service  # singleton — dual persistence automatica (D-08)

    onboarded: list[str] = []
    skipped: list[str] = []
    partial: list[str] = []   # WR-03: descoberta falhou ou 0 mappings persistidos

    for brand_key, brand_name, domain in BRAND_TABLE:
        print(f"\n{'='*60}")
        print(f"Onboardando: {brand_name} ({brand_key}) — {domain}")
        print(f"{'='*60}")

        result = await onboard_brand(svc, brand_key, brand_name, domain)

        if result.brand is None:
            print(f"[SKIP] {brand_key}: nao onboardada — verificar dominio.")
            skipped.append(brand_key)
            continue

        # WR-04: operador recusou sobrescrever mappings existentes — honra a
        # decisao do passo 6 e NAO reabre discovery/persist (que reapresentaria
        # o gate de overwrite e poderia sobrescrever assim mesmo).
        if result.skip_mappings:
            print(f"[KEEP] {brand_key}: mantendo mappings existentes (operador recusou sobrescrever).")
            onboarded.append(brand_key)
            continue

        # Discovery + auto-match + revisao humana + persistencia de mappings
        discovered_count, proposals = await discover_and_match(svc, brand_key)

        if discovered_count == 0:
            # WR-03: discover_categories retornou vazio — provavel falha de
            # rede/dominio, indistinguivel de "site sem categorias". NAO marca
            # como onboardada com 0 mappings (sinal de sucesso enganoso).
            print(f"[WARN] {brand_key}: discover_categories vazio — possivel falha de rede/dominio.")
            partial.append(brand_key)
        elif proposals:
            if print_and_confirm(brand_key, proposals):
                persist_mappings(svc, brand_key, proposals)
                onboarded.append(brand_key)
            else:
                print(f"[SKIP] {brand_key}: mappings nao confirmados pelo operador.")
                partial.append(brand_key)
        else:
            # Descobriu categorias mas nenhuma deu match nos slugs canonicos.
            print(f"[WARN] {brand_key}: {discovered_count} categorias descobertas, nenhum match — verificar keywords.")
            partial.append(brand_key)

        # Live smoke (D-10a) — print apenas, nao falha em 0 resultados (D-10)
        try:
            results = await engine_factory.search_all_brands(
                "camisa", brands=[brand_key], max_per_brand=3
            )
            count = sum(len(r.products) for r in results if hasattr(r, "products")) if results else 0
            print(f"[SMOKE] {brand_key}: {count} produtos")
        except Exception as exc:  # noqa: BLE001
            print(f"[SMOKE] {brand_key}: erro na busca ({exc})")

    print(f"\n{'='*60}")
    print("Onboarding concluido.")
    print(f"  Onboardadas : {onboarded}")
    print(f"  Parciais    : {partial}")
    print(f"  Nao onboard.: {skipped}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
