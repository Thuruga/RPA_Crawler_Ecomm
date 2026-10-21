"""Spike observável para extrair banners desktop das marcas cadastradas.

Uso, a partir da raiz do projeto:
    python testes/extrair_banners.py
    python testes/extrair_banners.py --brands aramis ricardoalmeida
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
BRANDS_FILE = BACKEND / "data" / "brands.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "saida"

sys.path.insert(0, str(BACKEND))
from core.browser_manager import BrowserManager  # noqa: E402


VIEWPORT = {"width": 1366, "height": 768}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


COLLECT_BANNERS_JS = r"""
() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const minWidth = vw * 0.60;
  const minHeight = Math.max(180, vh * 0.23);
  const results = [];

  const absoluteUrl = (value) => {
    if (!value || value.startsWith('data:') || value.startsWith('blob:')) return '';
    try { return new URL(value, location.href).href; } catch { return ''; }
  };

  const largestFromSrcset = (value) => {
    if (!value) return '';
    const entries = value.split(',').map((part) => {
      const bits = part.trim().split(/\s+/);
      const width = Number((bits[1] || '').replace(/[^0-9.]/g, '')) || 0;
      return { url: bits[0] || '', width };
    });
    entries.sort((a, b) => b.width - a.width);
    return entries[0]?.url || '';
  };

  const rectData = (rect) => ({
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
    bottom: Math.round(rect.bottom),
  });

  const qualifies = (rect) => (
    rect.width >= minWidth &&
    rect.height >= minHeight &&
    rect.top < vh &&
    rect.bottom > 0
  );

  const visibility = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return Boolean(
      rect.width && rect.height &&
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0
    );
  };

  const closestLink = (element) => absoluteUrl(element.closest('a[href]')?.href || '');

  document.querySelectorAll('img').forEach((image, domOrder) => {
    const rect = image.getBoundingClientRect();
    if (!qualifies(rect)) return;

    const lazySrc = image.getAttribute('data-src') ||
      image.getAttribute('data-lazy-src') ||
      image.getAttribute('data-original') || '';
    const lazySrcset = largestFromSrcset(
      image.getAttribute('data-srcset') || image.getAttribute('data-lazy-srcset') || ''
    );
    const regularSrcset = largestFromSrcset(image.getAttribute('srcset') || '');
    const current = image.currentSrc || '';
    const src = image.getAttribute('src') || '';
    const chosen = [lazySrcset, lazySrc, current, regularSrcset, src]
      .map(absoluteUrl)
      .find(Boolean) || '';
    if (!chosen) return;

    results.push({
      kind: 'img',
      url: chosen,
      alt: image.getAttribute('alt') || '',
      link_url: closestLink(image),
      visible_on_capture: visibility(image),
      dom_order: domOrder,
      natural_width: image.naturalWidth || null,
      natural_height: image.naturalHeight || null,
      ...rectData(rect),
    });
  });

  const backgroundSelectors = [
    '[style*="background"]', '[class*="banner"]', '[class*="Banner"]',
    '[class*="hero"]', '[class*="Hero"]', '[class*="slide"]',
    '[class*="Slide"]', '[class*="carousel"]', '[class*="Carousel"]',
    '[class*="swiper"]', '[class*="Swiper"]'
  ].join(',');

  document.querySelectorAll(backgroundSelectors).forEach((element, domOrder) => {
    const rect = element.getBoundingClientRect();
    if (!qualifies(rect)) return;

    const styles = [getComputedStyle(element), getComputedStyle(element, '::before'), getComputedStyle(element, '::after')];
    styles.forEach((style, styleIndex) => {
      const matches = [...(style.backgroundImage || '').matchAll(/url\(["']?(.*?)["']?\)/g)];
      matches.forEach((match) => {
        const url = absoluteUrl(match[1]);
        if (!url) return;
        results.push({
          kind: styleIndex === 0 ? 'background' : `background-pseudo-${styleIndex}`,
          url,
          alt: element.getAttribute('aria-label') || '',
          link_url: closestLink(element),
          visible_on_capture: visibility(element),
          dom_order: 100000 + domOrder,
          natural_width: null,
          natural_height: null,
          ...rectData(rect),
        });
      });
    });
  });

  const deduplicated = new Map();
  results.forEach((item) => {
    if (!deduplicated.has(item.url)) deduplicated.set(item.url, item);
  });

  const sorted = [...deduplicated.values()].sort((a, b) => {
    const y = a.y - b.y;
    return y || (b.width * b.height) - (a.width * a.height) || a.dom_order - b.dom_order;
  });
  if (!sorted.length) return [];

  // "Banner da primeira tela" significa o primeiro grande bloco visual, não
  // imagens da seção seguinte que aparecem poucos pixels no rodapé da viewport.
  const heroTop = sorted[0].y;
  return sorted.filter((item) => item.y <= heroTop + 100);
}
"""


CLICK_NEXT_JS = r"""
() => {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const pattern = /(next|pr[oó]xim[oa]|seguinte|avan[cç]ar|arrow.?right|chevron.?right)/i;
  const selectors = [
    'button', '[role="button"]', '.swiper-button-next', '.slick-next',
    '[class*="carousel"][class*="next"]', '[class*="slider"][class*="next"]'
  ].join(',');

  for (const element of document.querySelectorAll(selectors)) {
    const descriptor = [
      element.getAttribute('aria-label'), element.getAttribute('title'),
      element.className, element.textContent
    ].filter(Boolean).join(' ');
    if (!pattern.test(descriptor) || element.disabled) continue;

    const rect = element.getBoundingClientRect();
    if (rect.top >= vh || rect.bottom <= 0 || rect.right <= 0 || rect.left >= vw) continue;

    let container = element.parentElement;
    let belongsToHero = false;
    while (container && container !== document.documentElement) {
      const box = container.getBoundingClientRect();
      if (box.width >= vw * 0.60 && box.height >= 180 && box.top < vh && box.bottom > 0) {
        belongsToHero = true;
        break;
      }
      container = container.parentElement;
    }
    if (!belongsToHero) continue;

    element.click();
    return descriptor.slice(0, 160);
  }
  return null;
}
"""


CAROUSEL_INFO_JS = r"""
() => {
  let declaredSlides = null;
  for (const element of document.querySelectorAll('[aria-label]')) {
    const match = (element.getAttribute('aria-label') || '').match(/^Dot\s+\d+\s+of\s+(\d+)$/i);
    if (match) declaredSlides = Math.max(declaredSlides || 0, Number(match[1]));
  }

  const videos = [];
  for (const video of document.querySelectorAll('video')) {
    const rect = video.getBoundingClientRect();
    if (rect.width < innerWidth * 0.60 || rect.height < 180 || rect.top >= innerHeight || rect.bottom <= 0) continue;
    const source = video.currentSrc || video.src || video.querySelector('source[src]')?.src || '';
    if (source && !videos.includes(source)) videos.push(source);
  }
  return { declared_slides: declaredSlides, video_slides: videos };
}
"""


DISMISS_OVERLAYS_JS = r"""
() => {
  const acceptPattern = /^(aceitar( todos)?|aceito|permitir todos|concordo|ok,? entendi|continuar)$/i;
  const dialogs = [...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')];
  const fixedContainers = [...document.querySelectorAll('div, section')].filter((element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.position === 'fixed' && rect.width > innerWidth * 0.5 && rect.height > 50;
  });
  const scopes = [...new Set([...dialogs, ...fixedContainers])];
  let clicks = 0;

  for (const scope of scopes) {
    const buttons = scope.querySelectorAll('button, [role="button"], input[type="button"]');
    for (const button of buttons) {
      const label = (button.textContent || button.value || button.getAttribute('aria-label') || '').trim();
      if (acceptPattern.test(label)) {
        button.click();
        clicks += 1;
        break;
      }
    }
  }
  return clicks;
}
"""


CONTENT_TYPE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extrai banners desktop das marcas cadastradas")
    parser.add_argument("--brands", nargs="*", help="Chaves específicas; por padrão usa todas as marcas ativas")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Diretório de saída")
    parser.add_argument("--timeout", type=int, default=30_000, help="Timeout de navegação em milissegundos")
    parser.add_argument("--settle-ms", type=int, default=4_000, help="Espera após carregar a home")
    parser.add_argument("--max-slides", type=int, default=12, help="Máximo de avanços tentados no carrossel")
    parser.add_argument("--show-browser", action="store_true", help="Exibe o Chromium durante a coleta")
    parser.add_argument("--no-download", action="store_true", help="Não baixa os arquivos originais")
    return parser.parse_args()


def load_brands(selected: list[str] | None) -> list[dict[str, Any]]:
    raw = json.loads(BRANDS_FILE.read_text(encoding="utf-8"))
    requested = {item.lower() for item in selected or []}
    unknown = requested.difference(raw)
    if unknown:
        raise SystemExit(f"Marcas não cadastradas: {', '.join(sorted(unknown))}")

    brands = []
    for key, data in raw.items():
        if requested and key not in requested:
            continue
        if not requested and not data.get("is_active", True):
            continue
        brands.append({"key": key, **data})
    return brands


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return cleaned or "arquivo"


def extension_for(url: str, content_type: str) -> str:
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type in CONTENT_TYPE_EXTENSIONS:
        return CONTENT_TYPE_EXTENSIONS[normalized_type]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".bin"


def download_banner(context: Any, banner: dict[str, Any], banners_dir: Path, index: int) -> None:
    try:
        response = context.request.get(
            banner["url"],
            headers={"Referer": banner.get("page_url", "")},
            timeout=30_000,
        )
        banner["download_status"] = response.status
        if not response.ok:
            banner["download_error"] = f"HTTP {response.status}"
            return

        body = response.body()
        content_type = response.headers.get("content-type", "")
        digest = hashlib.sha256(body).hexdigest()
        extension = extension_for(banner["url"], content_type)
        filename = f"banner_{index:02d}_{digest[:10]}{extension}"
        target = banners_dir / safe_name(filename)
        target.write_bytes(body)

        banner["sha256"] = digest
        banner["content_type"] = content_type
        banner["bytes"] = len(body)
        banner["local_file"] = target.relative_to(banners_dir.parent.parent).as_posix()
    except Exception as exc:  # noqa: BLE001 - o relatório precisa preservar falhas por site
        banner["download_error"] = f"{type(exc).__name__}: {exc}"


def extract_brand(browser: Any, brand: dict[str, Any], output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    key = brand["key"]
    brand_dir = output_dir / key
    banners_dir = brand_dir / "banners"
    if brand_dir.exists():
        resolved_brand_dir = brand_dir.resolve()
        if not resolved_brand_dir.is_relative_to(output_dir.resolve()):
            raise RuntimeError(f"Diretório de marca fora da saída: {resolved_brand_dir}")
        shutil.rmtree(resolved_brand_dir)
    banners_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://{brand['domain'].strip('/')}"

    result: dict[str, Any] = {
        "brand_key": key,
        "brand_name": brand.get("brand_name", key),
        "engine": brand.get("engine"),
        "requested_url": url,
        "status": "pending",
        "banners": [],
    }

    context = browser.new_context(
        viewport=VIEWPORT,
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        user_agent=USER_AGENT,
        java_script_enabled=True,
        extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        "Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });"
        "window.chrome = { runtime: {} };"
    )

    try:
        started = time.perf_counter()
        response = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout)
        page.wait_for_timeout(args.settle_ms)
        page.evaluate(DISMISS_OVERLAYS_JS)
        page.wait_for_timeout(500)

        result.update(
            {
                "http_status": response.status if response else None,
                "final_url": page.url,
                "page_title": page.title(),
                "viewport": VIEWPORT,
            }
        )
        page.screenshot(path=str(brand_dir / "viewport.png"), full_page=False)
        result["viewport_file"] = f"{key}/viewport.png"

        collected: dict[str, dict[str, Any]] = {}
        no_new_rounds = 0
        initial_carousel_info = page.evaluate(CAROUSEL_INFO_JS)
        declared_slides = initial_carousel_info.get("declared_slides")
        for attempt in range(args.max_slides + 1):
            for item in page.evaluate(COLLECT_BANNERS_JS):
                if item["url"] not in collected:
                    item["discovered_on_attempt"] = attempt
                    collected[item["url"]] = item

            if attempt >= args.max_slides:
                break
            previous_count = len(collected)
            clicked = page.evaluate(CLICK_NEXT_JS)
            if not clicked:
                break
            page.wait_for_timeout(800)
            for item in page.evaluate(COLLECT_BANNERS_JS):
                if item["url"] not in collected:
                    item["discovered_on_attempt"] = attempt + 1
                    collected[item["url"]] = item
            if len(collected) == previous_count:
                no_new_rounds += 1
                # Um carrossel pode ter vídeos entre dois banners de imagem. Quando
                # há indicadores, percorremos a quantidade declarada antes de parar.
                if declared_slides is None and no_new_rounds >= 2:
                    break
            else:
                no_new_rounds = 0

            if declared_slides is not None and attempt + 1 >= declared_slides - 1:
                break

        banners = list(collected.values())
        carousel_info = page.evaluate(CAROUSEL_INFO_JS)
        result["carousel_declared_slides"] = carousel_info.get("declared_slides")
        result["video_slides"] = carousel_info.get("video_slides", [])
        for index, banner in enumerate(banners, start=1):
            banner["page_url"] = page.url
            if not args.no_download:
                download_banner(context, banner, banners_dir, index)

        result["banners"] = banners
        result["status"] = "ok" if banners else "sem_banners"
        result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    except Exception as exc:  # noqa: BLE001 - cada site deve falhar isoladamente
        result["status"] = "erro"
        result["error"] = f"{type(exc).__name__}: {exc}"
        try:
            page.screenshot(path=str(brand_dir / "viewport_erro.png"), full_page=False)
            result["viewport_file"] = f"{key}/viewport_erro.png"
        except Exception:
            pass
    finally:
        page.close()
        context.close()

    (brand_dir / "resultado.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def write_csv(results: list[dict[str, Any]], output_dir: Path) -> None:
    columns = [
        "brand_key", "brand_name", "engine", "status", "banner_index", "kind",
        "url", "local_file", "alt", "link_url", "visible_on_capture", "width",
        "height", "y", "natural_width", "natural_height", "content_type", "bytes",
        "sha256", "download_status", "download_error",
    ]
    with (output_dir / "resumo.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            if not result["banners"]:
                writer.writerow({key: result.get(key, "") for key in columns})
                continue
            for index, banner in enumerate(result["banners"], start=1):
                writer.writerow(
                    {
                        "brand_key": result["brand_key"],
                        "brand_name": result["brand_name"],
                        "engine": result["engine"],
                        "status": result["status"],
                        "banner_index": index,
                        **banner,
                    }
                )


def write_gallery(results: list[dict[str, Any]], output_dir: Path, generated_at: str) -> None:
    sections = []
    for result in results:
        banner_cards = []
        for index, banner in enumerate(result["banners"], start=1):
            local_file = banner.get("local_file")
            image = (
                f'<a href="{html.escape(local_file)}"><img loading="lazy" src="{html.escape(local_file)}" alt="Banner {index}"></a>'
                if local_file else '<div class="missing">Imagem não baixada</div>'
            )
            banner_cards.append(
                f"""
                <article class="banner-card">
                  {image}
                  <h3>Slide {index}</h3>
                  <p><strong>{html.escape(banner.get('alt') or '(sem texto alternativo)')}</strong></p>
                  <p>{banner.get('width')}×{banner.get('height')} px · {html.escape(banner.get('kind', ''))}</p>
                  <p>Visível na captura: {'sim' if banner.get('visible_on_capture') else 'não'}</p>
                  <a class="asset-link" href="{html.escape(banner['url'])}" target="_blank" rel="noreferrer">Abrir imagem original</a>
                </article>
                """
            )

        viewport = result.get("viewport_file")
        viewport_html = (
            f'<a href="{html.escape(viewport)}"><img class="viewport" src="{html.escape(viewport)}" alt="Primeira tela"></a>'
            if viewport else "<p>Captura indisponível.</p>"
        )
        error = f'<p class="error">{html.escape(result.get("error", ""))}</p>' if result.get("error") else ""
        sections.append(
            f"""
            <section>
              <header>
                <div><h2>{html.escape(result['brand_name'])}</h2><p>{html.escape(result['brand_key'])} · {html.escape(str(result.get('engine') or ''))}</p></div>
                <span class="status {html.escape(result['status'])}">{html.escape(result['status'])} · {len(result['banners'])} imagem(ns) · {len(result.get('video_slides', []))} vídeo(s)</span>
              </header>
              {error}
              <p>Slides declarados pelo site: {result.get('carousel_declared_slides') or 'não informado'}</p>
              <details>
                <summary>Ver captura da primeira tela</summary>
                {viewport_html}
              </details>
              <div class="grid">{''.join(banner_cards) or '<p>Nenhum candidato detectado.</p>'}</div>
            </section>
            """
        )

    document = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Teste de extração de banners</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; color: #172033; background: #f4f6fa; }}
    body {{ margin: 0; padding: 32px; }}
    main {{ max-width: 1440px; margin: auto; }}
    h1, h2, h3, p {{ margin-top: 0; }}
    section {{ background: white; border: 1px solid #dfe4ee; border-radius: 14px; padding: 22px; margin: 22px 0; box-shadow: 0 8px 24px rgba(23,32,51,.06); }}
    section > header {{ display: flex; justify-content: space-between; gap: 20px; align-items: start; }}
    .status {{ border-radius: 999px; padding: 7px 11px; background: #eef2f8; font-size: 13px; white-space: nowrap; }}
    .status.ok {{ background: #dff7e9; color: #17683a; }}
    .status.erro {{ background: #ffe3e3; color: #a12626; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 18px; margin-top: 18px; }}
    .banner-card {{ border: 1px solid #e2e6ef; border-radius: 10px; overflow: hidden; padding-bottom: 16px; }}
    .banner-card img {{ display: block; width: 100%; aspect-ratio: 16/7; object-fit: cover; background: #edf0f5; }}
    .banner-card h3, .banner-card p, .asset-link {{ margin-left: 14px; margin-right: 14px; }}
    .banner-card h3 {{ margin-top: 14px; }}
    .banner-card p {{ color: #536078; font-size: 14px; }}
    .viewport {{ width: min(100%, 1000px); margin-top: 14px; border: 1px solid #dfe4ee; }}
    details {{ margin-top: 8px; }}
    summary {{ cursor: pointer; color: #405172; }}
    .error {{ color: #a12626; white-space: pre-wrap; }}
    .missing {{ aspect-ratio: 16/7; display: grid; place-items: center; background: #edf0f5; color: #69758b; }}
    a {{ color: #3758b8; }}
  </style>
</head>
<body><main>
  <h1>Teste de extração de banners desktop</h1>
  <p>Gerado em {html.escape(generated_at)} · viewport 1366×768</p>
  {''.join(sections)}
</main></body></html>
"""
    (output_dir / "index.html").write_text(document, encoding="utf-8")


def main() -> int:
    args = parse_args()
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    brands = load_brands(args.brands)
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    print(f"Extraindo banners desktop de {len(brands)} marca(s) para {output_dir}")
    results = []
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not args.show_browser,
            args=BrowserManager.CHROMIUM_ARGS,
        )
        try:
            for position, brand in enumerate(brands, start=1):
                print(f"[{position}/{len(brands)}] {brand.get('brand_name', brand['key'])}...", flush=True)
                result = extract_brand(browser, brand, output_dir, args)
                print(
                    f"    {result['status']}: {len(result['banners'])} banner(s)"
                    + (f" — {result['error']}" if result.get("error") else "")
                )
                results.append(result)
        finally:
            browser.close()

    payload = {
        "generated_at": generated_at,
        "viewport": VIEWPORT,
        "brands_file": str(BRANDS_FILE),
        "results": results,
    }
    (output_dir / "resultado.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(results, output_dir)
    write_gallery(results, output_dir, generated_at)

    ok = sum(result["status"] == "ok" for result in results)
    total_banners = sum(len(result["banners"]) for result in results)
    print(f"Concluído: {ok}/{len(results)} sites com banners; {total_banners} arquivos detectados.")
    print(f"Abra a galeria: {output_dir / 'index.html'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
