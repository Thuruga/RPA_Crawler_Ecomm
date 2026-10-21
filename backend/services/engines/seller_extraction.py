"""
Módulo puro de extração de vendedor (seller) para os marketplaces.

Não realiza I/O de rede. Recebe HTML/soup já obtido e retorna o nome do
vendedor terceiro, ou None quando nenhum sinal de seller real é encontrado.

Exports:
  MARKETPLACE_DEFAULT_SELLER  - mapa marketplace -> nome-default
  ALL_DEFAULT_SELLERS         - set normalizado de todos os defaults conhecidos
  is_marketplace_default(seller, marketplace=None) -> bool
  parse_ml_seller_from_html(html) -> Optional[str]
  parse_amazon_seller_from_html(html) -> Optional[str]
"""

import re
import unicodedata
from typing import Optional

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

MARKETPLACE_DEFAULT_SELLER: dict[str, str] = {
    "Mercado Livre": "Mercado Livre",
    "Amazon": "Amazon",
    "Netshoes": "Netshoes",
    "Dafiti": "Dafiti",
    "Privalia": "Privalia",
    "PRIVALIA": "PRIVALIA",
}


def _normalize(text: str) -> str:
    """NFD + ascii-ignore + lower + strip — mesma normalização de _slugify em ML engine."""
    return (
        unicodedata.normalize("NFD", text)
        .encode("ascii", "ignore")
        .decode("utf-8")
        .lower()
        .strip()
    )


ALL_DEFAULT_SELLERS: set[str] = {_normalize(v) for v in MARKETPLACE_DEFAULT_SELLER.values()}


# ---------------------------------------------------------------------------
# Helper de precedência
# ---------------------------------------------------------------------------

def is_marketplace_default(seller: Optional[str], marketplace: Optional[str] = None) -> bool:
    """
    Retorna True quando `seller` é considerado um default de marketplace.

    Casos que retornam True:
      - None
      - string vazia ou só espaços
      - coincide (case/acento-insensitive, trim) com qualquer valor em
        ALL_DEFAULT_SELLERS (não apenas o do marketplace informado, para
        evitar que defaults "cruzados" sejam tratados como lojistas reais)

    Retorna False para qualquer lojista terceira real.
    """
    if seller is None:
        return True
    normalized = _normalize(seller)
    if not normalized:
        return True
    return normalized in ALL_DEFAULT_SELLERS


# ---------------------------------------------------------------------------
# Extrator Mercado Livre
# ---------------------------------------------------------------------------

# Prefixos a remover do nome de seller no texto do ML
_ML_PREFIX_RE = re.compile(
    r"(?i)^\s*(vendido\s+por|loja\s+oficial|por)\s*",
)


def _ml_strip_prefix(text: str) -> str:
    """Remove prefixos como 'Vendido por', 'por', 'Loja oficial' do texto."""
    return _ML_PREFIX_RE.sub("", text).strip()


def parse_ml_seller_from_html(html: str) -> Optional[str]:
    """
    Tenta extrair o nome da lojista terceira de uma PDP do Mercado Livre.

    Tenta, em ordem:
      1. .ui-pdp-seller__link-trigger span
      2. .ui-pdp-seller__link-trigger-button span
      3. .ui-pdp-seller__header__title  (remove prefixos "Vendido por"/"por"/"Loja oficial")
      4. .ui-pdp-action-modal__link span
      5. a[href*="/loja/"] ou a[href*="tienda"]  (link de loja oficial)
      6. JSON embutido: chaves "official_store_name", "seller":{"nickname"|"name"}, "store_name"

    Retorna None quando nenhum sinal de seller real é encontrado.
    NUNCA retorna uma string que is_marketplace_default() considere default.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # --- 1. .ui-pdp-seller__link-trigger span ---
    tag = soup.select_one(".ui-pdp-seller__link-trigger span")
    if tag:
        candidate = tag.text.strip()
        if candidate and not is_marketplace_default(candidate):
            return candidate

    # --- 2. .ui-pdp-seller__link-trigger-button span ---
    tag = soup.select_one(".ui-pdp-seller__link-trigger-button span")
    if tag:
        candidate = tag.text.strip()
        if candidate and not is_marketplace_default(candidate):
            return candidate

    # --- 3. .ui-pdp-seller__header__title ---
    tag = soup.select_one(".ui-pdp-seller__header__title")
    if tag:
        candidate = _ml_strip_prefix(tag.text)
        if candidate and not is_marketplace_default(candidate):
            return candidate

    # --- 4. .ui-pdp-action-modal__link span ---
    tag = soup.select_one(".ui-pdp-action-modal__link span")
    if tag:
        candidate = tag.text.strip()
        if candidate and not is_marketplace_default(candidate):
            return candidate

    # --- 5. link de loja: a[href*="/loja/"] ou a[href*="tienda"] ---
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/loja/" in href or "tienda" in href:
            candidate = a.text.strip()
            if candidate and not is_marketplace_default(candidate):
                return candidate

    # --- 6. JSON embutido no HTML (estado React / SEO) ---
    candidate = _ml_extract_from_json_state(html)
    if candidate and not is_marketplace_default(candidate):
        return candidate

    return None


def _ml_extract_from_json_state(html: str) -> Optional[str]:
    """
    Tenta extrair seller do estado JSON embutido no HTML do ML.

    Procura, via regex, as chaves:
      "official_store_name": "..."
      "seller": {..., "nickname": "..." | "name": "..."}
      "store_name": "..."
    """
    # official_store_name
    m = re.search(r'"official_store_name"\s*:\s*"([^"]+)"', html)
    if m:
        val = m.group(1).strip()
        if val:
            return val

    # seller nickname ou name dentro de objeto
    m = re.search(r'"seller"\s*:\s*\{[^}]*"(?:nickname|name)"\s*:\s*"([^"]+)"', html)
    if m:
        val = m.group(1).strip()
        if val:
            return val

    # store_name
    m = re.search(r'"store_name"\s*:\s*"([^"]+)"', html)
    if m:
        val = m.group(1).strip()
        if val:
            return val

    return None


# ---------------------------------------------------------------------------
# Extrator Amazon
# ---------------------------------------------------------------------------

_AMAZON_PREFIX_RE = re.compile(
    r"(?i)^\s*(vendido\s+por|ships?\s+from|enviado\s+por)\s*",
)


def _amazon_strip_prefix(text: str) -> str:
    return _AMAZON_PREFIX_RE.sub("", text).strip()


def parse_amazon_seller_from_html(html: str) -> Optional[str]:
    """
    Tenta extrair o nome da lojista terceira de uma PDP da Amazon.

    Tenta, em ordem:
      1. #sellerProfileTriggerId
      2. #merchant-info a  (link de seller)
      3. texto após "Vendido por" dentro de #merchant-info
      4. #tabular-buybox  (string "Vendido por" → irmão seguinte)
      5. #offer-display-features / [data-csa-c-content-id*="desktop-fakeQuickView"]
         / .offer-display-feature-text-message

    Retorna None quando nada encontrado.
    NUNCA retorna uma string que is_marketplace_default() considere default.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # --- 1. #sellerProfileTriggerId ---
    tag = soup.find("a", id="sellerProfileTriggerId")
    if tag:
        candidate = tag.text.strip()
        if candidate and not is_marketplace_default(candidate):
            return candidate

    # --- 2. #merchant-info a ---
    merchant_info = soup.find("div", id="merchant-info")
    if merchant_info:
        a_tag = merchant_info.find("a")
        if a_tag:
            candidate = a_tag.text.strip()
            if candidate and not is_marketplace_default(candidate):
                return candidate

        # --- 3. texto "Vendido por ..." dentro de #merchant-info ---
        raw_text = merchant_info.get_text(separator=" ")
        candidate = _amazon_strip_prefix(raw_text.strip())
        if candidate and not is_marketplace_default(candidate):
            return candidate

    # --- 4. #tabular-buybox: "Vendido por" -> irmão seguinte ---
    tabular = soup.find("div", id="tabular-buybox")
    if tabular:
        vendido_tag = tabular.find(string=re.compile(r"Vendido\s+por", re.I))
        if vendido_tag and vendido_tag.parent:
            sibling = vendido_tag.parent.find_next_sibling()
            if sibling:
                candidate = sibling.get_text().strip()
                if candidate and not is_marketplace_default(candidate):
                    return candidate

    # --- 5. blocos modernos de oferta ---
    for selector in [
        "#offer-display-features",
        "[data-csa-c-content-id*='desktop-fakeQuickView']",
        ".offer-display-feature-text-message",
    ]:
        tags = soup.select(selector)
        for t in tags:
            raw = t.get_text(separator=" ")
            candidate = _amazon_strip_prefix(raw.strip())
            if candidate and not is_marketplace_default(candidate):
                return candidate

    return None
