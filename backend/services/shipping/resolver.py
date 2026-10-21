from __future__ import annotations

from typing import Any

from services.shipping.base import get_field
from services.shipping.unsupported import UnsupportedShipping


def resolve_shipping_provider(brand: Any):
    engine = str(get_field(brand, "engine", "") or "").lower()
    if engine == "vtex":
        from services.shipping.vtex import VtexShipping

        return VtexShipping()
    if engine == "shopify":
        from services.shipping.shopify import ShopifyShipping

        return ShopifyShipping()
    if engine == "wake":
        from services.shipping.wake import WakeShipping

        return WakeShipping()
    if engine == "mercadolivre":
        from services.shipping.mercado_livre import MercadoLivreShipping

        return MercadoLivreShipping()
    if engine == "amazon":
        from services.shipping.amazon import AmazonShipping

        return AmazonShipping()
    if engine == "netshoes":
        from services.shipping.netshoes import NetshoesShipping

        return NetshoesShipping()
    if engine == "linx":
        from services.shipping.linx import LinxShipping

        return LinxShipping()
    if engine == "zara":
        from services.shipping.zara import ZaraShipping

        return ZaraShipping()
    if engine == "dafiti" or str(get_field(brand, "brand_key", "") or "").lower() == "dafiti":
        from services.shipping.dafiti import DafitiShipping

        return DafitiShipping()
    if engine in ("lacoste", "algolia", "sfcc") or str(get_field(brand, "brand_key", "") or "").lower() == "lacoste":
        from services.shipping.lacoste import LacosteShipping

        return LacosteShipping()
    if engine == "privalia" or str(get_field(brand, "brand_key", "") or "").lower() == "privalia":
        from services.shipping.privalia import PrivaliaShipping

        return PrivaliaShipping()
    return UnsupportedShipping(reason=f"Frete nao suportado para engine '{engine or 'unknown'}'")
