from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from core.models import SearchProductResult, ShippingInfo
from services.shipping.base import (
    BaseShipping,
    ShippingCalculation,
    ShippingState,
    brand_domain,
    get_field,
    is_url_allowed_for_brand,
    normalize_zipcode,
    sorted_shipping_options,
)

logger = logging.getLogger(__name__)


class LinxShipping(BaseShipping):
    """Provedor de frete para e-commerces baseados na plataforma Linx Commerce / DCG (ex: Brooksfield)."""

    def __init__(self, session_factory: Any = None, sleep: Any = None) -> None:
        self.session_factory = session_factory
        self.sleep = sleep or asyncio.sleep

    def _open_session(self) -> AsyncSession:
        if self.session_factory:
            return self.session_factory()
        return AsyncSession(impersonate="chrome120", timeout=15)

    def _parse_currency(self, text: str) -> Optional[float]:
        if not text:
            return None
        text_clean = text.strip()
        if "grátis" in text_clean.lower() or "gratis" in text_clean.lower():
            return 0.0
        match = re.search(r"R\$\s*([\d\.,]+)", text_clean, re.I)
        if match:
            raw = match.group(1).rstrip(".").strip()
            if "." in raw and "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
            elif "," in raw:
                raw = raw.replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                return None
        return None

    def _parse_days(self, text: str) -> Optional[int]:
        if not text:
            return None
        match = re.search(r"(\d+)\s*dia", text, re.I)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

    async def _resolve_product_and_sku(
        self,
        session: AsyncSession,
        product_url: str,
        product: SearchProductResult | dict[str, Any],
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve ProductID e SkuID com estoque positivo do produto Linx."""
        product_id = get_field(product, "shipping_product_id") or get_field(product, "product_code")
        sku_id = get_field(product, "shipping_variant_id")

        # Tenta extrair do link se faltar algum
        if not product_id:
            pid_match = re.search(r"-p(\d+)", product_url)
            if pid_match:
                product_id = pid_match.group(1)

        if not sku_id:
            v_match = re.search(r"[?&]v=(\d+)", product_url)
            if v_match:
                sku_id = v_match.group(1)

        # Sempre busca a PDP para validar estoque real e escolher variante disponível
        try:
            resp = await session.get(product_url)
            if resp.status_code == 200:
                html = resp.text
                if not product_id:
                    pid_m = re.search(r'name=[\'"]ProductID[\'"]\s+value=[\'"](\d+)[\'"]', html)
                    if pid_m:
                        product_id = pid_m.group(1)

                idx = html.find("var product =")
                if idx != -1:
                    line = html[idx + len("var product =") :].strip()
                    try:
                        decoder = json.JSONDecoder()
                        pdata, _ = decoder.raw_decode(line)
                        if not product_id:
                            product_id = pdata.get("ProductID")

                        items = pdata.get("Items") or []
                        chosen_sku = None
                        # Se sku_id foi indicado, valida se possui saldo de estoque
                        if sku_id:
                            for it in items:
                                if str(it.get("ProductID")) == str(sku_id) and float(it.get("StockBalance") or 0) > 0:
                                    chosen_sku = str(sku_id)
                                    break

                        # Se não tem saldo ou não foi informado, seleciona a primeira variante com estoque positivo
                        if not chosen_sku:
                            for it in items:
                                stock = float(it.get("StockBalance") or 0)
                                it_pid = str(it.get("ProductID"))
                                if stock > 0 and it_pid != str(product_id):
                                    chosen_sku = it_pid
                                    break

                        if chosen_sku:
                            sku_id = chosen_sku
                    except Exception as e:
                        logger.debug("[LinxShipping] Falha ao decodificar var product: %s", e)
        except Exception as exc:
            logger.debug("[LinxShipping] Erro ao carregar PDP para resolver SKU: %s", exc)

        return (str(product_id) if product_id else None, str(sku_id) if sku_id else None)

    async def calculate(
        self,
        product: SearchProductResult | dict[str, Any],
        zipcode: str,
        brand: Any,
    ) -> ShippingCalculation:
        try:
            cep = normalize_zipcode(zipcode)
        except ValueError:
            return ShippingCalculation(
                state=ShippingState.UNAVAILABLE_FOR_CEP,
                message="CEP invalido",
            )

        product_url = str(get_field(product, "url", "") or "")
        if not is_url_allowed_for_brand(product_url, brand):
            return ShippingCalculation(
                state=ShippingState.UNSUPPORTED,
                message="URL do produto nao pertence ao dominio da marca",
            )

        parsed_host = urlparse(product_url).netloc
        domain = parsed_host or brand_domain(brand)
        if not domain:
            return ShippingCalculation(
                state=ShippingState.UNSUPPORTED,
                message="Dominio da marca nao configurado",
            )

        try:
            async with self._open_session() as session:
                product_id, sku_id = await self._resolve_product_and_sku(session, product_url, product)
                if not product_id or not sku_id:
                    return ShippingCalculation(
                        state=ShippingState.UNAVAILABLE_FOR_CEP,
                        message="Produto Linx sem ProductID/SkuID com estoque",
                    )

                cep_formatted = f"{cep[:5]}-{cep[5:]}"
                params = {
                    "ProductID": product_id,
                    "SkuID": sku_id,
                    "PostalCode": cep_formatted,
                    "Template": "wd.product.deliveryfee.result",
                    "nocache": str(int(time.time() * 1000)),
                }
                headers = {
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": product_url,
                }
                url = f"https://{domain}/widget/product_deliveryfee"
                resp = await session.get(url, params=params, headers=headers)

                if resp.status_code != 200:
                    return ShippingCalculation(
                        state=ShippingState.TEMPORARY_FAILURE,
                        message=f"HTTP {resp.status_code} na consulta de frete Linx",
                    )

                html = resp.text
                if "Estoque insuficiente" in html or "Não foi possível encontrar" in html:
                    return ShippingCalculation(
                        state=ShippingState.UNAVAILABLE_FOR_CEP,
                        message="Entrega indisponivel para este CEP",
                    )

                # Parse das opções de frete
                soup = BeautifulSoup(html, "html.parser")
                options: list[ShippingInfo] = []
                items = soup.find_all(class_="item")

                for it in items:
                    text_all = it.get_text(" ", strip=True)
                    price = self._parse_currency(text_all)
                    days = self._parse_days(text_all)

                    # Extrai nome do serviço
                    # Tenta ler do strong ou das classes
                    service_name = "Entrega"
                    strong = it.find("strong")
                    if strong:
                        name_raw = strong.get_text(strip=True)
                        if name_raw:
                            service_name = name_raw
                    elif "expressa" in text_all.lower():
                        service_name = "Expressa"
                    elif "sedex" in text_all.lower():
                        service_name = "Sedex"
                    elif "retirar" in text_all.lower() or "retirada" in text_all.lower():
                        service_name = "Retirada em Loja"

                    if price is not None:
                        is_free = (price == 0.0)
                        options.append(
                            ShippingInfo(
                                price=price,
                                status="Grátis" if is_free else "Disponível",
                                estimated_delivery_days=days,
                                raw_text=text_all,
                                service_name=service_name,
                                service_id=service_name.lower().replace(" ", "_"),
                                estimate_display=f"{days} dia(s) úteis" if days is not None else None,
                                estimate_unit="bd" if days is not None else None,
                                is_free_shipping=is_free,
                            )
                        )

                if not options:
                    return ShippingCalculation(
                        state=ShippingState.UNAVAILABLE_FOR_CEP,
                        message="Entrega indisponivel para este CEP",
                    )

                return ShippingCalculation(
                    state=ShippingState.AVAILABLE,
                    shipping_options=sorted_shipping_options(options),
                )

        except Exception as exc:
            logger.warning(
                "[LinxShipping] quote failed brand=%s status=%s",
                get_field(brand, "brand_key", "unknown"),
                type(exc).__name__,
            )
            return ShippingCalculation(
                state=ShippingState.TEMPORARY_FAILURE,
                message="Frete temporariamente indisponivel",
            )
