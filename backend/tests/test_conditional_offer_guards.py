"""High-severity config and SSRF contracts (activated by Plan 03-03)."""
from __future__ import annotations

import pytest

try:
    from services.promotions import guards
except ImportError:
    guards = None

requires_impl = pytest.mark.skipif(guards is None, reason="temporary Wave 0 guard: remove in 03-03")


def test_wave0_guard_contract_is_collectable_before_implementation():
    assert guards is None or hasattr(guards, "validate_promotion_target")


@requires_impl
@pytest.mark.parametrize("target", [
    "http://brand.example/p",
    "https://user:pass@brand.example/p",
    "https://localhost/p",
    "https://127.0.0.1/p",
    "https://10.0.0.4/p",
    "https://169.254.1.1/p",
    "https://other.example/p",
])
def test_d03_absolute_target_rejects_http_userinfo_local_private_ip_and_foreign_host(target):
    with pytest.raises(ValueError):
        guards.validate_promotion_target(target, allowed_host="brand.example")


@requires_impl
def test_d03_redirect_chain_rejects_final_host_outside_brand():
    with pytest.raises(ValueError):
        guards.validate_redirect_chain(["https://brand.example/p", "https://other.example/p"], allowed_host="brand.example")


@requires_impl
@pytest.mark.parametrize("path", ["https://other.example/p", "//other.example/p", "relative", "/p?token=value"])
def test_d03_config_accepts_only_safe_relative_paths_without_sensitive_query(path):
    with pytest.raises(ValueError):
        guards.validate_probe_path(path)
    assert guards.validate_probe_path("/_v/rsv-prime/primepromotion") == "/_v/rsv-prime/primepromotion"


@requires_impl
def test_d07_registry_is_closed_and_never_imports_configured_code():
    from services.promotions.registry import PROMOTION_ADAPTERS, resolve_adapter

    assert set(PROMOTION_ADAPTERS) >= {"vtex", "shopify", "reserva_prime", "html"}
    with pytest.raises(ValueError):
        resolve_adapter("package.module:ArbitraryClass")


class _FakeConfirmationSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


def _advertised_pix():
    from core.models import ConditionalOffer, OfferScope, OfferSource

    return ConditionalOffer(
        condition_type="payment_method",
        condition_label="10% com Pix",
        eligibility="requires_payment_method",
        payment_method="pix",
        scope=OfferScope(sku_id="vtex-sku-1", seller_id="1"),
        state="advertised",
        source=OfferSource(kind="vtex_catalog", ref="teaser", authority="structured"),
    )


@requires_impl
@pytest.mark.asyncio
@pytest.mark.parametrize("response,error", [({"status": 200, "payload": {"items": []}}, None), (None, TimeoutError()), (None, RuntimeError("boom"))])
async def test_d03_confirmation_session_is_always_discarded(response, error):
    from services.promotions.confirmation import AnonymousConfirmationTransport

    session = _FakeConfirmationSession(response=response, error=error)
    transport = AnonymousConfirmationTransport(session_factory=lambda: session)
    await transport.confirm_vtex(
        brand_domain="example.com", advertised=_advertised_pix(), sku_id="vtex-sku-1"
    )
    assert session.closed is True


@requires_impl
@pytest.mark.asyncio
async def test_d03_confirmation_uses_only_allowlisted_simulation_without_external_cookie():
    from services.promotions.confirmation import AnonymousConfirmationTransport

    session = _FakeConfirmationSession({"status": 200, "payload": {"items": []}})
    transport = AnonymousConfirmationTransport(session_factory=lambda: session)
    await transport.confirm_vtex(
        brand_domain="example.com", advertised=_advertised_pix(), sku_id="vtex-sku-1"
    )
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/api/checkout/pub/orderForms/simulation")
    assert kwargs["allow_redirects"] is False
    assert "cookies" not in kwargs and "headers" not in kwargs


@requires_impl
@pytest.mark.asyncio
async def test_d03_confirmation_rejects_user_profile_payload():
    from services.promotions.confirmation import AnonymousConfirmationTransport

    session = _FakeConfirmationSession({"status": 200, "payload": {"items": []}})
    transport = AnonymousConfirmationTransport(session_factory=lambda: session)
    with pytest.raises(ValueError):
        await transport._post(
            platform="vtex",
            brand_domain="example.com",
            payload={"items": [], "clientProfileData": {"email": "person@example.com"}},
        )
    assert session.calls == []


@requires_impl
@pytest.mark.asyncio
async def test_d04_shopify_requires_matching_line_allocation():
    import json
    from pathlib import Path

    from core.models import ConditionalOffer, OfferScope, OfferSource
    from services.promotions.confirmation import AnonymousConfirmationTransport

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "phase03" / "shopify_cart_discount.json").read_text(encoding="utf-8")
    )
    advertised = ConditionalOffer(
        condition_type="payment_method",
        condition_label="10% no Pix",
        eligibility="requires_payment_method",
        payment_method="pix",
        scope=OfferScope(sku_id="20001"),
        state="advertised",
        source=OfferSource(kind="shopify_product", ref="tag", authority="structured"),
    )
    session = _FakeConfirmationSession({"status": 200, "payload": fixture})
    result = await AnonymousConfirmationTransport(session_factory=lambda: session).confirm_shopify(
        brand_domain="example.com", advertised=advertised, variant_id="20001"
    )
    assert result.status == "found"
    assert result.offers[0].offer_key == advertised.offer_key
