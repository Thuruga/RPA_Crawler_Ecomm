"""Fix 3 (quick-260723-eq1) — VtexApiClient.__init__ pré-popula a conta VTEX
configurada em brands.json (ex.: Foxton -> "vtex_account": "foxton"),
eliminando o GET extra de auto-descoberta em toda chamada de produto.

Hermetic: nenhuma chamada de rede real. Mocka `brand_service.get_brand`
(patch no símbolo importado em vtex_api_scraper) para cobrir os dois
cenários do __init__ sem tocar a rede.
"""
from __future__ import annotations

from core.models import DynamicBrand
from services import brand_service as brand_service_module
from services.vtex_api_scraper import VtexApiClient


def test_init_prepopulates_resolved_account_when_configured(monkeypatch):
    fake_brand = DynamicBrand(
        brand_key="foxton",
        brand_name="Foxton",
        domain="www.foxton.com.br",
        vtex_account="foxton",
    )
    monkeypatch.setattr(
        brand_service_module.brand_service,
        "get_brand",
        lambda key: fake_brand if key == "foxton" else None,
    )

    client = VtexApiClient(brand_name="foxton")

    assert client.resolved_account == "foxton"
    assert client.use_stable_fallback is True


def test_init_preserves_old_default_when_vtex_account_absent(monkeypatch):
    fake_brand = DynamicBrand(
        brand_key="unregtestvtex",
        brand_name="unregtestvtex",
        domain="teststore.vtex.com",
    )
    monkeypatch.setattr(
        brand_service_module.brand_service,
        "get_brand",
        lambda key: fake_brand if key == "unregtestvtex" else None,
    )

    client = VtexApiClient(brand_name="unregtestvtex")

    assert client.resolved_account is None
    assert client.use_stable_fallback is False
