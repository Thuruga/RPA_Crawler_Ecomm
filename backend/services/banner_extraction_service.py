"""Production Playwright collector promoted from ``testes/extrair_banners.py``."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import Session

from core.banner_models import BannerCandidate, BannerVideoSlide, StoredBannerAsset
from services.banner_storage_service import BannerStorageService, banner_storage_service, friendly_banner_filename


VIEWPORT = {"width": 1366, "height": 768}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
MAX_SLIDES = 16
NAVIGATION_TIMEOUT_MS = 30_000
SETTLE_MS = 4_000
SLIDE_SETTLE_MS = 1_200
ASSET_RETRIES = 3
KNOWN_IMAGE_MIME = {
    "image/avif", "image/gif", "image/jpeg", "image/png", "image/svg+xml", "image/webp",
}
MIME_BY_SUFFIX = {
    ".avif": "image/avif", ".gif": "image/gif", ".jpeg": "image/jpeg", ".jpg": "image/jpeg",
    ".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
}
VIDEO_SUFFIXES = {".m4v", ".mov", ".mp4", ".webm"}
SERVER_SIDE_BANNER_BRANDS = frozenset({"zara", "lacoste"})
ZARA_MARKETING_URL = "https://www.zara.com/itxrest/2/marketing/store/21701/spot"
ZARA_HOME_SPOT = "ESpot_W2025-HOME"
LACOSTE_HOME_URL = "https://www.lacoste.com/br/masculino.html"
DIRECT_TIMEOUT_SECONDS = 30
DIRECT_DOCUMENT_BYTES = 8 * 1024 * 1024
ZARA_ASSET_HOSTS = frozenset({"static.zara.net"})
LACOSTE_ASSET_HOSTS = frozenset({"imagesa1.lacoste.com", "static1.lacoste.com"})


def is_video_url(url: str) -> bool:
    """Identify video assets before the image downloader relies on MIME headers."""
    return urlparse(url).path.lower().endswith(tuple(VIDEO_SUFFIXES))


COLLECT_BANNERS_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  const absolute = (value) => {
    try { return (!value || /^(data:|blob:)/i.test(value)) ? '' : new URL(value, location.href).href; }
    catch { return ''; }
  };
  const largest = (value) => (value || '').split(',')
    .map(part => { const bits=part.trim().split(/\s+/); return {url:bits[0], width:Number((bits[1]||'').replace(/\D/g,''))||0}; })
    .sort((a,b)=>b.width-a.width)[0]?.url || '';
  const labelOf = (element) => [element.id, element.className, element.getAttribute('role'),
    element.getAttribute('aria-label'), element.getAttribute('aria-roledescription'),
    element.getAttribute('data-testid')]
    .filter(value => typeof value === 'string').join(' ');
  const isCarouselLabel = (value) => /(banner|hero|carousel|carrossel|slider|swiper|slick|slide)/i.test(value || '');
  const rect = (element) => {
    const r=element.getBoundingClientRect();
    return {x:Math.round(r.x), y:Math.round(r.y), left:r.left, right:r.right, top:r.top, bottom:r.bottom,
      width:Math.round(r.width), height:Math.round(r.height),
      visible:r.width>0 && r.height>0 && r.right>0 && r.left<vw && r.bottom>0 && r.top<vh};
  };
  const assetNodes = [...document.querySelectorAll('img,video,[style*="background" i]')];
  const visualNodes = [...assetNodes, ...document.querySelectorAll(
    '[class*="banner" i],[class*="hero" i],'
    + '[class*="carousel" i],[class*="slider" i],[class*="swiper" i],[class*="slide" i]'
  )];
  const visibleHero = visualNodes
    .map(element => ({element, box:rect(element)}))
    .filter(item => item.box.width >= vw*.55 && item.box.height >= Math.max(180,vh*.20)
      && item.box.left < vw*1.25 && item.box.right > 0
      && item.box.top < vh*1.25 && item.box.bottom > 0
      && (item.element.matches('img,video') || getComputedStyle(item.element).backgroundImage !== 'none'))
    .sort((a,b)=>Number(!a.element.matches('img,video'))-Number(!b.element.matches('img,video')) || a.box.y-b.box.y)[0];
  if (!visibleHero) return [];
  let root = visibleHero.element.parentElement || visibleHero.element;
  const fallbackRoot = root;
  const heroY = visibleHero.box.y;
  const largeAssetCount = (element) => [...element.querySelectorAll('img,video')]
    .filter(candidate => {
      const box=rect(candidate);
      return box.width >= vw*.55 && box.height >= Math.max(180,vh*.20)
        && Math.abs(box.y-heroY) <= Math.max(120,vh*.30);
    }).length;
  let labeledRoot = null;
  let labeledMultiRoot = null;
  let multiAssetRoot = null;
  for (let depth=0; root.parentElement && depth<10; depth++) {
    const parent = root.parentElement;
    const label = labelOf(parent);
    const isLabeled = isCarouselLabel(label) || parent.getAttribute('aria-roledescription') === 'carousel';
    const assetCount = largeAssetCount(parent);
    if (!labeledRoot && isLabeled) labeledRoot=parent;
    if (!labeledMultiRoot && isLabeled && assetCount >= 2) labeledMultiRoot=parent;
    // Carousel implementations often label each individual slide (e.g. .slide),
    // while the actual track is the first ancestor containing multiple large assets.
    if (assetCount >= 2) { multiAssetRoot=parent; break; }
    root=parent;
  }
  root = labeledMultiRoot || multiAssetRoot || labeledRoot || fallbackRoot;
  const out=[];
  const add = (candidate) => {
    const source=absolute(candidate.source_url); if(!source) return;
    const existing=out.find(item=>item.source_url===source);
    if (existing) return;
    out.push({...candidate, source_url:source, rendered_url:absolute(candidate.rendered_url)||source});
  };
  // 1. Collect video nodes directly inside root
  const videoNodes = [...(root.matches('video') ? [root] : []), ...root.querySelectorAll('video')];
  videoNodes.forEach((v, index) => {
    const source = [
      v.currentSrc, v.src, v.getAttribute('data-src'),
      v.querySelector('source[src]')?.getAttribute('src'),
      v.querySelector('source[data-src]')?.getAttribute('data-src')
    ].map(absolute).find(Boolean);
    if (!source) return;
    const box = rect(v);
    const label = (v.getAttribute('aria-label') || v.getAttribute('title') || v.closest('[aria-label]')?.getAttribute('aria-label') || root.getAttribute('aria-label') || '').trim();
    add({
      kind: 'video',
      source_url: source,
      rendered_url: source,
      alt: label,
      link_url: absolute(v.closest('a[href]')?.href || ''),
      natural_width: v.videoWidth || null,
      natural_height: v.videoHeight || null,
      dom_order: index,
      ...box
    });
  });

  // 2. Collect image nodes, ignoring hidden/placeholder/zero-dimension images or images inside video containers
  const imageNodes = [...(root.matches('img,picture') ? [root] : []), ...root.querySelectorAll('img, picture source')];
  imageNodes.forEach((node, index) => {
    const img = node.tagName.toLowerCase()==='img' ? node : node.closest('picture')?.querySelector('img');
    const source = [
      img?.currentSrc, node.getAttribute('data-srcset'), node.getAttribute('data-lazy-srcset'),
      node.getAttribute('srcset'), img?.getAttribute('data-srcset'), img?.getAttribute('data-lazy-srcset'),
      img?.getAttribute('srcset'), node.getAttribute('data-src'), node.getAttribute('data-lazy-src'),
      node.getAttribute('data-original'), node.getAttribute('src'), img?.getAttribute('src')
    ].map(value => value && value.includes(',') ? largest(value) : value).map(absolute).find(Boolean);
    if (!source) return;
    const box=rect(img || node);
    // Ignore hidden or zero-size images (e.g. fallback images in video containers or 0x0 images)
    if (box.width === 0 || box.height === 0 || img?.closest('[class*="video" i]') || img?.parentElement?.querySelector('video')) {
      return;
    }
    const label=(img?.alt || node.getAttribute('alt') || root.getAttribute('aria-label') || '').trim();
    add({kind:'img', source_url:source, rendered_url:img?.currentSrc || source, alt:label,
      link_url:absolute(img?.closest('a[href]')?.href || node.closest('a[href]')?.href || ''),
      natural_width:img?.naturalWidth||null, natural_height:img?.naturalHeight||null,
      dom_order:1000+index, ...box});
  });

  // 3. Background images
  [root, ...root.querySelectorAll('[style*="background" i],[class*="banner" i],[class*="hero" i],[class*="slide" i]')]
    .forEach((element,index) => {
      const box=rect(element);
      if (box.width === 0 || box.height === 0) return;
      [getComputedStyle(element),getComputedStyle(element,'::before'),getComputedStyle(element,'::after')].forEach((style,pseudo) => {
        for (const match of (style.backgroundImage||'').matchAll(/url\(["']?(.*?)["']?\)/g)) {
          const source=absolute(match[1]); if (!source) continue;
          add({kind:pseudo?'background-pseudo':'background', source_url:source, rendered_url:source,
            alt:element.getAttribute('aria-label')||'', link_url:absolute(element.closest('a[href]')?.href||''),
            natural_width:null, natural_height:null, dom_order:100000+index, ...box});
        }
      });
    });
  return out.sort((a,b)=>a.dom_order-b.dom_order);
}
"""

CLICK_NEXT_JS = r"""
() => {
  const pattern=/(next|pr[oó]xim[oa]|seguinte|avançar|arrow.?right|chevron.?right|seta.?direita)/i;
  for(const el of document.querySelectorAll('button,[role="button"],.swiper-button-next,.slick-next')) {
    const label=[el.getAttribute('aria-label'),el.title,el.className,el.textContent].filter(Boolean).join(' ');
    const r=el.getBoundingClientRect();
    if(pattern.test(label) && !el.disabled && r.top<innerHeight && r.bottom>0 && r.left<innerWidth && r.right>0) {
      el.click(); return label.slice(0,160);
    }
  }
  return null;
}
"""

CAROUSEL_INFO_JS = r"""
() => {
  let declared=null;
  document.querySelectorAll('[aria-label]').forEach(el => {
    const label=el.getAttribute('aria-label')||'';
    const m=label.match(/(?:dot|slide|banner)?\s*\d+\s*(?:\/|of|de)\s*(\d+)/i);
    if(m) declared=Math.max(declared||0,Number(m[1]));
  });
  document.querySelectorAll('[data-slide-count],[data-total-slides]').forEach(el => {
    const value=Number(el.getAttribute('data-slide-count')||el.getAttribute('data-total-slides'));
    if(Number.isFinite(value)) declared=Math.max(declared||0,value);
  });
  const videos=[];
  const heroCandidates = document.querySelectorAll(
    '[class*="banner" i] video, [class*="hero" i] video, [class*="carousel" i] video, [class*="slider" i] video, [class*="swiper" i] video'
  );
  (heroCandidates.length ? heroCandidates : document.querySelectorAll('video')).forEach(v => {
    const r=v.getBoundingClientRect();
    if(r.width>=innerWidth*.50 && r.height>=180 && r.top<innerHeight*1.2 && r.bottom>0) {
      const source=v.currentSrc||v.src||v.querySelector('source[src]')?.src||v.querySelector('source[data-src]')?.getAttribute('data-src')||'';
      if(source) {
        const link = v.closest('a[href]')?.href || v.parentElement?.closest('a[href]')?.href || '';
        const label = (v.getAttribute('aria-label') || v.getAttribute('title') || v.closest('[aria-label]')?.getAttribute('aria-label') || '').trim();
        videos.push({source_url: source, link_url: link || null, alt: label || null});
      }
    }
  });
  return {declared_slides:declared,video_slides:videos};
}
"""

DISMISS_OVERLAYS_JS = r"""
() => { let n=0; document.querySelectorAll('[role="dialog"] button,[aria-modal="true"] button').forEach(b => {
  if(/^(aceitar( todos)?|aceito|permitir todos|concordo|continuar|aceitar e fechar|allow all|agree|accept)$/i.test((b.textContent||b.getAttribute('aria-label')||'').trim())) { b.click(); n++; }
}); return n; }
"""

ACTIVATE_LAZY_ASSETS_JS = r"""
() => {
  const top=window.scrollY;
  window.scrollTo(0, Math.min(document.body.scrollHeight, innerHeight));
  window.scrollTo(0, top);
  document.querySelectorAll('img[loading="lazy"]').forEach(img => img.loading='eager');
  return true;
}
"""

BLOCK_MARKERS = (
    "access denied", "you don't have permission", "permission to access", "request blocked",
    "forbidden", "akamai", "cf-chl", "just a moment", "unusual traffic",
)
logger = logging.getLogger("BannerExtractionService")


class BannerExtractionIssue(RuntimeError):
    def __init__(self, kind: str, message: str, screenshot_asset: StoredBannerAsset | None = None):
        super().__init__(message)
        self.kind = kind
        self.screenshot_asset = screenshot_asset


def is_safe_public_http_url(url: str, *, resolve_dns: bool = True) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            return False
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            if not resolve_dns:
                return True
            try:
                addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, None)}
            except (OSError, ValueError):
                return False
        return bool(addresses) and all(addr.is_global for addr in addresses)
    except (TypeError, ValueError):
        return False


def _cancelled(cancel_event: Any) -> bool:
    return bool(cancel_event and cancel_event.is_set())


def normalize_image_content_type(content_type: str, url: str, body: bytes) -> str:
    _validate_asset_body(body, url)
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in KNOWN_IMAGE_MIME:
        return normalized
    suffix = urlparse(url).path.lower()
    for extension, mime in MIME_BY_SUFFIX.items():
        if suffix.endswith(extension):
            return mime
    head = body[:32]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if b"ftypavif" in head or b"ftypavis" in head:
        return "image/avif"
    if body.lstrip()[:4].lower() == b"<svg":
        return "image/svg+xml"
    raise ValueError(f"unsupported banner content type: {content_type or 'missing'}")


def _validate_asset_body(body: bytes, url: str) -> None:
    """Reject HTML error pages saved under an image-looking URL."""
    sample = body[:16_384].lstrip().lower()
    if sample.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise BannerExtractionIssue("BLOCKED", f"O asset retornou HTML em vez de imagem: {url}")
    if any(marker.encode() in sample for marker in BLOCK_MARKERS):
        raise BannerExtractionIssue("BLOCKED", f"O asset foi bloqueado pelo provedor: {url}")


@dataclass
class BrandExtractionResult:
    banners: list[BannerCandidate]
    videos: list[BannerVideoSlide]
    screenshot_asset: StoredBannerAsset | None
    final_url: str


AssetFetcher = Callable[[Any, str, str], tuple[bytes, str]]


class BannerExtractionService:
    def __init__(
        self,
        storage: BannerStorageService = banner_storage_service,
        *,
        max_slides: int = MAX_SLIDES,
        settle_ms: int = SETTLE_MS,
        asset_fetcher: Optional[AssetFetcher] = None,
    ):
        self.storage = storage
        self.max_slides = max_slides
        self.settle_ms = settle_ms
        self.asset_fetcher = asset_fetcher or self._fetch_asset

    def extract_brand(
        self,
        browser: Any,
        brand: Any,
        cancel_event: Any = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> BrandExtractionResult:
        if _cancelled(cancel_event):
            raise InterruptedError("banner extraction cancelled")
        brand_key = getattr(brand, "brand_key", None) or brand["brand_key"]
        brand_name = getattr(brand, "brand_name", None) or brand["brand_name"]
        domain = getattr(brand, "domain", None) or brand["domain"]
        if isinstance(brand, dict):
            page_url = brand.get("banner_start_url") or f"https://{domain.strip('/')}"
            proxy_url = brand.get("proxy_url")
        else:
            page_url = getattr(brand, "banner_start_url", None) or f"https://{domain.strip('/')}"
            proxy_url = getattr(brand, "proxy_url", None)

        # Zara and Lacoste expose the home merchandising data without requiring
        # DOM automation.  Keeping these providers on the server makes the
        # production flow fully automatic and avoids the browser-extension path.
        if brand_key == "zara":
            return self._extract_zara(
                brand_key, brand_name, proxy_url, cancel_event, progress_callback,
            )
        if brand_key == "lacoste":
            return self._extract_lacoste(
                brand_key, brand_name, proxy_url, cancel_event, progress_callback,
            )
        if not is_safe_public_http_url(page_url):
            raise ValueError("brand domain does not resolve to a public HTTP(S) target")

        context_options = dict(
            viewport=VIEWPORT, locale="pt-BR", timezone_id="America/Sao_Paulo",
            user_agent=USER_AGENT, java_script_enabled=True,
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        if proxy_url:
            context_options["proxy"] = {"server": proxy_url}
        context = browser.new_context(**context_options)
        page = context.new_page()
        try:
            response = page.goto(page_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            if _cancelled(cancel_event):
                raise InterruptedError("banner extraction cancelled")
            page.wait_for_timeout(self.settle_ms)
            page.evaluate(DISMISS_OVERLAYS_JS)
            page.wait_for_timeout(500)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                logger.debug("networkidle timeout for %s; continuing with settled DOM", page_url)
            return self.collect_page(
                page, brand_key, brand_name, cancel_event, progress_callback,
                navigation_status=response.status if response else None,
            )
        finally:
            page.close()
            context.close()

    def collect_page(
        self,
        page: Any,
        brand_key: str,
        brand_name: str,
        cancel_event: Any = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
        navigation_status: int | None = None,
    ) -> BrandExtractionResult:
        if _cancelled(cancel_event):
            raise InterruptedError("banner extraction cancelled")
        screenshot = self.storage.store_asset(page.screenshot(full_page=False), "image/png")
        self._raise_if_blocked(page, navigation_status, screenshot)
        collected: dict[str, dict] = {}
        video_items: dict[str, dict] = {}
        no_new_rounds = 0
        declared_slides = None

        for attempt in range(self.max_slides + 1):
            if _cancelled(cancel_event):
                raise InterruptedError("banner extraction cancelled")
            page.evaluate(ACTIVATE_LAZY_ASSETS_JS)
            info = page.evaluate(CAROUSEL_INFO_JS)
            declared_slides = info.get("declared_slides") or declared_slides
            for v_entry in info.get("video_slides", []):
                raw_url = v_entry if isinstance(v_entry, str) else v_entry.get("source_url", "")
                if not raw_url:
                    continue
                absolute = urljoin(page.url, raw_url)
                if not is_safe_public_http_url(absolute):
                    continue
                v_meta = {"source_url": absolute}
                if isinstance(v_entry, dict):
                    v_meta["link_url"] = v_entry.get("link_url")
                    v_meta["alt"] = v_entry.get("alt")
                if absolute not in video_items:
                    video_items[absolute] = v_meta
                else:
                    for k in ("link_url", "alt"):
                        if v_meta.get(k) and not video_items[absolute].get(k):
                            video_items[absolute][k] = v_meta[k]
            previous = len(collected) + len(video_items)
            for item in page.evaluate(COLLECT_BANNERS_JS):
                source_url = item["source_url"]
                if is_video_url(source_url) or item.get("kind") == "video":
                    if is_safe_public_http_url(source_url):
                        if source_url not in video_items:
                            item["discovered_on_attempt"] = attempt
                            video_items[source_url] = item
                        else:
                            for k in ("link_url", "alt", "kind", "dom_kind"):
                                if item.get(k) and not video_items[source_url].get(k):
                                    video_items[source_url][k] = item[k]
                    continue
                if source_url not in collected:
                    item["discovered_on_attempt"] = attempt
                    collected[source_url] = item
            no_new_rounds = no_new_rounds + 1 if (len(collected) + len(video_items)) == previous else 0
            if progress_callback:
                progress_callback({"kind": "discovery", "attempt": attempt, "banner_count": len(collected), "video_count": len(video_items)})
            if attempt >= self.max_slides:
                break
            if declared_slides is None and no_new_rounds >= 2:
                break
            if declared_slides is not None and attempt >= declared_slides - 1:
                break
            if not page.evaluate(CLICK_NEXT_JS):
                break
            page.wait_for_timeout(SLIDE_SETTLE_MS)

        banners: list[BannerCandidate] = []
        seen_hashes: set[str] = set()
        try:
            for order, item in enumerate(collected.values(), start=1):
                if _cancelled(cancel_event):
                    raise InterruptedError("banner extraction cancelled")
                source_url = item["source_url"]
                rendered_url = item.get("rendered_url") or source_url
                if not is_safe_public_http_url(source_url):
                    continue
                # The URL must have been discovered in this page. Cross-host CDNs are valid;
                # arbitrary caller-supplied URLs never enter this method.
                body, content_type = self.asset_fetcher(page.context, source_url, page.url)
                if _cancelled(cancel_event):
                    raise InterruptedError("banner extraction cancelled")
                asset = self.storage.store_asset(body, content_type)
                if asset.sha256 in seen_hashes:
                    continue
                seen_hashes.add(asset.sha256)
                banner_order = len(banners) + 1
                description = item.get("alt") or f"banner-{banner_order}"
                banners.append(BannerCandidate(
                    banner_id=f"{brand_key}-{banner_order}-{asset.sha256[:12]}", brand_key=brand_key,
                    brand_name=brand_name, slide_order=banner_order,
                    friendly_filename=friendly_banner_filename(banner_order, description, brand_name, asset.extension),
                    asset=asset, source_url=source_url, rendered_url=rendered_url,
                    click_url=item.get("link_url") or None, alt_text=item.get("alt") or None,
                    dom_kind=item.get("kind", "img"), rendered_width=item.get("width"),
                    rendered_height=item.get("height"), natural_width=item.get("natural_width"),
                    natural_height=item.get("natural_height"),
                ))
                if progress_callback:
                    progress_callback({"kind": "banner", "banner": banners[-1].model_dump(mode="json")})
        except BannerExtractionIssue as exc:
            exc.screenshot_asset = screenshot
            raise

        videos = [
            BannerVideoSlide(
                brand_key=brand_key,
                slide_order=i,
                source_url=item["source_url"],
                click_url=item.get("link_url") or None,
                alt_text=item.get("alt") or None,
            )
            for i, item in enumerate(video_items.values(), 1)
        ]
        if not banners and not videos:
            raise BannerExtractionIssue(
                "NO_BANNERS",
                f"Nenhum banner ou vídeo foi encontrado em {page.url}",
                screenshot_asset=screenshot,
            )
        return BrandExtractionResult(banners=banners, videos=videos, screenshot_asset=screenshot, final_url=page.url)

    def _extract_zara(
        self,
        brand_key: str,
        brand_name: str,
        proxy_url: str | None,
        cancel_event: Any,
        progress_callback: Optional[Callable[[dict], None]],
    ) -> BrandExtractionResult:
        """Read the same marketing feed that renders Zara's first home universe."""
        query = "channel=web&languageId=300&spot=" + quote(ZARA_HOME_SPOT)
        endpoint = f"{ZARA_MARKETING_URL}?{query}"
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        with Session(impersonate="chrome120", timeout=DIRECT_TIMEOUT_SECONDS) as session:
            body, _, final_url = self._request_direct(
                session, endpoint, proxies=proxies, document=True,
                accept="application/json, text/plain, */*",
            )
            try:
                payload = json.loads(body)
            except (TypeError, ValueError) as exc:
                raise BannerExtractionIssue("BLOCKED", "A API de marketing da Zara não retornou JSON válido") from exc

            root_spots = payload.get("spots") if isinstance(payload, dict) else None
            if not isinstance(root_spots, list):
                raise BannerExtractionIssue("NO_BANNERS", "A resposta da Zara não contém spots de marketing")

            universe_slides: list[dict[str, Any]] = []
            for root in root_spots:
                slides = ((root or {}).get("content") or {}).get("slides") or []
                for universe in slides:
                    nested = (((universe or {}).get("spot") or {}).get("content") or {}).get("slides") or []
                    if nested:
                        universe_slides = nested
                        break
                if universe_slides:
                    break
            if not universe_slides:
                raise BannerExtractionIssue("NO_BANNERS", "A Zara não publicou banners no universo ativo da home")

            images: list[dict[str, Any]] = []
            videos: list[BannerVideoSlide] = []
            seen_sources: set[str] = set()
            for slide in universe_slides[: self.max_slides]:
                if _cancelled(cancel_event):
                    raise InterruptedError("banner extraction cancelled")
                media = self._select_zara_desktop_media(slide)
                if not media:
                    continue
                video_url = self._find_video_url(media)
                if video_url and self._allowed_asset_host(video_url, ZARA_ASSET_HOSTS):
                    videos.append(BannerVideoSlide(
                        brand_key=brand_key,
                        slide_order=len(videos) + 1,
                        source_url=video_url,
                        click_url=self._zara_click_url(slide),
                        alt_text=self._zara_description(slide),
                    ))
                    continue
                layer = next(
                    (item for item in (media.get("layers") or []) if str(item.get("type", "")).lower() == "image"),
                    None,
                )
                if not layer:
                    continue
                source_url = self._zara_image_url(layer)
                if not source_url or source_url in seen_sources or not self._allowed_asset_host(source_url, ZARA_ASSET_HOSTS):
                    continue
                seen_sources.add(source_url)
                images.append({
                    "source_url": source_url,
                    "description": self._zara_description(slide),
                    "click_url": self._zara_click_url(slide),
                    "width": self._positive_int(media.get("width")),
                    "height": self._positive_int(media.get("height")),
                })

            if progress_callback:
                progress_callback({"kind": "discovery", "attempt": 0, "banner_count": len(images), "video_count": len(videos)})
            banners = self._download_direct_images(
                session, images, brand_key, brand_name, final_url, proxies,
                cancel_event, progress_callback, "zara-api", ZARA_ASSET_HOSTS,
            )
        if not banners and not videos:
            raise BannerExtractionIssue("NO_BANNERS", "Nenhum banner válido foi encontrado na API de marketing da Zara")
        return BrandExtractionResult(banners=banners, videos=videos, screenshot_asset=None, final_url=final_url)

    def _extract_lacoste(
        self,
        brand_key: str,
        brand_name: str,
        proxy_url: str | None,
        cancel_event: Any,
        progress_callback: Optional[Callable[[dict], None]],
    ) -> BrandExtractionResult:
        """Parse Lacoste's server-rendered Page Designer content."""
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        with Session(impersonate="chrome120", timeout=DIRECT_TIMEOUT_SECONDS) as session:
            body, _, final_url = self._request_direct(
                session, LACOSTE_HOME_URL, proxies=proxies, document=True,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            )
            soup = BeautifulSoup(body, "html.parser")
            page_text = soup.get_text(" ", strip=True).lower()[:8_000]
            if any(marker in page_text for marker in BLOCK_MARKERS):
                raise BannerExtractionIssue("BLOCKED", "A Lacoste retornou uma página de bloqueio")
            root = soup.select_one(".js-modular-full-page")
            if root is None:
                raise BannerExtractionIssue("NO_BANNERS", "A área de campanhas da home da Lacoste não foi encontrada")

            images: list[dict[str, Any]] = []
            seen_sources: set[str] = set()
            for picture in root.select("picture"):
                selected = self._select_picture_source(picture, final_url)
                if not selected or selected in seen_sources or not self._allowed_asset_host(selected, LACOSTE_ASSET_HOSTS):
                    continue
                seen_sources.add(selected)
                image = picture.find("img")
                anchor = picture.find_parent("a", href=True)
                if anchor is None:
                    container = picture.find_parent(class_=re.compile(r"modular|push", re.I))
                    anchor = container.find("a", href=True) if container else None
                images.append({
                    "source_url": selected,
                    "description": (image.get("alt") if image else None) or "banner",
                    "click_url": urljoin(final_url, anchor.get("href")) if anchor else None,
                    "width": self._positive_int(image.get("width")) if image else None,
                    "height": self._positive_int(image.get("height")) if image else None,
                })

            videos: list[BannerVideoSlide] = []
            seen_video_urls: set[str] = set()
            for video in root.select("video"):
                source = video.get("src") or video.get("data-src")
                if not source:
                    source_node = video.select_one("source[src], source[data-src]")
                    source = source_node.get("src") or source_node.get("data-src") if source_node else None
                source_url = urljoin(final_url, source) if source else ""
                if not source_url or source_url in seen_video_urls or not self._allowed_asset_host(source_url, LACOSTE_ASSET_HOSTS):
                    continue
                seen_video_urls.add(source_url)
                anchor = video.find_parent("a", href=True)
                videos.append(BannerVideoSlide(
                    brand_key=brand_key,
                    slide_order=len(videos) + 1,
                    source_url=source_url,
                    click_url=urljoin(final_url, anchor.get("href")) if anchor else None,
                    alt_text=video.get("aria-label") or video.get("title") or "Vídeo da campanha",
                ))

            if progress_callback:
                progress_callback({"kind": "discovery", "attempt": 0, "banner_count": len(images), "video_count": len(videos)})
            banners = self._download_direct_images(
                session, images[: self.max_slides], brand_key, brand_name, final_url, proxies,
                cancel_event, progress_callback, "lacoste-html", LACOSTE_ASSET_HOSTS,
            )
        if not banners and not videos:
            raise BannerExtractionIssue("NO_BANNERS", "Nenhum banner ou vídeo foi encontrado na home da Lacoste")
        return BrandExtractionResult(banners=banners, videos=videos, screenshot_asset=None, final_url=final_url)

    def _download_direct_images(
        self,
        session: Session,
        images: list[dict[str, Any]],
        brand_key: str,
        brand_name: str,
        referer: str,
        proxies: dict[str, str] | None,
        cancel_event: Any,
        progress_callback: Optional[Callable[[dict], None]],
        dom_kind: str,
        allowed_hosts: frozenset[str],
    ) -> list[BannerCandidate]:
        banners: list[BannerCandidate] = []
        seen_hashes: set[str] = set()
        for item in images:
            if _cancelled(cancel_event):
                raise InterruptedError("banner extraction cancelled")
            source_url = item["source_url"]
            if not self._allowed_asset_host(source_url, allowed_hosts):
                continue
            body, content_type, rendered_url = self._request_direct(
                session, source_url, proxies=proxies, referer=referer, document=False,
                accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            )
            if not self._allowed_asset_host(rendered_url, allowed_hosts):
                raise ValueError("banner download redirected outside the retailer CDN")
            normalized_type = normalize_image_content_type(content_type, rendered_url, body)
            asset = self.storage.store_asset(body, normalized_type)
            if asset.sha256 in seen_hashes:
                continue
            seen_hashes.add(asset.sha256)
            order = len(banners) + 1
            description = item.get("description") or f"banner-{order}"
            candidate = BannerCandidate(
                banner_id=f"{brand_key}-{order}-{asset.sha256[:12]}",
                brand_key=brand_key,
                brand_name=brand_name,
                slide_order=order,
                friendly_filename=friendly_banner_filename(order, description, brand_name, asset.extension),
                asset=asset,
                source_url=source_url,
                rendered_url=rendered_url,
                click_url=item.get("click_url") or None,
                alt_text=description,
                dom_kind=dom_kind,
                rendered_width=item.get("width"),
                rendered_height=item.get("height"),
                natural_width=item.get("width"),
                natural_height=item.get("height"),
            )
            banners.append(candidate)
            if progress_callback:
                progress_callback({"kind": "banner", "banner": candidate.model_dump(mode="json")})
        return banners

    def _request_direct(
        self,
        session: Session,
        url: str,
        *,
        proxies: dict[str, str] | None,
        document: bool,
        accept: str,
        referer: str | None = None,
    ) -> tuple[bytes, str, str]:
        last_error: Exception | None = None
        headers = {"Accept": accept, "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"}
        if referer:
            headers["Referer"] = referer
        for attempt in range(ASSET_RETRIES):
            try:
                response = session.get(url, headers=headers, proxies=proxies, allow_redirects=True)
                if response.status_code in {401, 403, 429, 451}:
                    raise BannerExtractionIssue("BLOCKED", f"O provedor bloqueou a extração (HTTP {response.status_code})")
                if response.status_code < 200 or response.status_code >= 300:
                    raise RuntimeError(f"download failed with HTTP {response.status_code}")
                final_url = str(response.url)
                if not is_safe_public_http_url(final_url, resolve_dns=False):
                    raise ValueError("download redirected to a non-public target")
                response_body = bytes(response.content)
                size_limit = DIRECT_DOCUMENT_BYTES if document else self.storage.max_asset_bytes
                if len(response_body) > size_limit:
                    raise ValueError("response exceeds configured byte limit")
                return response_body, response.headers.get("content-type", ""), final_url
            except BannerExtractionIssue:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < ASSET_RETRIES:
                    time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"falha no acesso HTTP após {ASSET_RETRIES} tentativas: {last_error}") from last_error

    @staticmethod
    def _select_zara_desktop_media(slide: dict[str, Any]) -> dict[str, Any] | None:
        raw_candidates = slide.get("xmedias") or slide.get("xmedia") or []
        if isinstance(raw_candidates, dict):
            raw_candidates = [raw_candidates]
        candidates = [item for item in raw_candidates if isinstance(item, dict)]
        if not candidates:
            return None
        target_ratio = 16 / 9
        return min(candidates, key=lambda item: (
            abs(((BannerExtractionService._positive_int(item.get("width")) or 1) /
                 (BannerExtractionService._positive_int(item.get("height")) or 1)) - target_ratio),
            -(BannerExtractionService._positive_int(item.get("width")) or 0),
        ))

    @staticmethod
    def _zara_image_url(layer: dict[str, Any]) -> str | None:
        direct = layer.get("url") or layer.get("src")
        if isinstance(direct, str) and direct:
            return urljoin("https://static.zara.net/", direct).replace("{width}", "1280")
        path, name = layer.get("path"), layer.get("name")
        if not isinstance(path, str) or not isinstance(name, str) or not path or not name:
            return None
        if not path.endswith("/"):
            path += "/"
        base = urljoin("https://static.zara.net/", path.lstrip("/"))
        timestamp = layer.get("timestamp")
        suffix = f"?ts={quote(str(timestamp))}&w=1280" if timestamp is not None else "?w=1280"
        return f"{base}{quote(name, safe='')}/{quote(name, safe='')}.jpg{suffix}"

    @staticmethod
    def _zara_description(slide: dict[str, Any]) -> str:
        for key in ("sectionName", "name", "title", "description"):
            value = slide.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for meta in slide.get("meta") or []:
            value = ((meta or {}).get("enhancement") or {}).get("name")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "banner"

    @staticmethod
    def _zara_click_url(slide: dict[str, Any]) -> str | None:
        for key in ("href", "url", "link"):
            value = slide.get(key)
            if isinstance(value, str) and value:
                return urljoin("https://www.zara.com/br/", value)
        seo = ((slide.get("category") or {}).get("seo") or {}).get("keyword")
        return urljoin("https://www.zara.com/br/pt/", seo) if isinstance(seo, str) and seo else None

    @staticmethod
    def _find_video_url(value: Any) -> str | None:
        if isinstance(value, str):
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https"} and (is_video_url(value) or parsed.path.lower().endswith(".m3u8")):
                return value
            return None
        if isinstance(value, dict):
            for item in value.values():
                found = BannerExtractionService._find_video_url(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = BannerExtractionService._find_video_url(item)
                if found:
                    return found
        return None

    @staticmethod
    def _select_picture_source(picture: Any, base_url: str) -> str | None:
        candidates: list[tuple[int, str]] = []
        for source in picture.find_all("source"):
            srcset = source.get("srcset") or source.get("data-srcset")
            if not srcset:
                continue
            media = source.get("media") or ""
            match = re.search(r"min-width\s*:\s*(\d+)px", media, re.I)
            breakpoint = int(match.group(1)) if match else 0
            candidates.append((breakpoint, BannerExtractionService._best_srcset_url(srcset)))
        image = picture.find("img")
        if image:
            fallback = image.get("srcset") or image.get("data-srcset") or image.get("src") or image.get("data-src")
            if fallback:
                candidates.append((-1, BannerExtractionService._best_srcset_url(fallback)))
        valid = [(point, url) for point, url in candidates if url and not url.startswith(("data:", "blob:"))]
        return urljoin(base_url, max(valid, key=lambda item: item[0])[1]) if valid else None

    @staticmethod
    def _best_srcset_url(srcset: str) -> str:
        options: list[tuple[float, str]] = []
        for raw in srcset.split(","):
            bits = raw.strip().split()
            if not bits:
                continue
            score = 1.0
            if len(bits) > 1:
                descriptor = bits[-1].lower()
                try:
                    score = float(descriptor[:-1]) if descriptor.endswith(("x", "w")) else 1.0
                except ValueError:
                    score = 1.0
            options.append((score, bits[0]))
        return max(options, default=(0, ""), key=lambda item: item[0])[1]

    @staticmethod
    def _allowed_asset_host(url: str, allowed_hosts: frozenset[str]) -> bool:
        try:
            parsed = urlparse(url)
            return parsed.scheme == "https" and (parsed.hostname or "").lower().rstrip(".") in allowed_hosts
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _raise_if_blocked(page: Any, navigation_status: int | None, screenshot: StoredBannerAsset) -> None:
        status = int(navigation_status or 0)
        if status in {401, 403, 429, 451}:
            raise BannerExtractionIssue(
                "BLOCKED", f"A página bloqueou a extração (HTTP {status})", screenshot_asset=screenshot,
            )
        try:
            title = (page.title() or "").lower()
            body = (page.locator("body").inner_text(timeout=2_000) or "").lower()
        except Exception:
            title, body = "", ""
        evidence = f"{title} {body[:4_000]}"
        if any(marker in evidence for marker in BLOCK_MARKERS):
            raise BannerExtractionIssue(
                "BLOCKED", f"A página bloqueou a extração: {page.url}", screenshot_asset=screenshot,
            )

    def _fetch_asset(self, context: Any, url: str, referer: str) -> tuple[bytes, str]:
        last_error: Exception | None = None
        for attempt in range(ASSET_RETRIES):
            try:
                response = context.request.get(url, headers={"Referer": referer}, timeout=NAVIGATION_TIMEOUT_MS)
                if response.status in {401, 403, 429, 451}:
                    raise BannerExtractionIssue("BLOCKED", f"O asset foi bloqueado (HTTP {response.status})")
                if not response.ok:
                    raise RuntimeError(f"banner download failed with HTTP {response.status}")
                if not is_safe_public_http_url(response.url):
                    raise ValueError("banner download redirected to a non-public target")
                body = response.body()
                if len(body) > self.storage.max_asset_bytes:
                    raise ValueError("banner asset exceeds configured byte limit")
                return body, normalize_image_content_type(response.headers.get("content-type", ""), response.url, body)
            except BannerExtractionIssue:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < ASSET_RETRIES:
                    time.sleep(0.5 * (2 ** attempt))
        raise RuntimeError(f"falha ao baixar o banner após {ASSET_RETRIES} tentativas: {last_error}") from last_error


banner_extraction_service = BannerExtractionService()
