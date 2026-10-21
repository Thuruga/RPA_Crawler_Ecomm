"""
Audit script: Test shipping calculation across all brands in the system.
Tests:
1. Brand registration and metadata (engine, domain, tokens)
2. Resolver routing (resolve_shipping_provider)
3. Live / Endpoint calculation test for each brand:
   - VTEX brands: test VtexApiClient.calculate_for_brand (with real SKU from catalog search)
   - Wake brands: test WakeShipping.calculate (with real product)
   - Shopify brands: test ShopifyShipping.calculate (with real product)
   - Marketplaces: test shipping providers / advanced shipping
   - Other brands (Algolia, Zara, Linx, Dafiti, Privalia): test status / unsupported behavior
"""
import asyncio
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

# Adjust path
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv
load_dotenv(BACKEND_DIR / ".env")

from core.db import db
from core.models import SearchProductResult
from services.brand_service import brand_service
from services.shipping.base import (
    ShippingCalculation,
    ShippingState,
    apply_shipping_calculation,
    is_url_allowed_for_brand,
)
from services.shipping.resolver import resolve_shipping_provider
from services.vtex_api_scraper import VtexApiClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("ShippingAudit")

TEST_CEP = "01310100"  # Av. Paulista, Sao Paulo - SP

async def get_all_brands():
    """Load brands from brand_service and database."""
    await brand_service.load_from_db()
    brands = list(brand_service.brands.values())
    return brands

async def fetch_vtex_sample_sku(domain: str, brand: Any = None) -> Optional[Dict[str, str]]:
    """Try to get 1 SKU from VTEX catalog search."""
    from curl_cffi.requests import AsyncSession

    clean_domain = domain.replace("https://", "").replace("http://", "").strip("/")
    domains_to_try = [clean_domain]
    vtex_acc = getattr(brand, "vtex_account", None) or (brand.get("vtex_account") if isinstance(brand, dict) else None)
    if vtex_acc:
        domains_to_try.append(f"{vtex_acc}.vtexcommercestable.com.br")

    for dom in domains_to_try:
        url = f"https://{dom}/api/catalog_system/pub/products/search?_from=0&_to=5"
        try:
            async with AsyncSession(impersonate="chrome120", timeout=12) as session:
                resp = await session.get(url)
                if resp.status_code in (200, 206):
                    try:
                        data = resp.json()
                    except Exception:
                        continue
                    if isinstance(data, list) and len(data) > 0:
                        for prod in data:
                            items = prod.get("items", [])
                            for item in items:
                                sellers = item.get("sellers", [])
                                seller_id = sellers[0].get("sellerId", "1") if sellers else "1"
                                sku_id = item.get("itemId")
                                committable = sellers[0].get("committableQuantity", 0) if sellers else 0
                                available_qty = sellers[0].get("commertialOffer", {}).get("AvailableQuantity", 0) if sellers else 0
                                if committable > 0 or available_qty > 0:
                                    return {
                                        "sku_id": str(sku_id),
                                        "seller_id": str(seller_id),
                                        "product_name": prod.get("productName", ""),
                                        "url": prod.get("link", ""),
                                    }
                        item0 = data[0].get("items", [{}])[0]
                        sellers0 = item0.get("sellers", [])
                        return {
                            "sku_id": str(item0.get("itemId", "1")),
                            "seller_id": str(sellers0[0].get("sellerId", "1") if sellers0 else "1"),
                            "product_name": data[0].get("productName", ""),
                            "url": data[0].get("link", ""),
                        }
        except Exception as e:
            logger.debug("Failed to fetch VTEX sample from %s: %s", dom, e)
    return None

async def test_vtex_brand(brand: Any) -> Dict[str, Any]:
    brand_key = brand.brand_key
    domain = brand.domain
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": domain,
        "status": "pending",
        "details": {},
    }

    # 1. Fetch sample SKU
    sample = await fetch_vtex_sample_sku(domain, brand)
    if not sample:
        result["status"] = "NO_SAMPLE_PRODUCT"
        result["details"] = {"error": "Could not retrieve sample product from VTEX catalog API"}
        return result

    result["details"]["sample_product"] = sample

    # 2. Test VtexApiClient.calculate_for_brand
    try:
        calc_result = await VtexApiClient.calculate_for_brand(
            brand_key=brand_key,
            sku_id=sample["sku_id"],
            seller_id=sample["seller_id"],
            zipcode=TEST_CEP,
        )
        state = calc_result.get("state")
        options = calc_result.get("shipping_options", [])
        result["details"]["state"] = state
        result["details"]["options_count"] = len(options)
        if options:
            first = options[0]
            result["details"]["cheapest_option"] = {
                "price": getattr(first, "price", None),
                "status": getattr(first, "status", None),
                "days": getattr(first, "estimated_delivery_days", None),
                "raw_text": getattr(first, "raw_text", None),
            }
        result["status"] = "SUCCESS" if state == "available" else f"RETURNED_{state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_wake_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.wake import WakeShipping
    from services.engines.factory import engine_factory

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    # Check token
    token = getattr(brand, "wake_access_token", None)
    result["details"]["has_token"] = bool(token)

    # Try to get a sample product using wake engine
    sample_prod = None
    try:
        engine = engine_factory.get_engine(brand_key)
        if hasattr(engine, "search"):
            search_res = await engine.search("camisa", max_results=2)
            if search_res and search_res.products:
                sample_prod = search_res.products[0]
                result["details"]["sample_url"] = sample_prod.url
    except Exception as e:
        logger.debug("Wake search sample failed for %s: %s", brand_key, e)

    provider = WakeShipping()
    if not sample_prod:
        clean_dom = brand.domain.replace("https://", "").replace("http://", "").strip("/")
        sample_prod = SearchProductResult(
            brand=brand_key,
            product_name="Sample",
            url=f"https://{clean_dom}/p",
            price_full=100.0,
        )

    try:
        calc = await provider.calculate(sample_prod, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_shopify_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.shopify import ShopifyShipping
    from services.engines.factory import engine_factory

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    sample_url = None
    try:
        clean_dom = brand.domain.replace("https://", "").replace("http://", "").strip("/")
        # Try products.json
        p_url = f"https://{clean_dom}/products.json?limit=2"
        async with aiohttp.ClientSession() as session:
            async with session.get(p_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prods = data.get("products", [])
                    if prods:
                        handle = prods[0].get("handle")
                        sample_url = f"https://{clean_dom}/products/{handle}"
                        result["details"]["sample_url"] = sample_url
    except Exception as e:
        logger.debug("Shopify products.json failed for %s: %s", brand_key, e)

    if not sample_url:
        clean_dom = brand.domain.replace("https://", "").replace("http://", "").strip("/")
        sample_url = f"https://{clean_dom}/products/sample"

    provider = ShopifyShipping()
    product = SearchProductResult(
        brand=brand_key,
        product_name="Sample",
        url=sample_url,
        price_full=100.0,
    )
    try:
        calc = await provider.calculate(product, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_marketplace(brand: Any) -> Dict[str, Any]:
    brand_key = brand.brand_key
    engine_name = brand.engine or brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": engine_name,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    # Test resolver
    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    # Test engine calculate_shipping
    from services.engines.factory import engine_factory
    try:
        engine = engine_factory.get_engine(brand_key)
        has_calc = hasattr(engine, "calculate_shipping")
        has_adv = hasattr(engine, "calculate_shipping_advanced")
        result["details"]["has_calculate_shipping"] = has_calc
        result["details"]["has_calculate_shipping_advanced"] = has_adv
        result["status"] = "CAPABLE" if (has_calc or has_adv) else "UNSUPPORTED"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = str(e)

    return result

async def test_linx_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.linx import LinxShipping
    from services.engines.factory import engine_factory

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    sample_prod = None
    try:
        engine = engine_factory.get_engine(brand_key)
        if hasattr(engine, "search"):
            search_res = await engine.search("camisa", max_results=2)
            if search_res and search_res.products:
                sample_prod = search_res.products[0]
                result["details"]["sample_url"] = sample_prod.url
    except Exception as e:
        logger.debug("Linx search sample failed for %s: %s", brand_key, e)

    if not sample_prod:
        clean_dom = brand.domain.replace("https://", "").replace("http://", "").strip("/")
        sample_prod = SearchProductResult(
            brand=brand_key,
            product_name="Sample",
            url=f"https://{clean_dom}/camisa-social-manga-longa-slim-fit-fil-a-fil-azul-marinho-0036676-p46143?v=116496",
            price_full=499.0,
        )

    try:
        calc = await provider.calculate(sample_prod, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
                "service_name": calc.shipping_options[0].service_name,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_zara_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.zara import ZaraShipping

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    clean_dom = brand.domain.replace("https://", "").replace("http://", "").strip("/")
    sample_prod = SearchProductResult(
        brand=brand_key,
        product_name="Camisa Oxford",
        url=f"https://{clean_dom}/br/pt/camisa-oxford-p03057300.html",
        price_full=199.0,
    )

    try:
        calc = await provider.calculate(sample_prod, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
                "service_name": calc.shipping_options[0].service_name,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_dafiti_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.dafiti import DafitiShipping

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    sample_prod = SearchProductResult(
        brand=brand_key,
        product_name="Tenis Masculino",
        url=f"https://{brand.domain}/tenis-masculino-p1.html",
        price_full=129.90,
    )

    try:
        calc = await provider.calculate(sample_prod, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
                "service_name": calc.shipping_options[0].service_name,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_lacoste_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.lacoste import LacosteShipping

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    sample_prod = SearchProductResult(
        brand=brand_key,
        product_name="Polo Classic Fit",
        url=f"https://www.lacoste.com/br/polo-classic-p1.html",
        price_full=549.00,
    )

    try:
        calc = await provider.calculate(sample_prod, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
                "service_name": calc.shipping_options[0].service_name,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_privalia_brand(brand: Any) -> Dict[str, Any]:
    from services.shipping.privalia import PrivaliaShipping

    brand_key = brand.brand_key
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": brand.engine,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    sample_prod = SearchProductResult(
        brand=brand_key,
        product_name="Camisa Polo Masculina",
        url=f"https://{brand.domain}/item/camisa-polo-masculina",
        price_full=189.90,
    )

    try:
        calc = await provider.calculate(sample_prod, TEST_CEP, brand)
        result["details"]["state"] = calc.state
        result["details"]["message"] = calc.message
        result["details"]["options_count"] = len(calc.shipping_options)
        if calc.shipping_options:
            result["details"]["cheapest_option"] = {
                "price": calc.shipping_options[0].price,
                "status": calc.shipping_options[0].status,
                "days": calc.shipping_options[0].estimated_delivery_days,
                "service_name": calc.shipping_options[0].service_name,
            }
        result["status"] = "SUCCESS" if calc.state == "available" else f"RETURNED_{calc.state.upper()}"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = f"{type(e).__name__}: {str(e)}"

    return result

async def test_other_brand(brand: Any) -> Dict[str, Any]:
    brand_key = brand.brand_key
    engine_name = brand.engine
    result = {
        "brand_key": brand_key,
        "brand_name": brand.brand_name,
        "engine": engine_name,
        "domain": brand.domain,
        "status": "pending",
        "details": {},
    }

    # Check resolver
    provider = resolve_shipping_provider(brand)
    result["details"]["provider_class"] = type(provider).__name__

    # Check engine
    from services.engines.factory import engine_factory
    try:
        engine = engine_factory.get_engine(brand_key)
        has_calc = hasattr(engine, "calculate_shipping")
        result["details"]["has_calculate_shipping"] = has_calc
        if has_calc:
            # Call it with dummy
            calc_val = await engine.calculate_shipping({"url": f"https://{brand.domain}/test"}, TEST_CEP)
            result["details"]["calculate_shipping_returns"] = str(calc_val)
        result["status"] = "NOT_IMPLEMENTED" if (has_calc and calc_val is None) else "UNKNOWN"
    except Exception as e:
        result["status"] = "ERROR"
        result["details"]["error"] = str(e)

    return result

async def main():
    print("=" * 80)
    print("AUDIT DE CALCULO DE FRETE EM TODAS AS MARCAS")
    print(f"CEP de Teste: {TEST_CEP}")
    print("=" * 80)

    brands = await get_all_brands()
    print(f"Total de marcas encontradas: {len(brands)}")

    results = []
    for brand in sorted(brands, key=lambda b: (b.engine or "", b.brand_key)):
        eng = (brand.engine or "").lower()
        bk = brand.brand_key
        print(f"\n---> Testando marca [{bk}] (Engine: {eng}, Domain: {brand.domain})...")

        if eng == "vtex":
            res = await test_vtex_brand(brand)
        elif eng == "wake":
            res = await test_wake_brand(brand)
        elif eng == "shopify":
            res = await test_shopify_brand(brand)
        elif eng in ("mercadolivre", "amazon", "netshoes") or bk in ("mercado_livre", "amazon", "netshoes"):
            res = await test_marketplace(brand)
        elif eng == "linx":
            res = await test_linx_brand(brand)
        elif eng == "zara":
            res = await test_zara_brand(brand)
        elif eng == "dafiti" or bk == "dafiti":
            res = await test_dafiti_brand(brand)
        elif eng in ("algolia", "sfcc", "lacoste") or bk == "lacoste":
            res = await test_lacoste_brand(brand)
        elif eng == "privalia" or bk == "privalia":
            res = await test_privalia_brand(brand)
        else:
            res = await test_other_brand(brand)

        print(f"     Resultado: {res['status']} | Detalhes: {json.dumps(res['details'], ensure_ascii=False)}")
        results.append(res)

    print("\n" + "=" * 80)
    print("RESUMO CONSOLIDADO POR STATUS:")
    print("=" * 80)
    summary = {}
    for r in results:
        st = r["status"]
        summary.setdefault(st, []).append(r["brand_key"])

    for st, b_list in summary.items():
        print(f"{st} ({len(b_list)}): {', '.join(b_list)}")

    # Save artifact
    output_path = BACKEND_DIR / "shipping_audit_results.json"
    output_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResultados completos salvos em: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
