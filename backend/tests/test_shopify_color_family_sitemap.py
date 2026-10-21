"""
Debug ricardo-almeida-cores-vtex (2026-09-28) — regressão determinística para
`ShopifyApiClient._get_color_family()`.

Cobre:
  - `_slugify` / `_derive_sibling_handle_prefix`: derivação pura do prefixo de
    handle compartilhado pela família de cores (cor de uma ou várias
    palavras; retorna None quando a cor não aparece como segmento explícito
    no handle).
  - `_get_color_family`: descoberta via sitemap_products_*.xml como fonte
    primária (determinística) — encontra produtos-irmãos e mescla suas
    cores, mesmo quando a busca textual legada não os encontraria.
  - Fallback: quando o prefixo não pode ser derivado, ou o sitemap está
    inacessível/sem produtos com esse prefixo, cai para a busca textual HTML
    legada (`_get_color_family_via_search`) — comportamento aditivo, não uma
    substituição cega.

100% hermético: nenhuma chamada de rede real. `SessionManager.get_session` é
monkeypatchado para retornar uma sessão falsa que resolve URLs conhecidas
(sitemap.xml, sitemap_products_1.xml, .json de produtos-irmãos) a partir de
uma tabela de roteamento em memória.
"""
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest

from core.models import RawProductBronze
from services.shopify_api_client import ShopifyApiClient


# ---------------------------------------------------------------------------
# _slugify / _derive_sibling_handle_prefix — funções puras
# ---------------------------------------------------------------------------
def test_slugify_strips_accents_and_normalizes_separators():
    assert ShopifyApiClient._slugify("Cinza") == "cinza"
    assert ShopifyApiClient._slugify("Azul Marinho Escuro") == "azul-marinho-escuro"
    assert ShopifyApiClient._slugify("Terracota/Rosado") == "terracota-rosado"


def test_derive_sibling_handle_prefix_single_word_color():
    handle = "camisa-polo-interloque-44-1-manga-curta-cinza-64859-76775"
    prefix = ShopifyApiClient._derive_sibling_handle_prefix(handle, "Cinza")
    assert prefix == "camisa-polo-interloque-44-1-manga-curta-"


def test_derive_sibling_handle_prefix_multi_word_color():
    handle = "camisa-x-azul-marinho-escuro-123-456"
    prefix = ShopifyApiClient._derive_sibling_handle_prefix(handle, "Azul Marinho Escuro")
    assert prefix == "camisa-x-"


def test_derive_sibling_handle_prefix_returns_none_when_color_absent_from_handle():
    """Cor não aparece como segmento explícito no handle -> None (sinal para
    o chamador acionar o fallback de busca textual legado)."""
    handle = "camisa-x-123-456"
    assert ShopifyApiClient._derive_sibling_handle_prefix(handle, "Roxo") is None


# ---------------------------------------------------------------------------
# Fakes herméticos para SessionManager.get_session()
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "", json_data: Any = None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data


class _FakeSession:
    """Roteia GETs por URL (comparação exata) para respostas pré-definidas."""

    def __init__(self, routes: Dict[str, _FakeResponse]):
        self._routes = routes
        self.requested_urls: list[str] = []

    async def get(self, url: str, timeout=None, headers=None) -> _FakeResponse:
        self.requested_urls.append(url)
        response = self._routes.get(url)
        if response is None:
            return _FakeResponse(status_code=404)
        return response


_SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.example.com/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://www.example.com/sitemap_pages_1.xml</loc></sitemap>
</sitemapindex>"""


def _products_sitemap_xml(handles: list[str]) -> str:
    urls = "\n".join(
        f"  <url><loc>https://www.example.com/products/{h}</loc></url>" for h in handles
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>"
    )


def _sibling_json_response(color: str, image_url: str | None = None) -> _FakeResponse:
    product: Dict[str, Any] = {
        "options": [{"name": "Cor", "position": 1}],
        "variants": [{"option1": color, "available": True}],
    }
    if image_url is not None:
        product["images"] = [{"src": image_url}]
    return _FakeResponse(status_code=200, json_data={"product": product})


def _mapped_product(
    url: str, raw_title: str, color: str, image_url: str | None = None
) -> RawProductBronze:
    # NOTA: `image_url` só entra no payload quando explicitamente fornecido —
    # RawProductBronze valida `image_url` como obrigatório/não-vazio QUANDO
    # a chave está presente no input (mesmo que o valor seja None); omiti-la
    # preserva o comportamento anterior (produto sem imagem conhecida).
    kwargs: Dict[str, Any] = dict(
        url=url,
        brand="unregistered_test_brand_shopify",
        raw_title=raw_title,
        available_colors=[color],
    )
    if image_url is not None:
        kwargs["image_url"] = image_url
    return RawProductBronze(**kwargs)


def _make_client(brand_key: str = "unregistered_test_brand_shopify") -> ShopifyApiClient:
    """Constrói um ShopifyApiClient com domínio controlado (www.example.com),
    contornando o brand_service (marca desconhecida -> brand_info=None,
    domain="") para que as URLs de sitemap/produto batam exatamente com as
    rotas fake definidas nos testes."""
    client = ShopifyApiClient(brand_key)
    assert client.brand_info is None  # confirma que é uma marca não registrada
    client.domain = "www.example.com"
    client.base_url = "https://www.example.com"
    return client


def _patch_session(routes: Dict[str, _FakeResponse]):
    fake_session = _FakeSession(routes)

    async def _fake_get_session():
        return fake_session

    return fake_session, patch(
        "services.shopify_api_client.SessionManager.get_session",
        side_effect=_fake_get_session,
    )


# ---------------------------------------------------------------------------
# _get_color_family — caminho determinístico (sitemap)
# ---------------------------------------------------------------------------
def _allow_sitemap_for_unregistered_test_brand():
    """A allowlist de descoberta via sitemap (`_SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST`)
    só contém marcas reais validadas (hoje: apenas 'ricardoalmeida' — ver
    Evidence/guard anti-Buckman). Os testes hermético/sitemap usam uma
    marca fake ('unregistered_test_brand_shopify'); este helper habilita
    temporariamente essa marca fake na allowlist, para exercitar o caminho
    determinístico sem depender de dados reais de brand_service."""
    return patch(
        "services.shopify_api_client._SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST",
        frozenset({"unregistered_test_brand_shopify"}),
    )


@pytest.mark.asyncio
async def test_get_color_family_uses_sitemap_and_merges_sibling_colors():
    """Produto-irmão que a busca textual legada perderia (ex.: fora da janela
    de páginas percorrida) é encontrado via sitemap e sua cor é mesclada.
    Também cobre D-Opção A (color_swatches): image_url/sibling_code são
    preenchidos reaproveitando SOMENTE dados já buscados nesta descoberta
    (nenhuma chamada de rede nova)."""
    base = "camisa-polo-interloque-44-1-manga-curta"
    self_handle = f"{base}-cinza-64859-76775"
    sibling_handles = [
        f"{base}-azul-54765-76775",
        f"{base}-verde-55294-76775",
        f"{base}-roxo-55036-76775",
    ]

    routes = {
        "https://www.example.com/sitemap.xml": _FakeResponse(
            status_code=200, text=_SITEMAP_INDEX_XML
        ),
        "https://www.example.com/sitemap_products_1.xml": _FakeResponse(
            status_code=200,
            text=_products_sitemap_xml([self_handle] + sibling_handles),
        ),
        f"https://www.example.com/products/{sibling_handles[0]}.json": _sibling_json_response(
            "Azul", image_url="https://cdn.example.com/azul-54765.png"
        ),
        f"https://www.example.com/products/{sibling_handles[1]}.json": _sibling_json_response(
            "Verde", image_url="https://cdn.example.com/verde-55294.png"
        ),
        f"https://www.example.com/products/{sibling_handles[2]}.json": _sibling_json_response("Roxo"),
    }

    client = _make_client()
    mapped = _mapped_product(
        f"https://www.example.com/products/{self_handle}",
        "Camisa Polo Interloque 44.1 Manga Curta Cinza",
        "Cinza",
        image_url="https://cdn.example.com/cinza-64859.png",
    )

    fake_session, patcher = _patch_session(routes)
    with patcher, _allow_sitemap_for_unregistered_test_brand(), patch.object(
        ShopifyApiClient, "_get_color_family_via_search"
    ) as fallback_mock:
        colors = await client._get_color_family(mapped)

    assert set(colors) == {"Cinza", "Azul", "Verde", "Roxo"}
    # Fonte determinística resolveu sozinha — fallback textual nunca deveria
    # ser acionado quando o sitemap encontra produtos-irmãos.
    fallback_mock.assert_not_called()

    swatches_by_name = {s.name: s for s in mapped.color_swatches}
    assert set(swatches_by_name) == {"Cinza", "Azul", "Verde", "Roxo"}
    # Próprio produto: imagem já carregada por _map_to_bronze (mapped.image_url,
    # sem chamada de rede nova) + código extraído puramente do seu handle.
    assert swatches_by_name["Cinza"].image_url == "https://cdn.example.com/cinza-64859.png"
    assert swatches_by_name["Cinza"].sibling_code == "64859"
    # Produtos-irmãos: imagem vem do MESMO .json já buscado para a cor.
    assert swatches_by_name["Azul"].image_url == "https://cdn.example.com/azul-54765.png"
    assert swatches_by_name["Azul"].sibling_code == "54765"
    assert swatches_by_name["Verde"].image_url == "https://cdn.example.com/verde-55294.png"
    assert swatches_by_name["Verde"].sibling_code == "55294"
    # Roxo não expôs imagem na resposta fake — deve ficar None, nunca inventado.
    assert swatches_by_name["Roxo"].image_url is None
    assert swatches_by_name["Roxo"].sibling_code == "55036"


@pytest.mark.asyncio
async def test_get_color_family_falls_back_to_search_when_prefix_not_derivable():
    """Cor não aparece como segmento no handle -> sitemap não pode filtrar
    -> cai para a busca textual legada (comportamento aditivo, não regressivo)."""
    mapped = _mapped_product(
        "https://www.example.com/products/produto-generico-123-456",
        "Produto Genérico",
        "Vermelho",  # não aparece no handle
    )

    client = _make_client()

    with patch.object(
        ShopifyApiClient, "_get_color_family_via_search", return_value=["Vermelho", "Fallback"]
    ) as fallback_mock:
        colors = await client._get_color_family(mapped)

    fallback_mock.assert_called_once()
    assert colors == ["Vermelho", "Fallback"]


@pytest.mark.asyncio
async def test_get_color_family_falls_back_to_search_when_sitemap_unreachable():
    """Prefixo é derivável, mas o sitemap.xml não responde (404/erro) ->
    fallback textual é acionado."""
    base = "camisa-polo"
    self_handle = f"{base}-cinza-123-456"
    mapped = _mapped_product(
        f"https://www.example.com/products/{self_handle}",
        "Camisa Polo Cinza",
        "Cinza",
    )

    client = _make_client()

    routes: Dict[str, _FakeResponse] = {}  # sitemap.xml -> 404 (não roteado)
    fake_session, patcher = _patch_session(routes)

    with patcher, _allow_sitemap_for_unregistered_test_brand(), patch.object(
        ShopifyApiClient, "_get_color_family_via_search", return_value=["Cinza"]
    ) as fallback_mock:
        colors = await client._get_color_family(mapped)

    fallback_mock.assert_called_once()
    assert colors == ["Cinza"]


@pytest.mark.asyncio
async def test_get_color_family_skips_sitemap_when_multiple_colors_already_known():
    """Guard preexistente: produto com >1 cor já conhecida (VTEX-style) não
    aciona nenhuma busca de família — comportamento inalterado pelo fix."""
    mapped = RawProductBronze(
        url="https://www.example.com/products/produto-x",
        brand="unregistered_test_brand_shopify",
        raw_title="Produto X",
        available_colors=["Azul", "Preto"],
    )
    client = _make_client()

    with patch.object(ShopifyApiClient, "_get_color_family_via_search") as fallback_mock, \
         patch.object(ShopifyApiClient, "_get_color_family_via_sitemap") as sitemap_mock:
        colors = await client._get_color_family(mapped)

    fallback_mock.assert_not_called()
    sitemap_mock.assert_not_called()
    assert colors == ["Azul", "Preto"]


# ---------------------------------------------------------------------------
# color_swatches — fallback textual legado (name-only, sem imagem/código)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_color_family_via_search_builds_name_only_swatches():
    """[Fallback legado] color_swatches reflete EXATAMENTE os mesmos nomes de
    cor já retornados por esta busca textual — nem mais, nem menos. Sem
    image_url/sibling_code (esta fonte não expõe imagem nem handle do
    produto-irmão sem uma chamada de rede nova, que NÃO deve ser
    introduzida). Cobre o caminho usado hoje por marcas fora da allowlist
    de sitemap (ex.: Buckman) — os swatches delas não ganham nem perdem
    nenhum sibling em relação ao comportamento anterior a esta mudança."""
    mapped = _mapped_product(
        "https://www.example.com/products/camisa-x-cinza-123-456",
        "Camisa X Cinza",
        "Cinza",
    )
    client = _make_client()

    routes = {
        "https://www.example.com/search?page=1&q=Camisa%20X": _FakeResponse(
            status_code=200,
            text="<div>Camisa X Cinza</div><div>Camisa X Azul</div>",
        ),
        "https://www.example.com/search?page=2&q=Camisa%20X": _FakeResponse(
            status_code=200,
            text="<div>Nenhum resultado relacionado</div>",
        ),
    }
    fake_session, patcher = _patch_session(routes)

    with patcher:
        colors = await client._get_color_family_via_search(mapped)

    assert set(colors) == {"Cinza", "Azul"}
    swatches_by_name = {s.name: s for s in mapped.color_swatches}
    assert set(swatches_by_name) == {"Cinza", "Azul"}
    for swatch in swatches_by_name.values():
        assert swatch.image_url is None
        assert swatch.sibling_code is None


# ---------------------------------------------------------------------------
# Guard anti-Buckman — allowlist de descoberta via sitemap (2026-09-30)
# ---------------------------------------------------------------------------
def test_sitemap_sibling_discovery_allowlist_includes_ricardoalmeida_excludes_buckman():
    """Documentação viva / regressão: se este teste falhar, alguém alterou a
    allowlist sem atualizar esta prova explícita — 'ricardoalmeida' deve
    permanecer incluída (validada, fix confirmado E2E) e 'bck' (Buckman)
    deve permanecer EXCLUÍDA até que exista um parser dedicado do código
    'Referencia: NN.NN.NNNN' (ver Evidence 2026-09-30 em
    .planning/debug/resolved/ricardo-almeida-cores-vtex.md — 11.8% de
    contaminação medida contra ground-truth)."""
    from services.shopify_api_client import _SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST

    assert "ricardoalmeida" in _SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST
    assert "bck" not in _SHOPIFY_SITEMAP_SIBLING_DISCOVERY_ALLOWLIST


@pytest.mark.asyncio
async def test_buckman_guard_prevents_sitemap_sibling_discovery():
    """Regressão de segurança de dados: para a marca 'bck' (Buckman), mesmo
    diante de um sitemap contendo um handle que a heurística de prefixo
    trataria como 'produto-irmão' (mas que na realidade pertence a uma
    referência/coleção REALMENTE distinta — cenário de contaminação medido
    em 10/85 = 11.8% dos casos reais, ver Evidence 2026-09-30),
    `_get_color_family` NUNCA deve chamar `_get_color_family_via_sitemap`
    para esta marca — o guard (allowlist) intercepta antes disso e usa
    exclusivamente o fallback textual legado, preservando o comportamento
    de hoje (sem o novo risco de contaminação)."""
    # 'camisa-m-l-linho-puro-' é um descritor genérico de tecido/estilo
    # (não um identificador de família única) — reutilizado por referências
    # diferentes na Buckman, ver Evidence. O handle abaixo simula um
    # "produto-irmão" que a heurística de prefixo casaria incorretamente.
    self_handle = "camisa-m-l-linho-puro-marinho-8850-312"
    contaminating_handle = "camisa-m-l-linho-puro-verde-312"

    routes = {
        "https://www.example.com/sitemap.xml": _FakeResponse(
            status_code=200, text=_SITEMAP_INDEX_XML
        ),
        "https://www.example.com/sitemap_products_1.xml": _FakeResponse(
            status_code=200,
            text=_products_sitemap_xml([self_handle, contaminating_handle]),
        ),
        f"https://www.example.com/products/{contaminating_handle}.json": _sibling_json_response("Verde"),
    }

    client = ShopifyApiClient("bck")
    client.domain = "www.example.com"
    client.base_url = "https://www.example.com"

    mapped = RawProductBronze(
        url=f"https://www.example.com/products/{self_handle}",
        brand="bck",
        raw_title="Camisa M/L Linho Puro Marinho",
        available_colors=["Marinho"],
    )

    fake_session, patcher = _patch_session(routes)

    with patcher, \
         patch.object(ShopifyApiClient, "_get_color_family_via_sitemap") as sitemap_mock, \
         patch.object(
             ShopifyApiClient, "_get_color_family_via_search", return_value=["Marinho"]
         ) as fallback_mock:
        colors = await client._get_color_family(mapped)

    # A prova central do guard: o caminho novo (sitemap) nunca é sequer
    # tentado para esta marca — não é uma questão de ele "falhar" ou
    # "retornar vazio", ele simplesmente não é chamado.
    sitemap_mock.assert_not_called()
    fallback_mock.assert_called_once()
    assert colors == ["Marinho"]
