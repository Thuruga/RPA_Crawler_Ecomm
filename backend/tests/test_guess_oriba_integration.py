import pytest

from services.brand_service import brand_service
from services.engines.factory import EngineFactory
from services.engines.vtex_engine import VTEXEngine
from services.engines.wake_engine import WakeEngine


def test_brand_service_loads_guess_and_oriba():
    """Valida que o BrandService carrega Guess Brasil e Oriba com Pydantic válido."""
    guess = brand_service.get_brand("guess")
    assert guess is not None
    assert getattr(guess, "brand_key", None) == "guess"
    assert getattr(guess, "brand_name", None) == "Guess Brasil"
    assert getattr(guess, "domain", None) == "www.guessbrasil.com.br"
    assert getattr(guess, "is_active", None) is True

    oriba = brand_service.get_brand("oriba")
    assert oriba is not None
    assert getattr(oriba, "brand_key", None) == "oriba"
    assert getattr(oriba, "brand_name", None) == "Oriba"
    assert getattr(oriba, "domain", None) == "www.oriba.com.br"
    assert getattr(oriba, "is_active", None) is True


def test_engine_factory_resolves_correct_engines():
    """Valida que o EngineFactory instancia VTEXEngine para Guess e WakeEngine para Oriba."""
    guess_engine = EngineFactory.get_engine("guess")
    assert isinstance(guess_engine, VTEXEngine)
    assert guess_engine.brand_key == "guess"
    assert guess_engine.get_engine_name() == "VTEX"

    oriba_engine = EngineFactory.get_engine("oriba")
    assert isinstance(oriba_engine, WakeEngine)
    assert oriba_engine.brand_key == "oriba"
    assert oriba_engine.get_engine_name() == "Wake"


def test_guess_and_oriba_canonical_mappings_integrity():
    """Valida que as categorias canônicas masculinas essenciais estão devidamente mapeadas."""
    essential_slugs = {
        "camisas",
        "camisetas",
        "polos",
        "calcas",
        "bermudas",
        "jaquetas",
    }

    # Guess
    guess_data = brand_service.get_brand("guess")
    guess_mappings = getattr(guess_data, "mappings", [])
    guess_slugs = {getattr(m, "canonical_slug", None) for m in guess_mappings}
    for slug in essential_slugs:
        assert slug in guess_slugs, f"Categoria '{slug}' deve estar mapeada para Guess"

    # Oriba
    oriba_data = brand_service.get_brand("oriba")
    oriba_mappings = getattr(oriba_data, "mappings", [])
    oriba_slugs = {getattr(m, "canonical_slug", None) for m in oriba_mappings}
    for slug in essential_slugs:
        assert slug in oriba_slugs, f"Categoria '{slug}' deve estar mapeada para Oriba"


def test_domain_and_account_configurations():
    """Valida requisitos críticos de domínio e conta de infraestrutura."""
    guess = brand_service.get_brand("guess")
    assert getattr(guess, "vtex_account", None) == "guessbr"

    oriba = brand_service.get_brand("oriba")
    assert getattr(oriba, "domain", None) == "www.oriba.com.br"
    token = getattr(oriba, "wake_access_token", None)
    assert token and token.startswith("tcs_oriba_")
