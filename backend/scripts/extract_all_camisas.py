"""
Script de Extração Exaustiva: Camisas de 10 Marcas
Marcas: Reserva, Oficina Reserva, Calvin Klein, Boss, Tommy Hilfiger, Foxton, Richards, Levi's, Lacoste e Brooksfield.
Atributos extraídos obrigatoriamente: Faixa de Preço, Tamanho, Cor e Tecido/Composição.
"""

import os
import sys
from pathlib import Path

# Adiciona o diretório backend ao sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional
import pandas as pd

from services.brand_service import brand_service
from services.engines.factory import EngineFactory
from services.engines.vtex_engine import VTEXEngine
from services.engines.wake_engine import WakeEngine
from services.engines.algolia_engine import AlgoliaEngine
from services.engines.linx_engine import LinxEngine

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ExtractAllCamisas")

# Mapeamento de tarefas de extração por marca
BRAND_CONFIGS = [
    {
        "brand_key": "reserva",
        "brand_name": "Reserva",
        "engine_type": "vtex",
        "urls": ["https://www.usereserva.com/reserva/masculino/camisas"],
    },
    {
        "brand_key": "ofcreserva",
        "brand_name": "Oficina Reserva",
        "engine_type": "vtex",
        "urls": ["https://secure.oficinareserva.com/roupas/camisas"],
    },
    {
        "brand_key": "calvinklein",
        "brand_name": "Calvin Klein",
        "engine_type": "vtex",
        "urls": ["https://www.calvinklein.com.br/masculino/roupas/camisas"],
    },
    {
        "brand_key": "hugoboss",
        "brand_name": "Hugo Boss",
        "engine_type": "vtex",
        "urls": [
            "https://www.hugoboss.com.br/boss/masculino/roupas/camisas",
            "https://www.hugoboss.com.br/hugo/masculino/roupas/camisas",
        ],
    },
    {
        "brand_key": "tommy",
        "brand_name": "Tommy Hilfiger",
        "engine_type": "wake",
        "urls": ["https://br.tommy.com/roupas/camisas"],
    },
    {
        "brand_key": "foxton",
        "brand_name": "Foxton",
        "engine_type": "vtex",
        "urls": ["https://www.foxtonbrasil.com.br/roupas/camisas"],
    },
    {
        "brand_key": "richards",
        "brand_name": "Richards",
        "engine_type": "wake",
        "urls": ["https://www.richards.com.br/camisas"],
    },
    {
        "brand_key": "levis",
        "brand_name": "Levi's",
        "engine_type": "vtex",
        "urls": ["https://www.levi.com.br/masculino/roupas/camisas"],
    },
    {
        "brand_key": "lacoste",
        "brand_name": "Lacoste",
        "engine_type": "algolia",
        "urls": ["https://www.lacoste.com.br/camisas"],
    },
    {
        "brand_key": "brooksfield",
        "brand_name": "Brooksfield",
        "engine_type": "linx",
        "urls": ["https://www.brooksfield.com.br/adulto/camisas"],
    },
]


def format_currency(value: Optional[float]) -> str:
    """Formata valor float como moeda BRL (R$ 1.234,56)."""
    if value is None or pd.isna(value):
        return ""
    try:
        val = float(value)
        return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


def build_price_range_str(original: Optional[float], current: Optional[float]) -> str:
    """Gera string descritiva da faixa de preço e desconto."""
    if current is None and original is None:
        return "Sob consulta"
    
    orig_val = original if original is not None else current
    curr_val = current if current is not None else original
    
    if orig_val and curr_val and orig_val > curr_val:
        pct = round((1 - (curr_val / orig_val)) * 100)
        return f"De {format_currency(orig_val)} por {format_currency(curr_val)} (-{pct}%)"
    else:
        return format_currency(curr_val)


def clean_list_str(items: Any) -> str:
    """Converte lista de strings para texto separado por vírgula."""
    if not items:
        return ""
    if isinstance(items, list):
        clean = [str(i).strip() for i in items if str(i).strip()]
        return ", ".join(dict.fromkeys(clean))  # preserva ordem e deduplica
    return str(items).strip()


def normalize_record(raw: Dict[str, Any], brand_display: str) -> Dict[str, Any]:
    """Normaliza o produto extraído no esquema canônico solicitado pelo usuário."""
    # Preços
    price_full = raw.get("price_full")
    price_discount = raw.get("price_discount")
    
    if price_discount is not None and price_full is not None and price_discount > 0 and price_full > 0:
        if price_discount < price_full:
            p_orig = float(price_full)
            p_curr = float(price_discount)
        else:
            p_orig = float(price_full)
            p_curr = float(price_full)
    elif price_full is not None:
        p_orig = float(price_full)
        p_curr = float(price_full)
    elif price_discount is not None:
        p_orig = float(price_discount)
        p_curr = float(price_discount)
    else:
        p_orig = None
        p_curr = None

    desconto_pct = ""
    if p_orig and p_curr and p_orig > p_curr:
        desconto_pct = f"{round((1 - (p_curr / p_orig)) * 100)}%"
    elif p_orig and p_curr:
        desconto_pct = "0%"

    faixa_preco = build_price_range_str(p_orig, p_curr)

    # Cores
    cores = raw.get("available_colors") or raw.get("raw_colors") or []
    cores_str = clean_list_str(cores)

    # Tamanhos
    tamanhos = raw.get("available_sizes") or raw.get("raw_sizes") or []
    tamanhos_str = clean_list_str(tamanhos)

    grade = raw.get("size_grid") or raw.get("raw_size_grid") or tamanhos
    grade_str = clean_list_str(grade)

    # Composição / Tecido
    comp = raw.get("composition") or ""
    if not comp:
        # Tenta extrair do título ou descrição
        t_lower = (raw.get("raw_title") or "").lower()
        d_lower = (raw.get("raw_description") or "").lower()
        if "linho" in t_lower or "linho" in d_lower:
            comp = "Linho"
        elif "algod" in t_lower or "algod" in d_lower or "oxford" in t_lower:
            comp = "100% Algodão"
        elif "jeans" in t_lower:
            comp = "Jeans (100% Algodão)"
        elif "flanela" in t_lower:
            comp = "Flanela de Algodão"

    # URL
    url = raw.get("url") or ""
    # Imagem
    img = raw.get("image_url") or ""

    return {
        "marca": brand_display,
        "produto": (raw.get("raw_title") or "").strip(),
        "faixa_preco": faixa_preco,
        "preco_original": p_orig,
        "preco_atual": p_curr,
        "desconto_percentual": desconto_pct,
        "tamanhos_disponiveis": tamanhos_str,
        "grade_tamanhos": grade_str,
        "cores_disponiveis": cores_str,
        "tecido_composicao": comp.strip() if comp else "Não especificado",
        "url_produto": url,
        "imagem_url": img,
        "codigo_produto": raw.get("product_code") or "",
    }


async def scrape_brand(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrai todas as camisas de uma marca específica."""
    b_key = config["brand_key"]
    b_name = config["brand_name"]
    urls = config["urls"]

    logger.info(f"\n{'='*60}\n>>> INICIANDO EXTRAÇÃO: {b_name} ({b_key})\n{'='*60}")
    engine = EngineFactory.get_engine(b_key)
    
    brand_products: List[Dict[str, Any]] = []
    seen_urls = set()

    for cat_url in urls:
        logger.info(f"[{b_name}] Varrendo categoria: {cat_url}")
        count_url = 0
        try:
            async for item in engine.run_bulk_scrape(
                category_url=cat_url,
                log_callback=lambda msg: logger.info(f"[{b_name}] {msg}" if isinstance(msg, str) else f"[{b_name}] {msg.get('message', '')}"),
            ):
                item_dict = dict(item) if isinstance(item, dict) else (item.model_dump() if hasattr(item, "model_dump") else vars(item))
                prod_url = item_dict.get("url")
                
                # Deduplicação por URL
                if prod_url and prod_url in seen_urls:
                    continue
                if prod_url:
                    seen_urls.add(prod_url)

                norm = normalize_record(item_dict, b_name)
                brand_products.append(norm)
                count_url += 1

                if count_url % 25 == 0:
                    logger.info(f"[{b_name}] Extraídos {count_url} produtos até o momento...")

        except Exception as e:
            logger.error(f"[{b_name}] Erro durante a varredura de {cat_url}: {e}", exc_info=True)

        logger.info(f"[{b_name}] Concluído {cat_url}: {count_url} camisas encontradas.")

    logger.info(f"[{b_name}] TOTAL FINAL: {len(brand_products)} camisas extraídas.")
    return brand_products


async def main():
    start_time = time.time()
    logger.info("==================================================================")
    logger.info("  INICIANDO EXTRAÇÃO EXAUSTIVA DE CAMISAS - 10 MARCAS")
    logger.info("  Reserva, Oficina, Calvin Klein, Boss, Tommy, Foxton, Richards,")
    logger.info("  Levi's, Lacoste e Brooksfield")
    logger.info("==================================================================")

    all_camisas: List[Dict[str, Any]] = []
    brand_stats: Dict[str, Dict[str, Any]] = {}

    # Executa cada marca sequencialmente ou com leve paralelismo para não sobrecarregar
    for config in BRAND_CONFIGS:
        b_name = config["brand_name"]
        t0 = time.time()
        prods = await scrape_brand(config)
        elapsed = time.time() - t0

        all_camisas.extend(prods)

        # Estatísticas da marca
        with_comp = sum(1 for p in prods if p["tecido_composicao"] and p["tecido_composicao"] != "Não especificado")
        with_size = sum(1 for p in prods if p["tamanhos_disponiveis"])
        with_color = sum(1 for p in prods if p["cores_disponiveis"])
        avg_price = (sum(p["preco_atual"] for p in prods if p["preco_atual"]) / len(prods)) if prods else 0

        brand_stats[b_name] = {
            "total": len(prods),
            "com_tecido": with_comp,
            "com_tecido_pct": f"{(with_comp / len(prods) * 100):.1f}%" if prods else "0%",
            "com_tamanho": with_size,
            "com_tamanho_pct": f"{(with_size / len(prods) * 100):.1f}%" if prods else "0%",
            "com_cor": with_color,
            "com_cor_pct": f"{(with_color / len(prods) * 100):.1f}%" if prods else "0%",
            "preco_medio": format_currency(avg_price),
            "tempo_segundos": f"{elapsed:.1f}s",
        }

    total_time = time.time() - start_time
    logger.info("\n==================================================================")
    logger.info("  CONSOLIDAÇÃO E SALVAMENTO DOS DADOS")
    logger.info("==================================================================")

    if not all_camisas:
        logger.error("Nenhum produto foi extraído!")
        return

    df = pd.DataFrame(all_camisas)

    # Ordenar por marca e preço
    sort_cols = [c for c in ["marca", "preco_atual"] if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=[True, True])

    excel_path = BASE_DIR / "dados_camisas_10_marcas.xlsx"
    json_path = BASE_DIR / "dados_camisas_10_marcas.json"

    # Salva em Excel com formatação
    logger.info(f"Gravando arquivo Excel em {excel_path}...")
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Camisas Concorrentes", index=False)

    # Salva em JSON
    logger.info(f"Gravando arquivo JSON em {json_path}...")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_camisas, f, ensure_ascii=False, indent=2)

    # Imprime tabela resumo
    print("\n" + "=" * 95)
    print("                      RESUMO DA EXTRAÇÃO POR MARCA")
    print("=" * 95)
    header = f"{'Marca':<18} | {'Total':<6} | {'Tecido/Comp':<12} | {'Tamanhos':<10} | {'Cores':<10} | {'Preço Médio':<12} | {'Tempo':<8}"
    print(header)
    print("-" * 95)
    for b_name, s in brand_stats.items():
        row = f"{b_name:<18} | {s['total']:<6} | {s['com_tecido_pct']:<12} | {s['com_tamanho_pct']:<10} | {s['com_cor_pct']:<10} | {s['preco_medio']:<12} | {s['tempo_segundos']:<8}"
        print(row)
    print("-" * 95)
    total_total = len(all_camisas)
    total_comp = sum(s["com_tecido"] for s in brand_stats.values())
    total_size = sum(s["com_tamanho"] for s in brand_stats.values())
    total_color = sum(s["com_cor"] for s in brand_stats.values())
    print(f"{'TOTAL':<18} | {total_total:<6} | {total_comp/total_total*100:.1f}%        | {total_size/total_total*100:.1f}%      | {total_color/total_total*100:.1f}%      | {'-':<12} | {total_time:.1f}s")
    print("=" * 95)
    print(f"\nArquivos salvos com sucesso:")
    print(f"  Excel: {excel_path}")
    print(f"  JSON:  {json_path}")
    print("==================================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
