# -*- coding: utf-8 -*-
try:
    import pytest
except ImportError:
    pytest = None
from services.engines.base_engine import BaseEngine
from services.brand_service import brand_service
from services.engines.factory import engine_factory
from services.engines.zara_engine import ZaraEngine


def test_filter_boys_fashion_discards_girls_clothing():
    products = [
        {"title": "Vestido Infantil Estampado Floral", "gender": "feminino"},
        {"title": "Saia Rodada Menina", "gender": ""},
        {"title": "Blusa Manga Bufante Garotas", "gender": "female"},
        {"title": "Tiara com Laço Infantil", "gender": ""},
        {"title": "Sapatilha Princesa Rosa", "gender": "menina"},
        {"title": "Biquíni Infantil Babados", "gender": ""},
        {"title": "Boneca Bailarina Pelúcia", "gender": ""},
    ]

    filtered = BaseEngine.filter_boys_fashion(products)
    assert len(filtered) == 0


def test_filter_boys_fashion_keeps_unisex_and_boys():
    products = [
        {"title": "Camiseta Infantil Masculina Manga Curta", "gender": "masculino"},
        {"title": "Bermuda Jeans Menino", "gender": "boy"},
        {"title": "Moletom Infantil Básico Unissex Cinza", "gender": "unisex"},
        {"title": "Tênis Casual Infantil Branco", "gender": ""},
        {"title": "Meia Básica Cano Médio Infantil", "gender": None},
        {"title": "Jaqueta Corta-Vento Menino", "gender": "masculino"},
    ]

    filtered = BaseEngine.filter_boys_fashion(products)
    assert len(filtered) == 6


def test_filter_boys_fashion_discards_female_gender_even_with_neutral_title():
    products = [
        {"title": "Camiseta Básica Branca", "gender": "feminino"},
        {"title": "Camiseta Básica Branca", "gender": "masculino"},
    ]

    filtered = BaseEngine.filter_boys_fashion(products)
    assert len(filtered) == 1
    assert filtered[0]["gender"] == "masculino"


def test_zara_kids_mappings_and_config():
    brand = brand_service.get_brand("zara")
    assert brand is not None
    key = getattr(brand, "brand_key", None) or brand.get("brand_key")
    assert key == "zara"
    mappings = getattr(brand, "mappings", None) or brand.get("mappings") or []
    kids_mappings = [m for m in mappings if "infantil" in (getattr(m, "canonical_slug", None) or (m.get("canonical_slug") if isinstance(m, dict) else ""))]
    assert len(kids_mappings) > 0


def test_zara_engine_factory():
    engine = engine_factory.get_engine("zara")
    assert isinstance(engine, ZaraEngine)
    assert engine.brand_key == "zara"
    config = engine._brand_config()
    assert config["brand_name"] == "Zara"
