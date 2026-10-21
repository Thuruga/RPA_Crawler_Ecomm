"""
Mapeamento Canônico de Categorias — De/Para Multi-Marca.

Define um vocabulário único de categorias semânticas e mapeia cada uma
para o path VTEX real de cada marca suportada.  Isso garante que ao
selecionar "Camisas", a varredura use o caminho correto em Aramis,
Reserva e Tommy simultaneamente ("banana com banana").

Fontes:
    - config.py → BRAND_REGISTRY (domínios)
    - category_resolver.py → _BRAND_CATEGORY_PATHS (referência original)
    - vtex_catalog.py → STATIC_FALLBACK (paths do frontend)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from services.brand_service import brand_service


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BrandCategoryInfo:
    """Informações de uma categoria para uma marca específica."""
    path: str          # Path relativo usado na URL do site (ex: /roupas/camisas)
    vtex_fq: str       # Filtro VTEX fq=C:/... para busca exata


@dataclass(frozen=True)
class CanonicalCategory:
    """Categoria canônica com seus mapeamentos para cada marca."""
    slug: str
    label: str
    group: str                                      # Agrupamento visual (ex: "Roupas")
    brands: Dict[str, BrandCategoryInfo]             # brand_key → info


# ---------------------------------------------------------------------------
# Definição Centralizada — De/Para
# ---------------------------------------------------------------------------
_RAW_CATEGORIES: List[dict] = [
    # ── Roupas ──────────────────────────────────────────────────────────
    {
        "slug": "camisas",
        "label": "Camisas",
        "group": "Roupas",
        "brands": {
            "aramis":  {"path": "/camisa-masculina",              "vtex_fq": ""},
            "reserva": {"path": "/reserva/masculino/camisas",     "vtex_fq": "C:/1/101/10103/"},
            "tommy":   {"path": "/roupas/camisas",                "vtex_fq": "C:/1/5/"},
        },
    },
    {
        "slug": "polos",
        "label": "Polos",
        "group": "Roupas",
        "brands": {
            "aramis":  {"path": "/polo-masculina",                "vtex_fq": ""},
            "reserva": {"path": "/reserva/masculino/polos",       "vtex_fq": "C:/1/101/10113/"},
            "tommy":   {"path": "/roupas/polos",                  "vtex_fq": "C:/1/18/"},
        },
    },
    {
        "slug": "camisetas",
        "label": "Camisetas",
        "group": "Roupas",
        "brands": {
            "aramis":  {"path": "/camiseta-masculina",            "vtex_fq": ""},
            "reserva": {"path": "/reserva/masculino/camisetas",   "vtex_fq": "C:/1/101/10104/"},
            "tommy":   {"path": "/roupas/camisetas",              "vtex_fq": "C:/1/19/"},
        },
    },
    {
        "slug": "calcas",
        "label": "Calças",
        "group": "Roupas",
        "brands": {
            "aramis":  {"path": "/calca-masculina",               "vtex_fq": ""},
            "reserva": {"path": "/reserva/masculino/calcas",      "vtex_fq": "C:/1/101/10102/"},
            "tommy":   {"path": "/roupas/calcas",                 "vtex_fq": "C:/1/4/"},
        },
    },
    {
        "slug": "bermudas",
        "label": "Bermudas & Shorts",
        "group": "Roupas",
        "brands": {
            "aramis":  {"path": "/bermuda-masculina",             "vtex_fq": ""},
            "reserva": {"path": "/reserva/masculino/bermudas-e-shorts", "vtex_fq": "C:/1/101/10101/"},
            "tommy":   {"path": "/roupas/bermudas",               "vtex_fq": "C:/1/10/"},
        },
    },
    {
        "slug": "jaquetas",
        "label": "Jaquetas & Casacos",
        "group": "Roupas",
        "brands": {
            "aramis":  {"path": "/jaqueta-masculina",             "vtex_fq": ""},
            "reserva": {"path": "/reserva/masculino/jaquetas-e-casacos", "vtex_fq": "C:/1/101/10105/"},
            "tommy":   {"path": "/roupas/jaquetas",               "vtex_fq": "C:/1/6/"},
        },
    },
    # ── Segmentos Especiais ─────────────────────────────────────────────
    {
        "slug": "infantil",
        "label": "Infantil",
        "group": "Segmentos",
        "brands": {
            "aramis":  {"path": "/infantil",                      "vtex_fq": "C:/582/"},
            "reserva": {"path": "/mini",                          "vtex_fq": "C:/2/"},
            "tommy":   {"path": "/infantil",                      "vtex_fq": "B:2000003"},
            "hering":  {"path": "/kids",                          "vtex_fq": "B:2000004"},
            "lacoste": {"path": "/br/infantil.html",              "vtex_fq": "infantil"},
        },
    },
]


# ---------------------------------------------------------------------------
# Índice para acesso rápido
# ---------------------------------------------------------------------------
_CATEGORY_INDEX: Dict[str, CanonicalCategory] = {}

for _raw in _RAW_CATEGORIES:
    _brands_map = {
        bk: BrandCategoryInfo(path=bv["path"], vtex_fq=bv["vtex_fq"])
        for bk, bv in _raw["brands"].items()
    }
    _cat = CanonicalCategory(
        slug=_raw["slug"],
        label=_raw["label"],
        group=_raw["group"],
        brands=_brands_map,
    )
    _CATEGORY_INDEX[_cat.slug] = _cat


def _category_source_url(domain: str, path_or_url: str) -> str:
    """Build one category URL without prepending a host twice.

    Durable discovery mappings can contain either a relative source path or
    the complete URL returned by a storefront crawler.  The category picker
    submits the latter verbatim, so blindly prefixing ``domain`` produces
    ``https://host/https://host/...`` and makes Wake query ``/https://...``.
    """
    value = (path_or_url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    path = value if value.startswith("/") else f"/{value}"
    return f"https://{domain}{path}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_canonical_categories() -> List[dict]:
    """
    Retorna todas as categorias canônicas agrupadas, pronto para o frontend.
    Agora também inclui categorias customizadas vindas das marcas dinâmicas.
    """
    groups: Dict[str, list] = {}
    
    # 1. Adiciona as hardcoded
    for cat in _CATEGORY_INDEX.values():
        if cat.group not in groups:
            groups[cat.group] = []
        groups[cat.group].append({
            "slug": cat.slug,
            "label": cat.label,
            "available_brands": list(cat.brands.keys()),
        })

    # 2. Adiciona as dinâmicas de todas as marcas (para garantir que apareçam no select)
    for brand in brand_service.list_brands():
        for mapping in brand.mappings:
            # Verifica se já existe
            exists = False
            for group_items in groups.values():
                if any(i["slug"] == mapping.canonical_slug for i in group_items):
                    # Se já existe, apenas adiciona a marca na lista de disponíveis se não estiver lá
                    for item in group_items:
                        if item["slug"] == mapping.canonical_slug:
                            if brand.brand_key not in item["available_brands"]:
                                item["available_brands"].append(brand.brand_key)
                    exists = True
                    break
            
            if not exists:
                group_name = "Custom"
                if group_name not in groups:
                    groups[group_name] = []
                groups[group_name].append({
                    "slug": mapping.canonical_slug,
                    "label": mapping.label,
                    "available_brands": [brand.brand_key],
                })

    return [
        {"group": group_name, "categories": items}
        for group_name, items in groups.items()
    ]


def resolve_category_for_brands(
    category_slug: str,
    brand_keys: List[str],
) -> Dict[str, dict]:
    """
    Dado um slug canônico e lista de marcas, retorna o mapeamento de/para.
    Busca primeiro no índice hardcoded e depois nos mapeamentos dinâmicos das marcas.
    """
    result: Dict[str, dict] = {}
    missing_brands: List[str] = []

    for bk in brand_keys:
        bk_lower = bk.lower()
        brand_data = brand_service.get_brand(bk_lower)
        if not brand_data:
            missing_brands.append(bk)
            continue

        domain = brand_data.domain
        
        # 1. Tenta no índice hardcoded
        cat = _CATEGORY_INDEX.get(category_slug)
        if cat and bk_lower in cat.brands:
            brand_info = cat.brands[bk_lower]
            result[bk_lower] = {
                "url": _category_source_url(domain, brand_info.path),
                "path": brand_info.path,
                "label": cat.label,
            }
            continue

        # 2. Tenta nos mapeamentos da própria marca (DynamicBrand)
        dynamic_mapping = next((m for m in brand_data.mappings if m.canonical_slug == category_slug), None)
        if dynamic_mapping:
            # O mapping dinâmico guarda o path no vtex_fq_path se for um path simples,
            # ou precisamos garantir que temos o path da URL.
            # No modelo atual, vtex_fq_path parece ser usado para ambos em casos simples.
            path = dynamic_mapping.vtex_fq_path
            if not path.startswith("/"):
                # Se for um FQ (C:/...), não conseguimos gerar a URL da categoria diretamente
                # para o orchestrator sem saber o path amigável.
                # Mas para marcas dinâmicas, o usuário costuma mapear o path.
                pass 
                
            result[bk_lower] = {
                "url": _category_source_url(domain, path),
                "path": path,
                "label": dynamic_mapping.label,
            }
            continue

        missing_brands.append(bk)

    if missing_brands and not result:
        # Só lança erro se NENHUMA marca pôde ser resolvida
        raise ValueError(
            f"Categoria '{category_slug}' não encontrada para as marcas selecionadas."
        )

    return result



def get_category_preview(
    category_slug: str,
    brand_keys: List[str],
) -> Optional[dict]:
    """
    Retorna preview do mapeamento de/para para o frontend mostrar antes de iniciar.
    Agora consulta tanto o índice hardcoded quanto os mapeamentos dinâmicos das marcas.

    Returns:
        {
            "category": "Camisas",
            "slug": "camisas",
            "mappings": [
                {"brand": "aramis", "brand_name": "Aramis", "url": "https://..."},
                {"brand": "reserva", "brand_name": "Reserva", "url": "https://..."},
            ]
        }
    """
    mappings = []
    category_label = None

    for bk in brand_keys:
        bk_lower = bk.lower()
        brand_data = brand_service.get_brand(bk_lower)
        if not brand_data:
            continue

        domain = brand_data.domain
        brand_name = brand_data.brand_name

        # 1. Tenta no índice hardcoded
        cat = _CATEGORY_INDEX.get(category_slug)
        if cat and bk_lower in cat.brands:
            brand_info = cat.brands[bk_lower]
            category_label = cat.label
            mappings.append({
                "brand": bk_lower,
                "brand_name": brand_name,
                "path": brand_info.path,
                "url": _category_source_url(domain, brand_info.path),
            })
            continue

        # 2. Tenta nos mapeamentos dinâmicos da marca
        dynamic_mapping = next(
            (m for m in brand_data.mappings if m.canonical_slug == category_slug), None
        )
        if dynamic_mapping:
            path = dynamic_mapping.vtex_fq_path
            if not path.startswith(("/", "http://", "https://")):
                path = "/" + path
            category_label = category_label or dynamic_mapping.label
            mappings.append({
                "brand": bk_lower,
                "brand_name": brand_name,
                "path": path,
                "url": _category_source_url(domain, path),
            })

    if not mappings:
        return None

    return {
        "category": category_label or category_slug,
        "slug": category_slug,
        "mappings": mappings,
    }
