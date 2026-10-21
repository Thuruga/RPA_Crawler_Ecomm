from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Any, Optional
from core.models import coerce_money_decimal, money_decimal_to_json_number


def calculate_price_averages(products: List[Any]) -> Dict[str, Any]:
    """Calcula médias global e por marca para uma lista de produtos válidos.

    Regras (D-01, D-02, D-03):
    - Produtos esgotados (is_available=False) ou com preço <= 0 são ignorados.
    - O frete NÃO é somado (apenas preço puro do produto).
    - Retorna average_price (efetivo/promocional) e average_price_original (de tabela).
    - Retorna brand_averages com média e contagem por marca.
    """
    valid_products: List[tuple[Any, Decimal, Decimal]] = []
    brand_groups: Dict[str, List[tuple[Any, Decimal, Decimal]]] = {}

    for p in products:
        if isinstance(p, dict):
            if "price" in p:
                eff_raw = p["price"] if p["price"] is not None else p.get("price_discount")
            elif "price_discount" in p:
                eff_raw = p["price_discount"] if p["price_discount"] is not None else p.get("price_full")
            else:
                eff_raw = p.get("price_full")

            orig_raw = p.get("price_full") if p.get("price_full") is not None else (p.get("price_original") if p.get("price_original") is not None else p.get("price"))
            if p.get("stock_availability") is not None:
                available = p.get("stock_availability")
            elif p.get("is_available") is not None:
                available = p.get("is_available")
            else:
                available = p.get("available", True)
            brand = p.get("brand") or "unknown"
        else:
            p_price = getattr(p, "price", None)
            p_discount = getattr(p, "price_discount", None)
            p_full = getattr(p, "price_full", None)
            p_orig = getattr(p, "price_original", None)

            if p_price is not None:
                eff_raw = p_price
            elif p_discount is not None:
                eff_raw = p_discount
            else:
                eff_raw = p_full

            orig_raw = p_full if p_full is not None else (p_orig if p_orig is not None else p_price)

            p_stock = getattr(p, "stock_availability", None)
            if p_stock is not None:
                available = p_stock
            else:
                p_avail = getattr(p, "is_available", None)
                if p_avail is None:
                    p_avail = getattr(p, "available", True)
                available = p_avail if p_avail is not None else True
            brand = getattr(p, "brand", "") or "unknown"

        try:
            price_dec = coerce_money_decimal(eff_raw)
        except (ValueError, Exception):
            price_dec = None

        if price_dec is not None and price_dec > Decimal("0") and available:
            try:
                orig_dec = coerce_money_decimal(orig_raw)
            except (ValueError, Exception):
                orig_dec = None

            if orig_dec is not None and orig_dec >= price_dec:
                effective_orig = orig_dec
            else:
                effective_orig = price_dec

            valid_products.append((p, price_dec, effective_orig))

            brand_key = brand.strip().lower()
            brand_groups.setdefault(brand_key, []).append((p, price_dec, effective_orig))

    if not valid_products:
        return {
            "average_price": None,
            "average_price_original": None,
            "total_items": 0,
            "count": 0,
            "brand_averages": {},
        }

    total_effective = sum(p_dec for _, p_dec, _ in valid_products)
    avg_effective = (total_effective / Decimal(len(valid_products))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    total_original = sum(orig_dec for _, _, orig_dec in valid_products)
    avg_original = (total_original / Decimal(len(valid_products))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    brand_averages: Dict[str, Any] = {}
    for bk, b_items in brand_groups.items():
        b_eff_sum = sum(p_dec for _, p_dec, _ in b_items)
        b_eff_avg = (b_eff_sum / Decimal(len(b_items))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        b_orig_sum = sum(orig_dec for _, _, orig_dec in b_items)
        b_orig_avg = (b_orig_sum / Decimal(len(b_items))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        brand_averages[bk] = {
            "average_price": money_decimal_to_json_number(b_eff_avg),
            "average_price_original": money_decimal_to_json_number(b_orig_avg),
            "count": len(b_items),
        }

    return {
        "average_price": money_decimal_to_json_number(avg_effective),
        "average_price_original": money_decimal_to_json_number(avg_original),
        "total_items": len(valid_products),
        "count": len(valid_products),
        "brand_averages": brand_averages,
    }
