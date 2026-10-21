from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import unicodedata
from typing import Any

import pandas as pd


CANONICAL_PRODUCT_COLUMNS = [
    "brand",
    "url",
    "price_full",
    "price_discount",
    "product_name",
    "product_description",
    "composition",
    "gender",
    "age_group",
    "product_group",
    "available_colors",
    "raw_colors",
    "available_sizes",
    "raw_sizes",
    "size_grid",
    "raw_size_grid",
    "product_code",
    "category",
    "rating",
    "review_count",
]

# Contrato de apresentação comum a todos os downloads do produto.  O contrato
# canônico acima continua sendo interno/técnico; este é o cabeçalho em PT-BR
# consumido pelas pessoas no Excel.
STANDARD_EXPORT_COLUMNS = [
    "Marca", "Vendedor", "URL do Produto", "Código do Produto",
    "Nome do Produto", "Cor", "Descrição do Produto", "Composição",
    "Cores Disponíveis", "Tamanhos Disponíveis", "Grade de Tamanhos", "Categoria", "Avaliação",
    "Preço Atual", "Preço com Desconto", "Valor do Desconto",
    "Frete", "Tempo de Frete", "Frete Grátis",
    "Valor do Frete", "Preço Total",
]



CANONICAL_SPEC_ALIASES = {
    "composition": (
        "composition",
        "Composição",
        "Composicao",
        "Composição do produto",
        "Material",
        "Material principal",
        "Material Principal",
        "COMPOSIÇÃO",
    ),
    "product_code": (
        "product_code",
        "Código",
        "Codigo",
        "Código do produto",
        "Codigo do produto",
        "Referência",
        "Referencia",
        "Ref.",
        "REF",
    ),
    "category": (
        "category",
        "Categoria",
        "Department",
    ),
    "gender": (
        "gender",
        "Gênero",
        "Genero",
        "Para",
    ),
    "age_group": (
        "age_group",
        "Idade",
        "Faixa Etária",
        "Faixa Etaria",
        "age",
    ),
    "product_group": (
        "product_group",
        "Grupo de Produtos Similares",
        "NetshoesProductGroup",
    ),
}

LIST_SPEC_ALIASES = {
    "available_colors": (
        "Cor",
        "Cores",
        "Color",
        "Colors",
    ),
    "available_sizes": (
        "Tamanho",
        "Tamanhos",
        "Size",
        "Sizes",
    ),
}


def _as_dict(product_like: Any) -> dict[str, Any]:
    if product_like is None:
        return {}
    if isinstance(product_like, Mapping):
        return dict(product_like)
    if hasattr(product_like, "model_dump"):
        return product_like.model_dump(mode="json")
    if hasattr(product_like, "dict"):
        return product_like.dict()
    return dict(vars(product_like))


def _clean_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower() == "none":
            return None
        return cleaned
    return value


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower() == "none":
            return []
        for separator in ("|", ";", "/"):
            cleaned = cleaned.replace(separator, ",")
        return [part.strip() for part in cleaned.split(",") if part.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items: list[str] = []
        for item in value:
            cleaned = _clean_scalar(item)
            if cleaned is not None:
                items.append(str(cleaned))
        return items
    cleaned = _clean_scalar(value)
    return [str(cleaned)] if cleaned is not None else []


def _coerce_float(value: Any) -> float | None:
    cleaned = _clean_scalar(value)
    if cleaned is None:
        return None
    try:
        return round(float(str(cleaned).replace(",", ".")), 2)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    cleaned = _clean_scalar(value)
    if cleaned is None:
        return None
    digits = "".join(ch for ch in str(cleaned) if ch.isdigit())
    return int(digits) if digits else None


def _clean_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "1", "sim", "yes"}:
            return True
        if cleaned in {"false", "0", "nao", "não", "no"}:
            return False
    return bool(value)


def _safe_export_text(value: Any) -> Any:
    """Avoid Excel formula interpretation for untrusted textual product data."""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _clean_raw_list(value: Any) -> list[str]:
    """Limpa uma lista BRUTA preservando ordem e capitalização originais (D-01).

    Diferente de `_clean_list`, remove duplicatas EXATAS (sem casefold) para não
    inventar normalização — dois valores com capitalização distinta (ex.: 'Azul' e
    'azul') NÃO são considerados duplicatas aqui; isso é responsabilidade explícita
    de `normalize_color_values`, nunca desta função de limpeza bruta.
    """
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.lower() == "none":
            return []
        for separator in ("|", ";", "/"):
            cleaned = cleaned.replace(separator, ",")
        items = [part.strip() for part in cleaned.split(",") if part.strip()]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = []
        for item in value:
            scalar = _clean_scalar(item)
            if scalar is not None:
                items.append(str(scalar))
    else:
        scalar = _clean_scalar(value)
        items = [str(scalar)] if scalar is not None else []

    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def normalize_color_values(
    raw: Sequence[Any] | None,
    aliases: Mapping[str, str] | None,
) -> list[str]:
    """Normaliza uma lista de cores usando SOMENTE um mapa exato explícito (D-01/D-02).

    Nunca aplica NLP, fuzzy matching ou heurística de similaridade — apenas lookup
    exato (case-insensitive/trim) em `aliases`. Valores sem entrada explícita no mapa
    permanecem inalterados (fail-safe: nunca inventa uma normalização). O resultado
    preserva a ordem de primeira ocorrência e remove duplicatas exatas pós-normalização.
    """
    if not raw:
        return []
    alias_map = {str(k).strip().lower(): v for k, v in (aliases or {}).items()}

    seen: set[str] = set()
    normalized: list[str] = []
    for color in raw:
        scalar = _clean_scalar(color)
        if scalar is None:
            continue
        text = str(scalar)
        mapped = alias_map.get(text.strip().lower(), text)
        if mapped not in seen:
            seen.add(mapped)
            normalized.append(mapped)
    return normalized





def _normalize_export_prices(payload: Mapping[str, Any]) -> tuple[float | None, float | None]:
    price_full = _coerce_float(payload.get("price_full"))
    price_discount = _coerce_float(payload.get("price_discount"))

    if payload.get("price_discount_is_delta") and price_full is not None and price_discount is not None:
        return round(price_full + price_discount, 2), round(price_full, 2)

    return price_full, price_discount


def _fold_key(value: str) -> str:
    """Normaliza uma chave de spec para lookup insensível a acento/maiúscula.

    Decompõe via NFKD, remove os caracteres combinantes (acentos) e aplica
    casefold + trim — assim "COMPOSIÇÃO", "Composição" e "composicao" colapsam
    para a mesma chave "folded".
    """
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return without_accents.strip().casefold()


def normalize_specifications_aliases(specifications: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(specifications or {})

    # Mapa auxiliar {chave_folded: chave_original}; em colisão de folding,
    # preserva a primeira ocorrência (ordem de inserção do dict original).
    folded_lookup: dict[str, str] = {}
    for original_key in normalized:
        folded = _fold_key(str(original_key))
        if folded not in folded_lookup:
            folded_lookup[folded] = original_key

    for canonical_key, aliases in CANONICAL_SPEC_ALIASES.items():
        if _clean_scalar(normalized.get(canonical_key)) is not None:
            continue
        for alias in aliases:
            original_key = folded_lookup.get(_fold_key(alias))
            if original_key is None:
                continue
            candidate = _clean_scalar(normalized.get(original_key))
            if candidate is not None:
                normalized[canonical_key] = candidate
                break
    return normalized


def extract_canonical_spec_value(
    specifications: Mapping[str, Any] | None,
    canonical_key: str,
) -> Any:
    return normalize_specifications_aliases(specifications).get(canonical_key)


def _extract_list_from_specs(
    specifications: Mapping[str, Any] | None,
    canonical_key: str,
) -> list[str]:
    normalized = normalize_specifications_aliases(specifications)
    direct_value = normalized.get(canonical_key)
    if direct_value is not None:
        return _clean_list(direct_value)
    for alias in LIST_SPEC_ALIASES.get(canonical_key, ()):
        candidate = specifications.get(alias) if specifications else None
        values = _clean_list(candidate)
        if values:
            return values
    return []


def build_canonical_product_row(product_like: Any) -> dict[str, Any]:
    payload = _as_dict(product_like)
    specifications = normalize_specifications_aliases(payload.get("specifications"))
    price_full, price_discount = _normalize_export_prices(payload)

    row = {
        "brand": _clean_scalar(payload.get("brand")),
        "url": _clean_scalar(payload.get("url")),
        "price_full": price_full,
        "price_discount": price_discount,
        "product_name": (
            _clean_scalar(payload.get("product_name"))
            or _clean_scalar(payload.get("raw_title"))
            or _clean_scalar(payload.get("title"))
        ),
        "product_description": (
            _clean_scalar(payload.get("product_description"))
            or _clean_scalar(payload.get("raw_description"))
            or _clean_scalar(payload.get("description"))
        ),
        "composition": (
            _clean_scalar(payload.get("composition"))
            or _clean_scalar(specifications.get("composition"))
        ),
        "gender": (
            _clean_scalar(payload.get("gender"))
            or _clean_scalar(specifications.get("gender"))
        ),
        "age_group": (
            _clean_scalar(payload.get("age_group"))
            or _clean_scalar(specifications.get("age_group"))
        ),
        "product_group": (
            _clean_scalar(payload.get("product_group"))
            or _clean_scalar(specifications.get("product_group"))
        ),
        "available_colors": (
            _clean_list(payload.get("available_colors"))
            or _extract_list_from_specs(specifications, "available_colors")
        ),
        # D-01: raw_colors reflete SOMENTE o que a fonte declarou como bruto —
        # NUNCA reconstruído a partir de available_colors/specifications. Ausência
        # de bruto na origem produz [] mesmo que available_colors exista.
        "raw_colors": _clean_raw_list(payload.get("raw_colors")),
        "available_sizes": (
            _clean_list(payload.get("available_sizes"))
            or _extract_list_from_specs(specifications, "available_sizes")
        ),
        # D-01: mesma regra de raw_colors — sem fallback para available_sizes/specs.
        "raw_sizes": _clean_raw_list(payload.get("raw_sizes")),
        # Grade completa (todas as variantes de tamanho, com ou sem estoque) —
        # distinta de available_sizes acima. Sem fallback para specs (não é um
        # atributo textual livre, vem sempre de items/variantes do adapter).
        "size_grid": _clean_list(payload.get("size_grid")),
        "raw_size_grid": _clean_raw_list(payload.get("raw_size_grid")),
        # D-03: product_code aceita SOMENTE o ID nativo bruto fornecido pelo adapter
        # (payload.product_code) — sem fallback para specifications (Referencia/Código/
        # SKU), composição, hash ou case-fold.
        "product_code": _clean_scalar(payload.get("product_code")),
        "category": (
            _clean_scalar(payload.get("category"))
            or _clean_scalar(specifications.get("category"))
        ),
        "rating": _coerce_float(payload.get("rating")),
        "review_count": _coerce_int(payload.get("review_count")),
    }

    if row["rating"] is None:
        row["rating"] = _coerce_float(specifications.get("rating"))
    if row["review_count"] is None:
        row["review_count"] = _coerce_int(specifications.get("review_count"))

    result = {column: row.get(column) for column in CANONICAL_PRODUCT_COLUMNS}
    return result


def build_canonical_export_dataframe(products: Sequence[Any]) -> pd.DataFrame:
    rows = [build_canonical_product_row(product) for product in products]
    if not rows:
        return pd.DataFrame(columns=CANONICAL_PRODUCT_COLUMNS)

    df = pd.DataFrame(rows)
    for column in (
        "available_colors",
        "raw_colors",
        "available_sizes",
        "raw_sizes",
        "size_grid",
        "raw_size_grid",
    ):
        if column in df.columns:
            df[column] = df[column].apply(
                lambda value: ", ".join(value) if isinstance(value, list) else value
            )

    return df[CANONICAL_PRODUCT_COLUMNS]


def build_standard_export_dataframe(products: Sequence[Any]) -> pd.DataFrame:
    """Gera o Excel único de produto para busca, SKU e varredura.

    Ausência na origem é representada por célula vazia.  Nunca deduzimos cor,
    transportadora ou prazo a partir de outro atributo do produto.
    """
    rows: list[dict[str, Any]] = []
    for product in products:
        payload = _as_dict(product)
        canonical = build_canonical_product_row(payload)
        raw_price = _coerce_float(payload.get("price"))
        price_current = (raw_price
                         or canonical["price_discount"]
                         or canonical["price_full"])
        price_discount = canonical["price_discount"]
        price_original = (_coerce_float(payload.get("price_original"))
                          or canonical["price_full"])
        if price_discount is None and price_original is not None and raw_price is not None and price_original > raw_price:
            price_discount = raw_price
        discount_value = None
        if price_original is not None and price_discount is not None and price_original > price_discount:
            discount_value = round(price_original - price_discount, 2)

        shipping = _coerce_float(payload.get("shipping_price"))
        free_shipping = _clean_bool(payload.get("is_free_shipping"))
        total = round(price_current + shipping, 2) if price_current is not None and shipping is not None else price_current
        if free_shipping is True and price_current is not None:
            total = price_current

        colors = canonical["available_colors"]
        sizes = canonical["available_sizes"]
        size_grid = canonical["size_grid"]
        rows.append({
            "Marca": _safe_export_text(_clean_scalar(payload.get("brand")) or _clean_scalar(payload.get("marketplace")) or ""),
            "Vendedor": _safe_export_text(_clean_scalar(payload.get("seller")) or ""),
            "URL do Produto": _safe_export_text(canonical["url"] or ""),
            "Código do Produto": _safe_export_text(canonical["product_code"] or _clean_scalar(payload.get("sku")) or _clean_scalar(payload.get("sku_id")) or ""),
            "Nome do Produto": _safe_export_text(canonical["product_name"] or ""),
            "Cor": _safe_export_text(_clean_scalar(payload.get("color")) or _clean_scalar(payload.get("selected_color")) or ""),
            "Descrição do Produto": _safe_export_text(canonical["product_description"] or ""),
            "Composição": _safe_export_text(canonical["composition"] or ""),
            "Cores Disponíveis": _safe_export_text(", ".join(colors) if colors else ""),
            "Tamanhos Disponíveis": _safe_export_text(", ".join(sizes) if sizes else ""),
            "Grade de Tamanhos": _safe_export_text(", ".join(size_grid) if size_grid else ""),
            "Categoria": _safe_export_text(canonical["category"] or ""),
            "Avaliação": canonical["rating"] if canonical["rating"] is not None else "",
            "Preço Atual": price_current if price_current is not None else "",
            "Preço com Desconto": price_discount if price_discount is not None else "",
            "Valor do Desconto": discount_value if discount_value is not None else "",
            "Frete": _safe_export_text(_clean_scalar(payload.get("shipping_carrier")) or _clean_scalar(payload.get("carrier")) or ""),
            "Tempo de Frete": _safe_export_text(_clean_scalar(payload.get("estimated_delivery_days")) or _clean_scalar(payload.get("delivery_time")) or ""),
            "Frete Grátis": "Sim" if free_shipping is True else "Não" if free_shipping is False else "",
            "Valor do Frete": shipping if shipping is not None else "",
            "Preço Total": total if total is not None else "",
        })
    return pd.DataFrame(rows, columns=STANDARD_EXPORT_COLUMNS)


def extract_category_product_item(product_like: Any, default_brand: str = "") -> dict[str, Any]:
    """Converte um produto (RawProductBronze, model ou dict) no formato consumido
    pelo evento WebSocket category_product_item do frontend."""
    from core.models import resolve_effective_price, resolve_original_price

    p = _as_dict(product_like)
    full_price = _coerce_float(p.get("price_full"))
    discount_price = _coerce_float(p.get("price_discount"))
    is_delta = bool(p.get("price_discount_is_delta", False))

    eff = resolve_effective_price(full_price, discount_price, is_delta)
    orig = resolve_original_price(full_price, discount_price, is_delta)

    raw_price = _coerce_float(p.get("price"))
    raw_orig = _coerce_float(p.get("price_original"))

    price = raw_price if raw_price is not None else eff
    if price is None:
        price = discount_price if discount_price is not None else full_price

    price_orig = raw_orig if raw_orig is not None else orig
    if price_orig is None:
        price_orig = full_price if full_price is not None else price

    prod_name = (
        _clean_scalar(p.get("product_name"))
        or _clean_scalar(p.get("raw_title"))
        or _clean_scalar(p.get("title"))
        or ""
    )
    prod_url = str(_clean_scalar(p.get("url")) or "")
    prod_img = _clean_scalar(p.get("image_url")) or _clean_scalar(p.get("image"))
    prod_sku = (
        _clean_scalar(p.get("sku_id"))
        or _clean_scalar(p.get("product_code"))
        or _clean_scalar(p.get("sku"))
    )

    prod_avail = _clean_bool(p.get("is_available"))
    if prod_avail is None:
        prod_avail = _clean_bool(p.get("stock_availability"))
    if prod_avail is None:
        prod_avail = _clean_bool(p.get("available"))
    if prod_avail is None:
        prod_avail = True

    brand = _clean_scalar(p.get("brand")) or default_brand

    return {
        "product_name": str(prod_name),
        "price": price,
        "price_original": price_orig,
        "url": prod_url,
        "image_url": str(prod_img) if prod_img else None,
        "sku": str(prod_sku) if prod_sku else None,
        "brand": str(brand),
        "is_available": prod_avail,
        "available_sizes": _clean_list(p.get("available_sizes")),
        "size_grid": _clean_list(p.get("size_grid")),
        "shipping_price": _coerce_float(p.get("shipping_price")),
        "shipping_carrier": _clean_scalar(p.get("shipping_carrier")),
        "estimated_delivery_days": _clean_scalar(p.get("estimated_delivery_days")),
        "is_free_shipping": _clean_bool(p.get("is_free_shipping")),
    }

