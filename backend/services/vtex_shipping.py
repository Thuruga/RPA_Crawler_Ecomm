"""
Helpers puros para parsing/filtragem/ordenacao de SLAs do checkout VTEX.

Modulo sem estado, sem rede, sem sessao HTTP — apenas transformacoes
deterministicas sobre os dados de logisticsInfo[].slas[] do endpoint:
  POST /api/checkout/pub/orderForms/simulation

Convencao espelhada de vtex_parsing.py: funcoes de modulo, sem `self`, sem I/O.
Escopo: somente sites de marca VTEX (D-03). SFCC, Wake, Shopify e marketplaces
nao passam por este caminho.

Requisito: FRET-05 (contrato centavos→reais, filtro pickup, unidades de prazo).
Mitigacao: T-33-04 (payload nao confiavel — validate antes de construir modelos).
"""
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes internas
# ---------------------------------------------------------------------------

# Mapa oficial VTEX de unidades de prazo → (multipicador_segundos, texto_pt)
_UNIT_CONFIG: Dict[str, Tuple[int, str]] = {
    "bd": (86400,  "dia útil"),     # business day (dia util) — ~1 dia calendario
    "d":  (86400,  "dia"),          # dia calendario
    "h":  (3600,   "hora"),
    "m":  (60,     "minuto"),
}

# Regex para extrair numero + unidade de um shippingEstimate ("5bd", "12h", etc.)
_ESTIMATE_RE = re.compile(r'^(\d+)(bd|d|h|m)$')


# ---------------------------------------------------------------------------
# parse_estimate
# ---------------------------------------------------------------------------

def parse_estimate(shipping_estimate: str) -> Optional[Tuple[int, str, int, str]]:
    """Parseia um shippingEstimate VTEX em (value, unit, sort_seconds, display_text).

    Retorna None para entradas nao-reconhecidas (malformadas, vazias).

    Unidades suportadas: bd (dias uteis), d (dias), h (horas), m (minutos).
    Textos PT exatos:
      - bd → "Até X dias úteis"
      - d  → "Até X dias"
      - h  → "Até X horas"
      - m  → "Até X minutos"

    sort_seconds permite comparar duracoes para desempate de ordenacao.
    bd usa o mesmo multiplicador de 'd' para fins de ordenacao (1 dia util ≈ 1 dia).
    """
    if not shipping_estimate:
        return None

    match = _ESTIMATE_RE.match(shipping_estimate.strip())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    config = _UNIT_CONFIG.get(unit)
    if config is None:
        return None  # unidade desconhecida — nao deve ocorrer com o regex acima

    multiplier, base_label = config

    # Pluralizacao para o texto PT
    if unit in ("bd", "d"):
        # "1 dia util" / "1 dia" vs "X dias uteis" / "X dias"
        if unit == "bd":
            label = "dia útil" if value == 1 else "dias úteis"
        else:
            label = "dia" if value == 1 else "dias"
    elif unit == "h":
        label = "hora" if value == 1 else "horas"
    else:  # "m"
        label = "minuto" if value == 1 else "minutos"

    display_text = f"Até {value} {label}"
    sort_seconds = value * multiplier

    return (value, unit, sort_seconds, display_text)


# ---------------------------------------------------------------------------
# filter_and_sort_slas
# ---------------------------------------------------------------------------

def filter_and_sort_slas(slas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filtra e ordena a lista de SLAs do logisticsInfo VTEX.

    Regras aplicadas (em ordem):
    1. Manter apenas deliveryChannel == "delivery".
    2. Excluir defensivamente: pickupStoreInfo.isPickupStore is True,
       pickupPointId nao-vazio.
    3. Descartar entradas malformadas: price ausente, price negativo,
       shippingEstimate nao-parseavel.
    4. Converter centavos → reais via /100 (nunca via truthiness — 0.0 != None).
    5. Marcar is_free_shipping=True quando price_reais == 0.0.
    6. Ordenar por price_reais asc, depois sort_seconds asc (D-10).

    Retorna lista de dicts normalizados com as chaves adicionais:
      - price_reais: float (0.0 == gratis, nunca None para entradas validas)
      - is_free_shipping: bool
      - estimate_value: int
      - estimate_unit: str
      - estimate_sort_seconds: int
      - estimate_display: str

    Entradas malformadas sao descartadas individualmente (D-16).
    """
    valid_options = []

    for sla in slas:
        # --- 1/2. Filtro de canal e pickup defensivo ---
        channel = sla.get("deliveryChannel", "")
        if channel != "delivery":
            continue

        pickup_info = sla.get("pickupStoreInfo", {}) or {}
        if pickup_info.get("isPickupStore") is True:
            continue

        pickup_point_id = sla.get("pickupPointId", "") or ""
        if pickup_point_id:
            continue

        # --- 3. Validacao de preco (T-33-04) ---
        raw_price = sla.get("price")
        if raw_price is None:
            logger.debug("SLA descartada: campo 'price' ausente (%s)", sla.get("name"))
            continue

        try:
            raw_price = int(raw_price)
        except (TypeError, ValueError):
            logger.debug("SLA descartada: 'price' nao e inteiro (%s)", sla.get("name"))
            continue

        if raw_price < 0:
            logger.debug("SLA descartada: 'price' negativo (%s)", sla.get("name"))
            continue

        # --- 4. Parse do prazo (T-33-04) ---
        estimate_str = sla.get("shippingEstimate", "") or ""
        parsed = parse_estimate(estimate_str)
        if parsed is None:
            logger.debug(
                "SLA descartada: shippingEstimate '%s' nao parseavel (%s)",
                estimate_str, sla.get("name"),
            )
            continue

        est_value, est_unit, est_sort_seconds, est_display = parsed

        # --- 5. Conversao centavos → reais (D-02) ---
        price_reais = raw_price / 100
        is_free = price_reais == 0.0  # explicito: 0.0 e gratis; None e nao-calculado

        valid_options.append({
            **sla,
            "price_reais": price_reais,
            "is_free_shipping": is_free,
            "estimate_value": est_value,
            "estimate_unit": est_unit,
            "estimate_sort_seconds": est_sort_seconds,
            "estimate_display": est_display,
        })

    # --- 6. Ordenacao: price_reais asc, sort_seconds asc (D-10) ---
    valid_options.sort(key=lambda o: (o["price_reais"], o["estimate_sort_seconds"]))
    return valid_options


# ---------------------------------------------------------------------------
# select_candidate
# ---------------------------------------------------------------------------

def select_candidate(
    items: List[Dict[str, Any]],
) -> Optional[Tuple[str, str]]:
    """Seleciona (sku_id, seller_id) do primeiro item com oferta disponivel.

    Percorre os items VTEX e retorna o par (itemId, sellerId) do primeiro seller
    com AvailableQuantity > 0. Retorna None se nenhum item/seller tiver estoque.

    Substitui o hardcode seller="1" do codigo legado (ver vtex_api_scraper.py:435).
    """
    for item in items:
        item_id = item.get("itemId")
        if not item_id:
            continue
        for seller in item.get("sellers", []):
            offer = seller.get("commertialOffer", {}) or {}
            qty = offer.get("AvailableQuantity", 0) or 0
            if qty > 0:
                seller_id = seller.get("sellerId", "1")
                return (str(item_id), str(seller_id))
    return None


# ---------------------------------------------------------------------------
# classify_result
# ---------------------------------------------------------------------------

def classify_result(
    options: List[Dict[str, Any]],
    transport_error: bool = False,
) -> str:
    """Classifica o resultado da simulacao de frete VTEX.

    Estados (D-14):
    - "available"          — >=1 opcao valida de entrega domiciliar.
    - "unavailable_for_cep" — resposta 200 valida, mas zero opcoes de entrega
                              para o CEP informado (resultado de negocio, nao falha).
    - "temporary_failure"  — erro de transporte/timeout; o produto permanece na
                              busca com indicacao de falha temporaria (D-13).

    Nota: nao retentar um 200 com zero opcoes (pitfall 5 da RESEARCH).
    """
    if options:
        return "available"
    if transport_error:
        return "temporary_failure"
    return "unavailable_for_cep"
