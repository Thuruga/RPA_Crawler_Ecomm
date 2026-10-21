from fastapi import APIRouter, HTTPException, Response
from typing import Any, Dict, List, Tuple
import logging
import ipaddress
import json as _json
import re
import aiohttp
from bs4 import BeautifulSoup
from pydantic import BaseModel
from urllib.parse import urlparse
from core.models import DynamicBrand, DynamicBrandCreate, CategoryMapping, BrandActiveUpdate
from services.brand_service import brand_service
from services.engines.base_engine import BaseEngine
from services.engines.factory import engine_factory
from core.session_manager import SessionManager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Brands"])


# ---------------------------------------------------------------------------
# Pydantic models for POST /brands/identify
# ---------------------------------------------------------------------------

class IdentifyRequest(BaseModel):
    url: str


class IdentifyResponse(BaseModel):
    engine: str
    inferred_name: str
    domain: str
    warning: str | None = None


# ---------------------------------------------------------------------------
# infer_brand_name — D-01 precedence: JSON-LD → OG → <title> → domain
# ---------------------------------------------------------------------------

def infer_brand_name(html: "str | BeautifulSoup | None", domain: str) -> str:
    """Infer brand name from home HTML following D-01 precedence.

    Precedence:
      1. JSON-LD Organization / Brand ``name``
      2. OG ``og:site_name`` meta tag
      3. ``<title>`` first segment (split on `` - ``, `` | ``, `` – ``, `` — ``)
      4. Domain-derived fallback: strip TLD + ccTLD, split camelCase, capitalise.

    Accepts ``html`` as a raw HTML string, a pre-built BeautifulSoup object, or
    ``None`` (triggers domain fallback immediately).
    """
    # Build soup from html when needed
    if html is None:
        soup = None
    elif isinstance(html, BeautifulSoup):
        soup = html
    else:
        soup = BeautifulSoup(html, "html.parser")

    if soup is not None:
        # 1. JSON-LD — iterate all ld+json blocks (analog: zara_parser._jsonld_blocks)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                block = _json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            if isinstance(block, dict) and block.get("@type") in ("Organization", "Brand"):
                name = block.get("name", "").strip()
                if name:
                    return name

        # 2. OG site_name (analog: zara_parser.py l.267-270)
        og_tag = soup.find("meta", property="og:site_name")
        if og_tag:
            name = og_tag.get("content", "").strip()
            if name:
                return name

        # 3. <title> — first segment before common separators
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            raw_title = title_tag.string.strip()
            # Split on " - ", " | ", " – ", " — "
            segment = re.split(r"\s[-|–—]\s", raw_title)[0].strip()
            if segment:
                return segment

    # 4. Domain fallback (analog: 40-PATTERNS.md § Domain fallback pattern)
    # Strip www., then take the first label before the first dot.
    host = domain.lower()
    if host.startswith("www."):
        host = host[len("www."):]
    stem = host.split(".")[0]
    # Split camelCase (e.g. "hugoboss" → "hugoboss"; "HugoBoss" → "Hugo Boss")
    with_spaces = re.sub(r"([a-z])([A-Z])", r"\1 \2", stem)
    # Replace hyphens/underscores with spaces then capitalise each word
    return " ".join(w.capitalize() for w in with_spaces.replace("-", " ").replace("_", " ").split())


# ---------------------------------------------------------------------------
# Marketplace domain → engine mapping
# ---------------------------------------------------------------------------
# Os marketplaces (Mercado Livre / Amazon / Netshoes) NÃO respondem às probes de
# plataforma (collections.json, api/catalog_system, fbitsstatic, vtexassets,
# cdn.shopify, demandware) — então sem este mapeamento detect_engine os classifica
# como "unknown", o identify falha ("não identificado") e o título do monitor cai
# para o brand_key em maiúsculas. As strings retornadas são EXATAMENTE as chaves de
# engine resolvíveis por EngineFactory.get_engine / brands.json
# (mercadolivre / amazon / netshoes — via normalize_brand_key).
#
# A chave é o domínio REGISTRÁVEL (eTLD+1-ish) do marketplace; o match cobre
# qualquer subdomínio (www., produto., lista., m., etc.) por sufixo de label.
_MARKETPLACE_DOMAIN_ENGINES: dict[str, str] = {
    "mercadolivre.com.br": "mercadolivre",
    "mercadolibre.com": "mercadolivre",
    "amazon.com.br": "amazon",
    "amazon.com": "amazon",
    "netshoes.com.br": "netshoes",
}


# Domínio REGISTRÁVEL canônico de cada marketplace, como cadastrado em brands.json
# (o campo ``domain`` da marca). O identify normaliza o host detectado para este valor
# quando é um marketplace, de modo que o match por domínio do frontend
# (`normalizeDomain(b.domain) === normalizeDomain(identified.domain)`) resolva a marca
# cadastrada MESMO quando a URL vem em um subdomínio como `produto.mercadolivre.com.br`
# (que, sem esta normalização, não casaria `mercadolivre.com.br`). Mantém o frontend
# intocado e a correção testável server-side.
_MARKETPLACE_ENGINE_CANONICAL_DOMAIN: dict[str, str] = {
    "mercadolivre": "mercadolivre.com.br",
    "amazon": "amazon.com.br",
    "netshoes": "netshoes.com.br",
}


def detect_marketplace_engine(domain: str) -> str | None:
    """Retorna o engine de marketplace para um host, ou None se não for marketplace.

    Casa por sufixo de LABEL (boundary) para cobrir todas as formas de host:
    ``www.amazon.com.br``, ``produto.mercadolivre.com.br``, ``lista.mercadolivre.com.br``,
    ``m.netshoes.com.br`` etc. Usa ``host == base or host.endswith("." + base)`` para
    evitar que ``maliciousmercadolivre.com.br`` (sufixo de string, mas não de label)
    case indevidamente.
    """
    host = (domain or "").strip().lower().rstrip(".")
    if not host:
        return None
    for base, engine in _MARKETPLACE_DOMAIN_ENGINES.items():
        if host == base or host.endswith("." + base):
            return engine
    return None


def canonical_marketplace_domain(engine: str) -> str | None:
    """Domínio registrável canônico para um engine de marketplace, ou None."""
    return _MARKETPLACE_ENGINE_CANONICAL_DOMAIN.get(engine)


# ---------------------------------------------------------------------------
# detect_engine — returns (engine, home_html) tuple on EVERY path (D-01)
# ---------------------------------------------------------------------------

async def detect_engine(domain: str) -> tuple[str, str | None]:
    """Tenta descobrir automaticamente a plataforma (motor) do domínio.

    Returns a 2-tuple (engine: str, home_html: str | None).
    home_html is the raw HTML of the home page when the Step-3 HTTP probe
    successfully fetched it; None for early API-based detections and for
    fallback/error paths.
    """
    # Step 0 (marketplace): ML/Amazon/Netshoes não respondem às probes de
    # plataforma; resolvê-los aqui evita o "unknown" e o título caindo no
    # brand_key. Sem fetch de rede — só o host. home_html=None (o nome real do
    # produto é resolvido depois pelo get_pdp_product do monitor, não pela home).
    marketplace_engine = detect_marketplace_engine(domain)
    if marketplace_engine is not None:
        logger.info(
            "detect_engine: marketplace '%s' detectado para %s (domínio→engine)",
            marketplace_engine, domain,
        )
        return marketplace_engine, None

    session = await SessionManager.get_session()
    base_url = f"https://{domain}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # 1. Tenta Shopify via collections.json
    try:
        async with session.get(f"{base_url}/collections.json", timeout=aiohttp.ClientTimeout(total=5), headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "collections" in data:
                    return "shopify", None
    except Exception as e:
        logger.debug("Detecção Shopify via collections.json falhou para %s: %s", domain, e)

    # 2. Tenta VTEX via API padrão
    try:
        async with session.get(f"{base_url}/api/catalog_system/pub/category/tree/1", timeout=aiohttp.ClientTimeout(total=5), headers=headers) as resp:
            if resp.status == 200:
                return "vtex", None
    except Exception as e:
        logger.debug("Detecção VTEX via API de categorias falhou para %s: %s", domain, e)

    # 3. Fallback: Analisa o HTML da home page
    # Security (T-25-01-SR): allow_redirects=False evita que um redirect malicioso
    # faça a detecção ler HTML de um domínio atacante em vez do domínio-alvo.
    # Pitfall 1 (D-01): when no marker is matched but HTML was successfully fetched,
    # save it so name inference can reuse it — but still fall through to the browser
    # probe (step 6) which may identify sfcc/zara via rendered markers.
    _step3_html: str | None = None
    try:
        async with session.get(base_url, timeout=aiohttp.ClientTimeout(total=5), headers=headers, allow_redirects=False) as resp:
            html = await resp.text()
            html_lower = html.lower()

            # Step 3 (Wake Commerce — D-02, D-05, Pitfall 1): probe ANTES do VTEX HTML.
            # fbitsstatic.net é o CDN exclusivo da plataforma Wake Commerce.
            # D-05: retorna "wake" (engine correto) para não acionar a regra D-04 (unknown → inativo).
            if "fbitsstatic.net" in html_lower:
                logger.info("detect_engine: Wake Commerce detectado para %s (fbitsstatic.net)", domain)
                return "wake", html

            # Step 4 (VTEX HTML — T-25-01-WK): apenas marcadores exclusivos da VTEX.
            # Removida a condição solta '"vtex" in html_lower' que causava falso
            # positivo em páginas Wake/marketplace (Pitfall 1).
            if "vtexassets.com" in html_lower or "vtexcommercestable.com" in html_lower:
                return "vtex", html

            # Step 5 (Shopify HTML): marcadores exclusivos da plataforma Shopify.
            if "cdn.shopify.com" in html_lower or "window.shopify" in html_lower:
                return "shopify", html

            # Step 5b (Zara / Inditex): storefront proprio da Zara.
            if "static.zara.net" in html_lower or (
                "zara.com" in html_lower and "data-store=" in html_lower
            ):
                logger.info("detect_engine: Zara detectado para %s", domain)
                return "zara", html

            # No marker matched — save HTML for name inference fallback, then
            # continue to the browser probe (step 6) which may still identify sfcc/zara.
            _step3_html = html

    except Exception as e:
        logger.debug("Detecção via análise do HTML da home falhou para %s: %s", domain, e)

    # Step 6 (D-01, D-02, D-03, D-07): SFCC browser probe — last resort.
    # Renderiza a home via Playwright para expor assets demandware que HTTP direto
    # não entrega (403 em Lacoste/HugoBoss). Só dispara depois que Shopify, VTEX
    # e as probes HTML falharam (D-07 — last-resort ordering).
    # Marcadores exclusivos: demandware.static e demandware.edgesuite.net (D-02).
    # A substring ampla "demandware" NÃO é usada para evitar falsos positivos (SC-4).
    # Import lazy (dentro do try) para que uma instalação sem Playwright não quebre
    # o módulo no startup (D-03 — reusa BrowserManager existente, sem nova infra).
    try:
        from core.browser_manager import BrowserManager
        rendered_html = await BrowserManager.fetch_html(f"https://{domain}")
        rendered_lower = rendered_html.lower()
        if "static.zara.net" in rendered_lower or (
            "zara.com" in rendered_lower and "data-store=" in rendered_lower
        ):
            logger.info("detect_engine: Zara detectado para %s (rendered marker)", domain)
            return "zara", rendered_html
        if "demandware.static" in rendered_lower or "demandware.edgesuite.net" in rendered_lower:
            logger.info("detect_engine: SFCC detectado para %s (demandware marker)", domain)
            return "sfcc", rendered_html
    except Exception as e:
        # D-04: probe failure é normal (timeout, Playwright desabilitado, 403 sem markers) —
        # degrada silenciosamente para "unknown" sem crash.
        logger.debug("Detecção SFCC via browser falhou para %s: %s", domain, e)

    # Step 7: plataforma desconhecida. NÃO assume VTEX (evita mascaramento silencioso).
    # Carry step-3 HTML when available so name inference can still run (Pitfall 1 / D-01).
    return "unknown", _step3_html


@router.post("/brands/", response_model=DynamicBrand)
async def create_brand(brand_data: DynamicBrandCreate):
    """Cadastra ou atualiza uma nova marca no sistema."""
    try:
        if brand_data.engine == "auto":
            # Realiza a deteccao automatica
            brand_data.engine, _ = await detect_engine(brand_data.domain)

        saved = await brand_service.add_brand_async(brand_data)

        # D-04: Motor desconhecido → persiste inativo sem levantar erro HTTP.
        # Plataformas não suportadas não devem entrar na busca ativa (o chokepoint
        # list_brands(active_only=True) os excluirá). set_active é idempotente.
        if saved.engine == "unknown":
            logger.info(
                "create_brand: engine 'unknown' para '%s' — marcando is_active=False (D-04)",
                saved.brand_key,
            )
            saved = await brand_service.set_active_async(saved.brand_key, False)

        return saved
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/brands/identify", response_model=IdentifyResponse)
async def identify_brand(request: IdentifyRequest) -> IdentifyResponse:
    """Dry-run: detecta o motor e infere o nome da marca para uma URL fornecida.

    NÃO persiste nada (D-02). Permite que o operador confirme os dados antes de
    chamar POST /brands/ (create_brand).

    Security (T-40-SSRF / ASVS V5): rejeita schemes fora de {http, https} e
    hosts que resolvam para IPs privados/loopback/link-local/reservados ou
    'localhost' antes de qualquer fetch de rede.
    """
    # 1. Parse URL and extract domain
    parsed = urlparse(request.url.strip())
    # Support bare domains passed without scheme (urlparse puts them in path)
    scheme = parsed.scheme.lower() if parsed.scheme else ""
    host = parsed.netloc or parsed.path.split("/")[0]
    # Normalise: strip port, strip trailing slash
    host = host.split(":")[0].rstrip("/").lower()

    # 2. SSRF / input validation (T-40-SSRF, ASVS V5)
    # Reject non-http(s) schemes when an explicit scheme was provided
    if scheme and scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"Scheme não suportado: '{scheme}'. Apenas http e https são permitidos.",
        )

    # Reject localhost unconditionally
    if host in ("localhost", ""):
        raise HTTPException(
            status_code=400,
            detail="Host inválido: 'localhost' não é permitido (SSRF).",
        )

    # Reject IP literals that fall in private/loopback/link-local/reserved ranges
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise HTTPException(
                status_code=400,
                detail=f"Host inválido: endereço IP '{host}' está em um range privado/reservado (SSRF).",
            )
    except ValueError:
        # Not an IP literal — hostname is acceptable (domain name)
        pass

    # domain is the host as-is (preserve www — detect_engine uses it as passed, Pitfall 8)
    domain = host

    # 3. Engine detection (reuses home HTML for name inference — no second fetch, D-01)
    try:
        engine, home_html = await detect_engine(domain)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Erro ao acessar o domínio: {exc}",
        )

    # 4. Brand name inference (D-01 precedence: JSON-LD → OG → title → domain)
    inferred_name = infer_brand_name(home_html, domain)

    # 4b. Marketplace: normaliza o domínio de retorno para o registrável canônico
    # (ex.: produto.mercadolivre.com.br → mercadolivre.com.br). Assim o match por
    # domínio do frontend resolve a marca de marketplace cadastrada mesmo quando a
    # URL do produto veio num subdomínio (produto./lista./m.). Sem isto, o ML em
    # `produto.` não casava `mercadolivre.com.br` e nenhum monitor era criado.
    canonical_domain = canonical_marketplace_domain(engine)
    if canonical_domain is not None:
        domain = canonical_domain

    # 5. Warning for unknown engine (D-03) — does NOT block onboarding
    warning: str | None = None
    if engine == "unknown":
        warning = (
            "Motor de plataforma não identificado automaticamente. "
            "Selecione o motor manualmente antes de cadastrar a marca."
        )
        logger.info(
            "identify_brand: engine 'unknown' para '%s' — aviso emitido (D-03)", domain
        )

    return IdentifyResponse(
        engine=engine,
        inferred_name=inferred_name,
        domain=domain,
        warning=warning,
    )


@router.get("/brands/", response_model=List[DynamicBrand])
async def list_brands():
    """Lista todas as marcas cadastradas."""
    return brand_service.list_brands()


# ---------------------------------------------------------------------------
# Category-discovery diagnostic (Plano 01-04, D-04) — aditivo, sem duplicar
# logica de categoria: reusa a MESMA CategoryValidationOutcome que o proprio
# engine ja calcula em `_discover_and_validate()` (VTEX/Shopify/Wake/SFCC/Zara,
# Wave 2/3), apenas tambem lendo `.probes` (que `discover_categories()` /
# `get_catalog()` publicos descartam). Nenhuma politica de escopo/probe e
# reimplementada aqui — apenas o mesmo flatten trivial que cada engine ja usa.
# ---------------------------------------------------------------------------
def is_catalog_capable(engine: Any) -> bool:
    """True quando o engine sobrescreve `category_has_products` (participa do
    pipeline compartilhado de descoberta de categorias). Engines search-only
    (Amazon/Mercado Livre/Netshoes) nao sobrescrevem — logo sao N/A (D-04)."""
    return type(engine).category_has_products is not BaseEngine.category_has_products


async def resolve_discover_categories(engine: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Retorna (categorias, diagnostico aditivo) para `GET /brands/{k}/discover`.

    O shape de `categorias` e IDENTICO ao que `engine.discover_categories()`
    sempre retornou (`[{"name":..., "path":...}]`) — nenhum campo e removido
    ou renomeado. `diagnostico` e somente informativo (usado como header
    aditivo, nunca no corpo desta rota, que precisa continuar sendo uma lista).
    """
    if not is_catalog_capable(engine):
        # Search-only (D-04): nao ha pipeline de categorias a rodar; retorna
        # a lista vazia canonica do proprio engine, sem custo de rede extra.
        categories = await engine.discover_categories()
        return categories, {
            "catalog_applicability": "not_applicable",
            "state": "not_applicable",
            "offered_count": len(categories),
            "empty_count": 0,
            "blocked_count": 0,
        }

    discover_and_validate = getattr(engine, "_discover_and_validate", None)
    if discover_and_validate is None:
        # Fallback defensivo: engine catalog-capable sem o metodo compartilhado
        # (nao ocorre nos 5 engines atuais) — nunca finge "ok" sem evidencia.
        categories = await engine.discover_categories()
        state = "ok" if categories else "unknown"
        return categories, {
            "catalog_applicability": "catalog_capable",
            "state": state,
            "offered_count": len(categories),
            "empty_count": 0,
            "blocked_count": 0,
        }

    from services.category_discovery import ProbeStatus
    from services.category_discovery_cache import get_cached_outcome, set_cached_outcome

    brand_key = getattr(engine, "brand_key", None)
    outcome = await get_cached_outcome(brand_key) if brand_key else None
    if outcome is None:
        outcome = await discover_and_validate()
        if brand_key:
            await set_cached_outcome(brand_key, outcome)
    # Mesma transformacao trivial que TODO engine catalog-capable ja aplica em
    # discover_categories() — nao e logica de categoria nova, apenas reaproveitada.
    categories = [{"name": c.name, "path": c.path} for c in outcome.offered]
    probe_values = list(outcome.probes.values())
    empty_count = sum(1 for p in probe_values if p.status == ProbeStatus.EMPTY)
    blocked_count = sum(1 for p in probe_values if p.status == ProbeStatus.BLOCKED)
    offered_count = len(outcome.offered)
    state = "ok" if offered_count > 0 else ("blocked" if blocked_count > 0 else "empty")

    return categories, {
        "catalog_applicability": "catalog_capable",
        "state": state,
        "offered_count": offered_count,
        "empty_count": empty_count,
        "blocked_count": blocked_count,
    }


@router.get("/brands/{brand_key}/discover")
async def discover_categories(brand_key: str, response: Response):
    """
    Aciona o motor de Auto-Discovery para encontrar a árvore de categorias real.
    Não salva nada no banco, apenas retorna para o frontend.

    Contrato preservado (mesma lista `[{"name","path"}]`, mesmo status 400
    quando um engine catalog-capable não encontra nada). Diferença aditiva
    (D-04): engines search-only (Amazon/Mercado Livre/Netshoes) nunca mais
    recebem o erro genérico "não foi possível descobrir" — são N/A, não uma
    falha de sincronização. Um header `X-Category-Discovery-State` aditivo
    (ok/empty/blocked/not_applicable) é sempre enviado para diagnóstico.
    """
    brand = brand_service.get_brand(brand_key)
    if not brand:
        raise HTTPException(status_code=404, detail="Marca não encontrada")

    engine = engine_factory.get_engine(brand_key)
    categories, diagnostic = await resolve_discover_categories(engine)
    response.headers["X-Category-Discovery-State"] = diagnostic["state"]

    if diagnostic["catalog_applicability"] == "not_applicable":
        # D-04: engine search-only nunca recebe o erro de sincronização — N/A
        # é um estado válido e distinto, não uma falha de descoberta.
        return categories

    if not categories:
        raise HTTPException(
            status_code=400,
            detail="Não foi possível descobrir as categorias. Verifique o domínio ou motor.",
            headers={"X-Category-Discovery-State": diagnostic["state"]},
        )
    
    return categories


@router.get("/brands/{brand_key}/mappings", response_model=List[CategoryMapping])
async def get_brand_mappings(brand_key: str):
    """Retorna os mapeamentos atuais de uma marca."""
    brand = brand_service.get_brand(brand_key)
    if not brand:
        raise HTTPException(status_code=404, detail="Marca não encontrada")
    return brand.mappings


@router.put("/brands/{brand_key}/mappings", response_model=DynamicBrand)
async def update_brand_mappings(brand_key: str, mappings: List[CategoryMapping]):
    """Salva os mapeamentos de categoria selecionados pelo usuário."""
    try:
        return await brand_service.update_mappings_async(brand_key, mappings)
    except KeyError:
        raise HTTPException(status_code=404, detail="Marca não encontrada")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/brands/{brand_key}/active", response_model=DynamicBrand)
async def set_brand_active(brand_key: str, payload: BrandActiveUpdate):
    """Ativa ou desativa uma marca (idempotente). 404 se a marca não existir."""
    result = await brand_service.set_active_async(brand_key, payload.is_active)
    if result is None:
        raise HTTPException(status_code=404, detail="Marca não encontrada")
    return result


@router.delete("/brands/{brand_key}")
async def delete_brand(brand_key: str):
    """Exclui uma marca do sistema e limpa monitores ativos."""
    from services.price_monitor_service import monitor_service
    
    # 1. Limpa monitores ativos desta marca
    await monitor_service.delete_monitors_by_brand(brand_key)
    
    # 2. Exclui a marca do banco
    success = await brand_service.delete_brand_async(brand_key)
    if not success:
        raise HTTPException(status_code=404, detail="Marca não encontrada")
    return {"message": f"Marca '{brand_key}' excluída com sucesso (monitores também removidos)."}
