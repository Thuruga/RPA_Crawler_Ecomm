"""Contrato Nyquist Wave 0 (Phase 01, Plano 01-01) — proveniência, normalização e identidade.

Cobre D-01 (cores/tamanhos brutos coexistem com normalizados, sem reconstrução a partir
do normalizado) e D-03 (product_code é apenas o ID nativo do adapter, sem fallback de
specifications/SKU/composição/hash/case-fold).
"""
from core.models import RawProductBronze, SearchProductResult
from services.product_contract import (
    build_canonical_product_row,
    normalize_color_values,
    normalize_specifications_aliases,
)


def _raw_product(**overrides):
    defaults = dict(
        url="https://example.com/produto",
        brand="marca",
        raw_title="Produto Teste",
        raw_description="Descrição",
        price_full=100.0,
        image_url="https://example.com/img.png",
    )
    defaults.update(overrides)
    return RawProductBronze(**defaults)


# ---------------------------------------------------------------------------
# Modelos aceitam raw_colors/raw_sizes de forma aditiva
# ---------------------------------------------------------------------------

def test_raw_product_bronze_accepts_raw_colors_and_sizes():
    product = _raw_product(raw_colors=["Azul Marinho"], raw_sizes=["P", "M"])
    assert product.raw_colors == ["Azul Marinho"]
    assert product.raw_sizes == ["P", "M"]


def test_raw_product_bronze_defaults_raw_fields_to_empty_list():
    product = _raw_product()
    assert product.raw_colors == []
    assert product.raw_sizes == []


def test_search_product_result_accepts_raw_colors_and_sizes():
    result = SearchProductResult(
        brand="marca",
        product_name="Produto",
        url="https://example.com/p",
        raw_colors=["Azul Marinho"],
        raw_sizes=["G"],
    )
    assert result.raw_colors == ["Azul Marinho"]
    assert result.raw_sizes == ["G"]


def test_raw_product_bronze_instances_do_not_share_mutable_lists():
    a = _raw_product()
    b = _raw_product()
    a.raw_colors.append("Vermelho")
    assert b.raw_colors == []


def test_search_product_result_instances_do_not_share_mutable_lists():
    a = SearchProductResult(brand="m", product_name="p", url="https://x/1")
    b = SearchProductResult(brand="m", product_name="p", url="https://x/2")
    a.raw_sizes.append("P")
    assert b.raw_sizes == []


# ---------------------------------------------------------------------------
# D-01 — bruto preserva capitalização/ordem; normalização vive só em available_colors
# ---------------------------------------------------------------------------

def test_raw_colors_preserve_exact_casing_and_order():
    product = _raw_product(raw_colors=["Azul Marinho", "Preto"])
    assert product.raw_colors == ["Azul Marinho", "Preto"]
    assert product.raw_colors[0] == "Azul Marinho"


def test_build_canonical_product_row_does_not_reconstruct_raw_colors_from_available():
    # Fonte só forneceu available_colors (já normalizado) — nenhum bruto declarado.
    product = _raw_product(available_colors=["Azul"])
    row = build_canonical_product_row(product)
    assert row["raw_colors"] == []
    assert row["available_colors"] == ["Azul"]


def test_build_canonical_product_row_preserves_raw_colors_when_declared():
    product = _raw_product(raw_colors=["Azul Marinho"], available_colors=["Azul"])
    row = build_canonical_product_row(product)
    assert row["raw_colors"] == ["Azul Marinho"]
    assert row["available_colors"] == ["Azul"]


def test_build_canonical_product_row_raw_sizes_empty_when_source_silent():
    product = _raw_product(available_sizes=["P", "M"])
    row = build_canonical_product_row(product)
    assert row["raw_sizes"] == []
    assert row["available_sizes"] == ["P", "M"]


def test_build_canonical_product_row_cleans_and_dedupes_raw_colors_preserving_case():
    product = _raw_product(raw_colors=["Azul Marinho", "Azul Marinho", "azul marinho", " Preto "])
    row = build_canonical_product_row(product)
    # Duplicata EXATA removida; "azul marinho" (case distinto) NÃO é fundido com "Azul Marinho".
    assert row["raw_colors"] == ["Azul Marinho", "azul marinho", "Preto"]


def test_build_canonical_product_row_empty_raw_list_stays_empty():
    product = _raw_product(raw_colors=[], raw_sizes=[])
    row = build_canonical_product_row(product)
    assert row["raw_colors"] == []
    assert row["raw_sizes"] == []


# ---------------------------------------------------------------------------
# size_grid — grade completa de tamanhos, distinta de available_sizes (estoque)
# ---------------------------------------------------------------------------

def test_build_canonical_product_row_size_grid_independent_of_available_sizes():
    # Grade completa (P/M/G) inclui tamanhos sem estoque; available_sizes só o que sobrou.
    product = _raw_product(
        available_sizes=["P", "G"],
        raw_sizes=["P", "G"],
        size_grid=["P", "M", "G"],
        raw_size_grid=["P", "M", "G"],
    )
    row = build_canonical_product_row(product)
    assert row["available_sizes"] == ["P", "G"]
    assert row["size_grid"] == ["P", "M", "G"]
    assert row["raw_size_grid"] == ["P", "M", "G"]


def test_build_canonical_product_row_size_grid_defaults_to_empty_when_source_silent():
    product = _raw_product(available_sizes=["P", "M"])
    row = build_canonical_product_row(product)
    # Ausência de dado de grade na fonte produz [] — nunca inferido de available_sizes.
    assert row["size_grid"] == []
    assert row["raw_size_grid"] == []
    assert row["available_sizes"] == ["P", "M"]


def test_canonical_product_columns_include_size_grid():
    from services.product_contract import CANONICAL_PRODUCT_COLUMNS

    assert "size_grid" in CANONICAL_PRODUCT_COLUMNS
    assert "raw_size_grid" in CANONICAL_PRODUCT_COLUMNS


# ---------------------------------------------------------------------------
# normalize_color_values — mapa exato explícito, sem NLP/fuzzy
# ---------------------------------------------------------------------------

def test_normalize_color_values_uses_only_explicit_exact_map():
    aliases = {"azul marinho": "Azul", "preto": "Preto"}
    result = normalize_color_values(["Azul Marinho", "PRETO"], aliases)
    assert result == ["Azul", "Preto"]


def test_normalize_color_values_unknown_alias_passes_through_unchanged():
    aliases = {"azul marinho": "Azul"}
    result = normalize_color_values(["Verde Musgo"], aliases)
    assert result == ["Verde Musgo"]


def test_normalize_color_values_dedupes_after_mapping_preserving_first_order():
    aliases = {"azul marinho": "Azul", "azul royal": "Azul"}
    result = normalize_color_values(["Azul Marinho", "Azul Royal"], aliases)
    assert result == ["Azul"]


def test_normalize_color_values_empty_list_returns_empty():
    assert normalize_color_values([], {"azul marinho": "Azul"}) == []
    assert normalize_color_values(None, {"azul marinho": "Azul"}) == []


# ---------------------------------------------------------------------------
# D-03 — product_code é somente o ID nativo bruto do adapter
# ---------------------------------------------------------------------------

def test_product_code_not_sourced_from_specifications_referencia():
    product = _raw_product(product_code=None, specifications={"Referencia": "REF-999"})
    row = build_canonical_product_row(product)
    assert row["product_code"] is None


def test_product_code_preserved_verbatim_with_leading_zeros_and_punctuation():
    product = _raw_product(product_code="00123-AB")
    row = build_canonical_product_row(product)
    assert row["product_code"] == "00123-AB"


def test_product_code_absent_stays_none_even_with_other_spec_aliases_present():
    product = _raw_product(
        product_code=None,
        specifications={"Código do produto": "COD-1", "Ref.": "REF-1"},
    )
    row = build_canonical_product_row(product)
    assert row["product_code"] is None


def test_shipping_fields_untouched_by_row_builder():
    product = _raw_product(product_code="123")
    row = build_canonical_product_row(product)
    # Campos de shipping/identidade de SKU não fazem parte do contrato canônico de
    # exportação e não devem ser removidos/renomeados por esta mudança aditiva.
    assert "shipping_price" not in row
    assert "landed_price" not in row


# ---------------------------------------------------------------------------
# Fix 2 — normalize_specifications_aliases insensível a maiúscula/acento
# (grafias reais confirmadas ao vivo na auditoria: Hering, Zapalla, Austral,
# Track&Field).
# ---------------------------------------------------------------------------

def test_normalize_specifications_aliases_hering_uppercase_accented_keys():
    specs = {"COMPOSIÇÃO": "100% Algodão", "GÊNERO": "Masculino"}
    normalized = normalize_specifications_aliases(specs)
    assert normalized["composition"] == "100% Algodão"
    assert normalized["gender"] == "Masculino"


def test_normalize_specifications_aliases_zapalla_unaccented_and_lowercase_keys():
    specs = {"Genero": "Feminino", "Faixa etaria": "Adulto"}
    normalized = normalize_specifications_aliases(specs)
    assert normalized["gender"] == "Feminino"
    assert normalized["age_group"] == "Adulto"


def test_normalize_specifications_aliases_austral_english_age_alias():
    specs = {"age": "Infantil", "gender": "Unissex"}
    normalized = normalize_specifications_aliases(specs)
    assert normalized["age_group"] == "Infantil"
    assert normalized["gender"] == "Unissex"


def test_normalize_specifications_aliases_track_and_field_uppercase_and_titlecase_keys():
    specs = {"GÊNERO": "Feminino", "Composição": "90% Poliéster, 10% Elastano"}
    normalized = normalize_specifications_aliases(specs)
    assert normalized["gender"] == "Feminino"
    assert normalized["composition"] == "90% Poliéster, 10% Elastano"


def test_normalize_specifications_aliases_never_overwrites_present_canonical_key():
    # Regra preservada: a chave canônica já presente e não-vazia NUNCA é
    # sobrescrita por um alias com grafia divergente.
    specs = {"gender": "Feminino", "GÊNERO": "Masculino"}
    normalized = normalize_specifications_aliases(specs)
    assert normalized["gender"] == "Feminino"
