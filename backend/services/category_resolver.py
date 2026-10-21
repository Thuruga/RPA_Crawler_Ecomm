from typing import Optional

# Mapeamento de termos buscados pelo usuário -> 'slug' canônico
_TERM_ALIASES = {
    "camisa": "camisas",
    "camisas": "camisas",
    "camisa social": "camisas",
    
    "polo": "polos",
    "polos": "polos",
    "camisa polo": "polos",
    
    "camiseta": "camisetas",
    "camisetas": "camisetas",
    "t-shirt": "camisetas",
    "tshirt": "camisetas",
    
    "calca": "calcas",
    "calcas": "calcas",
    "calça": "calcas",
    "calças": "calcas",
    
    "bermuda": "bermudas",
    "bermudas": "bermudas",
    "short": "bermudas",
    "shorts": "bermudas",
    
    "jaqueta": "jaquetas",
    "jaquetas": "jaquetas",
    "casaco": "jaquetas",
    "casacos": "jaquetas",
    
    "infantil": "infantil",
    "kids": "infantil",
    "menino": "infantil",
    "criança": "infantil",
    "crianca": "infantil",
}

# VTEX usa fq=C:/{dept}/{category}/...
# Abaixo mapeamos o slug canônico -> caminho exato de categoria na VTEX para cada marca.
# Isso garante que ao buscar "camisa", não traga infantil, nem feminino, nem polos.
_BRAND_CATEGORY_PATHS = {
    "aramis": {
        "camisas": "C:/480/507/",
        "polos": "C:/480/523/",
        "camisetas": "C:/480/510/",
        "calcas": "C:/480/501/",
        "bermudas": "C:/480/491/",
        "jaquetas": "C:/480/514/",
        "infantil": "C:/582/",
    },
    "reserva": {
        "camisas": "C:/1/101/10103/",
        "polos": "C:/1/101/10113/",
        "camisetas": "C:/1/101/10104/",
        "calcas": "C:/1/101/10102/",
        "bermudas": "C:/1/101/10101/",
        "jaquetas": "C:/1/101/10105/",
        "infantil": "C:/2/",
    },
    "tommy": {
        "camisas": "C:/1/5/",
        "polos": "C:/1/18/",
        "camisetas": "C:/1/19/",
        "calcas": "C:/1/4/",
        "bermudas": "C:/1/10/",
        "jaquetas": "C:/1/6/",
        "infantil": "B:2000003",
    },
    "hering": {
        "camisas": "/masculino/camisas",
        "polos": "/masculino/polos",
        "camisetas": "/masculino/camisetas",
        "calcas": "/masculino/calcas",
        "bermudas": "/masculino/bermudas",
        "jaquetas": "/masculino/jaquetas-e-casacos",
        "moletom": "/masculino/moletom",
        "infantil": "B:2000004",
    },
}

def resolve_query_to_vtex_category_path(query: str, brand_key: str) -> Optional[str]:
    """
    Resolve a query de busca livre para um Path de Categoria VTEX estrito (fq=C:/...).
    Se a query nao for uma categoria conhecida, retorna None (usa busca full-text).

    ORDEM DE PRECEDENCIA:
    1. Tabela `brand_category_mappings` no PostgreSQL (via brand_service) -- FONTE PRIMARIA.
       Edicoes no painel de administracao refletem imediatamente aqui.
    2. Dicionario `_BRAND_CATEGORY_PATHS` abaixo -- FALLBACK LEGADO.
       Mantido apenas enquanto as marcas existentes nao tiverem mapeamentos completos no banco.
    """
    query_clean = query.strip().lower()
    canonical_slug = _TERM_ALIASES.get(query_clean)

    if not canonical_slug:
        return None

    # 1. Consulta o banco de dados primeiro (fonte primaria)
    from services.brand_service import brand_service
    brand_data = brand_service.get_brand(brand_key.lower())
    if brand_data:
        for mapping in brand_data.mappings:
            if mapping.canonical_slug == canonical_slug and mapping.vtex_fq_path:
                return mapping.vtex_fq_path

    # 2. Fallback legado: dicionario estatico (deprecado -- migrar para o banco)
    brand_paths = _BRAND_CATEGORY_PATHS.get(brand_key.lower())
    if brand_paths and canonical_slug in brand_paths:
        return brand_paths[canonical_slug]

    return None
