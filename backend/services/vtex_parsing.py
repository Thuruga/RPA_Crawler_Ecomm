"""
Funções puras de parsing/normalização da integração VTEX.

Extraídas de ``VtexApiClient`` para isolar a lógica determinística (sem rede,
sem sessão HTTP, sem estado) e permitir teste unitário. ``VtexApiClient``
continua sendo a fachada pública e delega para estas funções.
"""
import re
from typing import List
from urllib.parse import urlparse

from core.vtex_schemas import VtexProduct, VtexItem
from services.attribute_parsing import extract_color_fallback, extract_size_fallback


# Chaves de especificação que carregam cor na API Catalog System da VTEX.
_COLOR_SPEC_KEYS = ["Cor", "Color", "Cor Real", "Cores", "COR", "DESC_COR_ORIGINAL", "Cores Filtráveis"]


def discover_account_from_html(domain: str, html_content: str) -> str:
    """Extrai o nome da conta VTEX de um HTML bruto (CDN vtexassets ou domínio)."""
    # 1. Procura pela CDN
    vtexassets_match = re.search(r"https:\/\/([^.]+)\.vtexassets\.com", html_content)
    if vtexassets_match:
        return vtexassets_match.group(1)

    # 2. Fallback pelo domínio
    account_match = re.search(r"^(?:www\.)?([^.]+)", domain)
    return account_match.group(1) if account_match else domain.split(".")[0]


def transform_url_to_api(url: str) -> str:
    """Converte uma URL de produto frontend para a API Catalog System."""
    url = url.strip()
    if "/api/catalog_system/" in url:
        return url
    parsed = urlparse(url)
    dominio = f"{parsed.scheme}://{parsed.netloc}"
    slug = parsed.path.strip("/").split("/")[-1]
    if slug == "p":
        slug = parsed.path.strip("/").split("/")[-2]
    return f"{dominio}/api/catalog_system/pub/products/search/{slug}/p"


def sanitize_product_url(url: str, public_domain: str) -> str:
    """Garante que o link aponte para o domínio público (www.brand.com.br)."""
    if not url or not public_domain:
        return url
    clean_domain = public_domain.replace("https://", "").replace("http://", "").strip("/")
    if not clean_domain:
        return url
    parsed_p = urlparse(url)
    if parsed_p.netloc and parsed_p.netloc.lower() != clean_domain.lower():
        return url.replace(parsed_p.netloc, clean_domain)
    return url


def extract_colors(
    p: VtexProduct,
    aliases: List[str] = None,
    extract_via_regex: bool = False,
) -> List[str]:
    """Varre as especificações em busca das cores na API Catalog System.

    Preserva o label exatamente como a VTEX declarou (capitalização/ordem), sem
    `set()`/`.upper()` destrutivos (D-01): a normalização (case-fold, mapa de
    aliases) é responsabilidade explícita de outra camada
    (``product_contract.normalize_color_values``), nunca desta extração bruta.
    Ausência de especificação de cor produz ``[]``, nunca um valor inventado.
    """
    cores_encontradas: List[str] = []

    spec_keys = list(_COLOR_SPEC_KEYS)
    if aliases:
        spec_keys.extend(aliases)

    # Foca em allSpecifications, que é a fonte mais confiável de cor.
    for spec_name in p.allSpecifications:
        if spec_name in spec_keys:
            valores = getattr(p, spec_name, [])
            if isinstance(valores, list):
                for valor in valores:
                    label = str(valor).strip()
                    if extract_via_regex:
                        label = extract_color_fallback(label)
                    if label and label not in cores_encontradas:
                        cores_encontradas.append(label)

    # Se não encontrar na raiz, tenta extrair das variações dos SKUs (fallback)
    if not cores_encontradas:
        for item in p.items:
            for spec_name in spec_keys:
                if hasattr(item, spec_name):
                    valores = getattr(item, spec_name, [])
                    if isinstance(valores, list):
                        for valor in valores:
                            label = str(valor).strip()
                            if extract_via_regex:
                                label = extract_color_fallback(label)
                            if label and label not in cores_encontradas:
                                cores_encontradas.append(label)
                # Opcional: tenta buscar em dict ou extras (Pydantic __pydantic_extra__)
                elif hasattr(item, "__pydantic_extra__") and item.__pydantic_extra__ and spec_name in item.__pydantic_extra__:
                    valores = item.__pydantic_extra__[spec_name]
                    if isinstance(valores, list):
                        for valor in valores:
                            label = str(valor).strip()
                            if extract_via_regex:
                                label = extract_color_fallback(label)
                            if label and label not in cores_encontradas:
                                cores_encontradas.append(label)

    return cores_encontradas


def _size_label(item: VtexItem, extract_via_regex: bool) -> str:
    # 1. Procura primeiro nas especificações/variações de tamanho do SKU (ex: Guess e outras lojas VTEX)
    size_spec_keys = ["Tamanho", "tamanho", "Size", "size", "TAMANHO", "Tamanhos", "TAM"]
    for k in size_spec_keys:
        val = getattr(item, k, None)
        if not val and hasattr(item, "__pydantic_extra__") and item.__pydantic_extra__:
            val = item.__pydantic_extra__.get(k)
        if val:
            if isinstance(val, list) and len(val) > 0:
                s = str(val[0]).strip()
                if s:
                    return s
            elif isinstance(val, str) and val.strip():
                return val.strip()

    # 1b. Varre item.variations se alguma contiver tamanho/size
    if hasattr(item, "variations") and item.variations:
        for var_name in item.variations:
            if any(term in var_name.lower() for term in ["tamanho", "size", "tam"]):
                val = getattr(item, var_name, None)
                if not val and hasattr(item, "__pydantic_extra__") and item.__pydantic_extra__:
                    val = item.__pydantic_extra__.get(var_name)
                if val:
                    if isinstance(val, list) and len(val) > 0:
                        s = str(val[0]).strip()
                        if s:
                            return s
                    elif isinstance(val, str) and val.strip():
                        return val.strip()

    # 2. Padrão comum VTEX: "Título do Produto - Tamanho"
    nome_tamanho = item.name
    if " - " in nome_tamanho:
        return nome_tamanho.split(" - ")[-1].strip()

    # 3. Extração via regex se habilitada
    if extract_via_regex:
        extracted = extract_size_fallback(nome_tamanho)
        if extracted:
            return extracted

    return nome_tamanho


def extract_sizes(items: List[VtexItem], extract_via_regex: bool = False) -> List[str]:
    """Extrai os tamanhos disponíveis dos SKUs que têm estoque (>0)."""
    tamanhos = []
    for item in items:
        tem_estoque = False
        for seller in item.sellers:
            # Apenas verifica se é > 0. A quantidade exata é irrelevante e evitada.
            if seller.commertialOffer.AvailableQuantity > 0:
                tem_estoque = True
                break

        if tem_estoque:
            nome_tamanho = _size_label(item, extract_via_regex)
            if nome_tamanho and nome_tamanho not in tamanhos:
                tamanhos.append(nome_tamanho)
    return tamanhos


def extract_size_grid(items: List[VtexItem], extract_via_regex: bool = False) -> List[str]:
    """Extrai a grade completa de tamanhos do produto (todas as SKUs
    registradas), independente de estoque atual. Distinto de ``extract_sizes``,
    que filtra por ``AvailableQuantity > 0``.
    """
    tamanhos = []
    for item in items:
        nome_tamanho = _size_label(item, extract_via_regex)
        if nome_tamanho and nome_tamanho not in tamanhos:
            tamanhos.append(nome_tamanho)
    return tamanhos
