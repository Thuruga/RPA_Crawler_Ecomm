"""
audit_live_site_matching.py
===========================
Verifica se todas as marcas e categorias cadastradas no banco:
1. Respondem com HTTP 200 no site real (sem 404 ou erro 5xx)
2. Não são páginas de erro ("página não encontrada", etc.)
3. Têm correspondência real com o catálogo do site
"""

import asyncio
import httpx
from bs4 import BeautifulSoup
from collections import defaultdict
from core.db import db

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

ERROR_PHRASES = [
    "página não encontrada",
    "pagina nao encontrada",
    "nenhum produto encontrado",
    "ops! não encontramos",
    "ops! nao encontramos",
    "não encontramos nenhum resultado",
    "404 not found",
    "page not found"
]

async def check_url(client: httpx.AsyncClient, domain: str, path: str, label: str, sem: asyncio.Semaphore):
    # Formatar URL completa
    if path.startswith("http"):
        url = path
    else:
        clean_path = path if path.startswith("/") else f"/{path}"
        url = f"https://{domain}{clean_path}"

    async with sem:
        try:
            r = await client.get(url, timeout=12.0)
            status = r.status_code
            final_url = str(r.url)
            
            # Checar se redirecionou para home ou página de erro
            is_redirected_to_home = False
            if r.status_code == 200:
                final_path = r.url.path.rstrip("/")
                original_path = httpx.URL(url).path.rstrip("/")
                if final_path in ("", "/") and original_path not in ("", "/"):
                    is_redirected_to_home = True

            # Checar texto para mensagens de 404 mascaradas com status 200
            html_lower = r.text[:15000].lower()
            has_error_phrase = any(phrase in html_lower for phrase in ERROR_PHRASES)

            # Avaliar saúde
            if status == 200 and not is_redirected_to_home and not has_error_phrase:
                health = "OK"
                issue = None
            elif status == 200 and is_redirected_to_home:
                health = "REDIRECT_HOME"
                issue = f"Redirecionou para a home ({final_url})"
            elif status == 200 and has_error_phrase:
                health = "SOFT_404"
                issue = "Página retornou 200 mas contém mensagem de erro/não encontrado"
            elif status == 404:
                health = "404_NOT_FOUND"
                issue = "HTTP 404 Not Found"
            else:
                health = f"HTTP_{status}"
                issue = f"HTTP status {status}"

            return {
                "label": label,
                "path": path,
                "url": url,
                "final_url": final_url,
                "status": status,
                "health": health,
                "issue": issue,
            }

        except httpx.TimeoutException:
            return {
                "label": label,
                "path": path,
                "url": url,
                "status": 0,
                "health": "TIMEOUT",
                "issue": "Timeout ao conectar (>12s)",
            }
        except Exception as e:
            return {
                "label": label,
                "path": path,
                "url": url,
                "status": 0,
                "health": "ERROR",
                "issue": str(e),
            }


async def audit_brand_categories(brand_key: str, brand_name: str, domain: str, mappings: list):
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, verify=False) as client:
        tasks = [
            check_url(client, domain, m["source_path"], m["label"], sem)
            for m in mappings
        ]
        results = await asyncio.gather(*tasks)

    ok_count = sum(1 for r in results if r["health"] == "OK")
    issues = [r for r in results if r["health"] != "OK"]
    
    return {
        "brand_key": brand_key,
        "brand_name": brand_name,
        "domain": domain,
        "total": len(mappings),
        "ok_count": ok_count,
        "issues": issues,
    }


async def main():
    await db.connect()
    
    rows = await db.query_raw("""
        SELECT b.brand_key, b.brand_name, b.domain, bcm.canonical_slug, bcm.label, bcm.source_path
        FROM brand_category_mappings bcm
        JOIN brands b ON b.id = bcm.brand_id
        WHERE b.is_active = true AND b.brand_key NOT IN ('amazon', 'mercado_livre', 'netshoes', 'privalia', 'dafiti')
        ORDER BY b.brand_name, bcm.label
    """)
    
    by_brand = defaultdict(list)
    brand_meta = {}
    for r in rows:
        bk = r["brand_key"]
        by_brand[bk].append(r)
        brand_meta[bk] = (r["brand_name"], r["domain"])

    print(f"Iniciando auditoria live de {len(by_brand)} marcas e {len(rows)} URLs de categorias...\n")
    
    total_checked = 0
    total_ok = 0
    all_issues = []

    for bk, mappings in sorted(by_brand.items(), key=lambda x: brand_meta[x[0]][0]):
        bname, domain = brand_meta[bk]
        res = await audit_brand_categories(bk, bname, domain, mappings)
        total_checked += res["total"]
        total_ok += res["ok_count"]
        
        pct = (res["ok_count"] / res["total"] * 100) if res["total"] > 0 else 0
        status_sym = "✅" if len(res["issues"]) == 0 else ("⚠️" if pct > 75 else "❌")
        
        print(f"{status_sym} [{bname}] ({bk}): {res['ok_count']}/{res['total']} URLs OK ({pct:.1f}%)")
        if res["issues"]:
            for iss in res["issues"]:
                print(f"     -> [{iss['health']}] '{iss['label']}' -> {iss['path']} | Motivo: {iss['issue']}")
                all_issues.append((bk, bname, iss))

    print("\n" + "="*70)
    print(f"RESULTADO GERAL: {total_ok}/{total_checked} URLs operacionais ({total_ok/total_checked*100:.1f}%)")
    print(f"Total de divergências/issues encontradas: {len(all_issues)}")
    print("="*70)

    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
