import asyncio
import logging
from typing import Dict, Any, Optional

from config import relevance_settings
from services import relevance_gates
from services.nlp_service import nlp_service
from services.engines.factory import engine_factory
from services.engines.brand_key_utils import normalize_brand_key
from services.engines.seller_extraction import is_marketplace_default
from services.brand_service import brand_service


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level engine map: engine_key → (display_name, factory_brand_key)
# Marketplace engines must always come from EngineFactory so every operation
# routes through the shared coordinator (cache, circuit breaker, concurrency
# gate) — instantiating an engine class directly here would bypass it.
# ---------------------------------------------------------------------------
_ENGINE_MAP: Dict[str, tuple] = {
    "mercadolivre": ("Mercado Livre", "mercado_livre"),
    "netshoes":     ("Netshoes",      "netshoes"),
    "amazon":       ("Amazon",        "amazon"),
}


def passes_brand_gate(titulo: str, official_title: str, enabled: bool) -> bool:
    """
    Predicado puro de nível de módulo para o gate de marca.

    Retorna True (mantém item) se o gate estiver desativado (enabled=False) ou
    se o título do marketplace contém ao menos uma das marcas conhecidas da query.
    Retorna False (descarta item) quando enabled=True e a marca conhecida da query
    está ausente no título do marketplace.

    É o ÚNICO objeto de código chamado tanto por produção (comprehension de
    produtos_filtrados em compare_product) quanto pelos testes — sem reimplementação
    do predicado nos testes, garantindo que um filtro mal-posicionado em produção
    falharia os testes de integração (BRAND-02, anti-tautologia HIGH-1).

    Args:
        titulo: Título bruto (raw) do produto do marketplace.
        official_title: Título oficial bruto da query (produto buscado).
        enabled: Flag BRAND_GATE_ENABLED lida de relevance_settings pelo chamador.

    Returns:
        bool — True para manter o item, False para descartá-lo.
    """
    return (not enabled) or nlp_service.brand_is_present(official_title, titulo)


def _detect_candidate_brand(titulo: str, vocab_brands) -> str | None:
    """
    Retorna a marca conhecida encontrada em titulo após limpeza, ou None.

    Reutiliza nlp_service._clean_text para normalização e compara tokens contra
    o frozenset vocab_brands (nlp_service._vocab.known_brands_for_detection).
    NÃO usa lista literal de marcas — fonte única de verdade é data/nlp_vocabulary.json.

    A iteração é feita sobre ``sorted(vocab_brands)`` para que o resultado seja
    DETERMINÍSTICO quando um título contém mais de uma marca conhecida (a ordem de
    iteração de um frozenset não é estável entre processos). Critério de desempate:
    a primeira marca em ordem alfabética vence. (WR-02)

    Args:
        titulo: Título bruto do produto do marketplace.
        vocab_brands: frozenset de marcas conhecidas (ex: {"aramis", "reserva", "tommy"}).

    Returns:
        str — marca encontrada (primeira em ordem alfabética se houver várias), ou None.
    """
    clean = nlp_service._clean_text(titulo)
    words = set(clean.split())
    for brand in sorted(vocab_brands):
        if brand in words:
            return brand
    return None


def apply_visual_tiebreak(
    candidates: list,
    window: float,
    enabled: bool,
) -> list:
    """
    Reordena candidatos promovendo o sinal visual como desempate explícito entre
    candidatos da mesma marca quando os scores de texto são ambíguos (MODEL-02).

    Função pura de nível de módulo — único objeto chamado por produção E testes.
    Recebe window e enabled como argumentos (lidos pelo chamador de relevance_settings).
    NÃO lê relevance_settings internamente (anti-tautologia HIGH-1).
    Retorna nova lista (não muta in-place).

    Semântica de ordenação (chave de duas faixas, SEM flooring — corrige CR-01):
      - "Cohort de ambiguidade" de uma marca = candidatos com imagem (image>0) cujo
        final_match_score está a no máximo ``window`` pontos do top-score da própria
        marca. Critério 2 (MODEL-02) é explícito: textos PRÓXIMOS são empate de texto,
        então o de maior similaridade VISUAL fica acima — INCLUSIVE acima do líder de
        texto da janela. Todos os membros do cohort ancoram na MESMA chave primária
        (-top da marca) e competem por -image; logo a chave é contínua (não há linha de
        grade arbitrária cortando a janela, ao contrário do bucket floored).
      - Candidatos FORA da janela (gap > window), sem marca conhecida, ou sem imagem,
        ordenam pelo -final_match_score exato e ficam ABAIXO do cohort (faixa 1).
        Como todo membro in-window tem final >= top-window > final de qualquer
        out-of-window da mesma marca, a separação por faixa é consistente com o texto.

    Fallback (enabled=False ou TODOS image_match_score==0):
        retorna sorted(candidates, key=(-final_match_score, preco)) — idêntico ao
        comportamento anterior, zero regressão.

    Args:
        candidates: lista de dicts de produto com 'final_match_score',
                    'image_match_score', 'titulo', 'preco'.
        window: janela de ambiguidade de texto (escala 0-100) medida a partir do
                top-score de cada marca. window<=0 => apenas o próprio top fica no
                cohort (degrada para ordenação por texto). Sem divisão — sem guard.
        enabled: flag VISUAL_TIEBREAK_ENABLED lida de relevance_settings pelo chamador.

    Returns:
        Nova lista (mesmos objetos, nova ordem).
    """
    if not candidates:
        return candidates

    has_image = any(c.get("image_match_score", 0) > 0 for c in candidates)
    if not (enabled and has_image):
        # Fallback idêntico ao comportamento anterior: sorted por (-final, preco)
        return sorted(
            candidates,
            key=lambda x: (-x.get("final_match_score", 0.0), x.get("preco", 0.0)),
        )

    vocab_brands = nlp_service._vocab.known_brands_for_detection

    # WR-03: detecta a marca de cada candidato UMA vez e cacheia por identidade.
    # A mesma marca cacheada alimenta tanto brand_top quanto _sort_key — elimina a
    # recomputação por comparação e o risco de inconsistência entre os dois usos.
    brand_by_id: dict = {}
    brand_top: dict = {}
    for c in candidates:
        bk = _detect_candidate_brand(c.get("titulo", ""), vocab_brands)
        brand_by_id[id(c)] = bk
        if bk is not None:
            brand_top[bk] = max(brand_top.get(bk, 0.0), c.get("final_match_score", 0.0))

    def _sort_key(c):
        final = c.get("final_match_score", 0.0)
        img = c.get("image_match_score", 0.0)
        preco = c.get("preco", 0.0)
        bk = brand_by_id.get(id(c))
        top = brand_top.get(bk, 0.0) if bk is not None else 0.0
        in_window = bk is not None and img > 0 and (top - final) <= window
        if in_window:
            # Faixa 0 (cohort de ambiguidade): ancora em -top da marca; desempate por
            # -image (MODEL-02), depois preço. Sem flooring -> sem descontinuidade.
            return (0, -top, -img, preco)
        # Faixa 1 (fora da janela / sem marca / sem imagem): ordena por -final exato.
        return (1, -final, 0.0, preco)

    return sorted(candidates, key=_sort_key)


class CrossMarketplaceService:
    def __init__(self):
        # Singleton wrapper instances resolved via EngineFactory — safe for concurrent
        # reuse because the coordinator owns deduplication, the global concurrency
        # gate, and the persistent circuit-breaker state (not the engine instance).
        self._engine_instances: Dict[str, Any] = {
            key: engine_factory.get_engine(factory_brand_key)
            for key, (_, factory_brand_key) in _ENGINE_MAP.items()
        }
        # Display-name → engine instance map for _enrich_pdp_and_shipping lookup (Pitfall 5).
        self._by_display: Dict[str, Any] = {
            display_name: self._engine_instances[key]
            for key, (display_name, _) in _ENGINE_MAP.items()
        }

    def _active_engines(self) -> Dict[str, Any]:
        """Returns {display_name: engine} for all marketplace engines whose brand_key
        is currently active in brands.json.  Called per-request so deactivating a
        marketplace via PATCH /brands/{key}/active takes effect on the very next search
        (D-11) without a server restart.
        """
        active_brands = brand_service.list_brands(active_only=True)
        # Normalize each active brand_key to the canonical engine key so the
        # production value "mercado_livre" matches the _ENGINE_MAP key
        # "mercadolivre" (otherwise Mercado Livre is silently excluded).
        active_keys = {normalize_brand_key(b.brand_key) for b in active_brands}
        return {
            display_name: self._engine_instances[engine_key]
            for engine_key, (display_name, _) in _ENGINE_MAP.items()
            if engine_key in active_keys
        }



    async def compare_product(
        self,
        broad_query: str,
        strict_query: str,
        target_sku: Optional[str] = None,
        min_score: float = 70.0,
        zipcode: Optional[str] = None,
        brand_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Busca 'broad_query' nos marketplaces, filtra usando 'strict_query'
        e retorna no formato de contrato definido para o Frontend.
        """
        from config import settings
        
        # Usa o DEFAULT_CEP caso o zipcode não tenha sido fornecido
        target_zipcode = zipcode or settings.DEFAULT_CEP
        todos_produtos, errors = await self._fetch_all_engines(broad_query)

        # ====== OBTENÇÃO DO PRODUTO CANÔNICO ======
        official_title = strict_query
        reference_image_bytes = await self._fetch_reference_image(target_sku, brand_key=brand_key)

        # ====== NLP (TEXT MATCH) ======
        from services.nlp_service import nlp_service

        for p in todos_produtos:
            text_score = nlp_service.calculate_text_score(
                official_title, p.get("titulo", "")
            )
            p["text_match_score"] = text_score * 100.0  # Convert to 0-100
            p["image_match_score"] = 0.0
            # Score final provisório — apenas texto enquanto vision não rodou
            p["final_match_score"] = p["text_match_score"] * relevance_settings.FINAL_TEXT_WEIGHT

        # Agrupa por plataforma e seleciona os melhores candidatos para validação visual.
        top_candidates = relevance_gates.select_top_candidates_per_platform(todos_produtos)

        # ====== COMPUTER VISION (IMAGE MATCH) ======
        # Baixa imagens dos candidatos, roda CLIP em lote e compoe o score final
        # (gates em relevance_gates). Sem imagem de referencia / IA indisponivel,
        # mantem final = score de texto. Detalhe em _run_visual_matching.
        ref_embed = await self._run_visual_matching(top_candidates, reference_image_bytes)

        # Log dos scores
        for p in sorted(todos_produtos, key=lambda x: -x.get("final_match_score", 0))[
            :5
        ]:
            logger.debug(
                f"  [{p.get('final_match_score', 0):.0f}%] {p['plataforma']}: {p['titulo'][:50]} (Text: {p.get('text_match_score', 0):.0f}%, Img: {p.get('image_match_score', 0):.0f}%)"
            )

        # ====== RÉGUA DE CORTE (dinâmica, configurável via .env) ======
        actual_min_score = relevance_gates.compute_min_score_cutoff(
            min_score, ref_embed is not None
        )
        produtos_filtrados = [
            p
            for p in todos_produtos
            if p.get("final_match_score", 0) >= actual_min_score
            and p.get("preco", 0) > 0
            and passes_brand_gate(p.get("titulo", ""), official_title, relevance_settings.BRAND_GATE_ENABLED)
        ]

        # Fallback "produtos similares" por-plataforma (S1): plataformas que retornaram
        # produtos brutos mas zeraram no filtro estrito exibem similares (corte mais baixo)
        # em vez de sumirem. Detalhe e racional em relevance_gates.apply_similar_fallback.
        produtos_filtrados, used_similar_fallback = relevance_gates.apply_similar_fallback(
            produtos_filtrados, todos_produtos
        )

        # Reordena aplicando desempate visual cross-candidato entre candidatos da mesma
        # marca (MODEL-02): dentro de VISUAL_TIEBREAK_TEXT_WINDOW pontos do top-score da
        # marca, o maior image_match_score sobe. Fallback quando desativado ou sem sinal
        # de imagem: sorted(-final, preco) — idêntico ao comportamento anterior.
        produtos_filtrados = apply_visual_tiebreak(
            produtos_filtrados,
            window=relevance_settings.VISUAL_TIEBREAK_TEXT_WINDOW,
            enabled=relevance_settings.VISUAL_TIEBREAK_ENABLED,
        )

        # Limita o número de produtos por plataforma no resultado final (configurável).
        top_filtered = relevance_gates.cap_results_per_platform(produtos_filtrados)


        # ====== PDP FETCH & SHIPPING (SELLER ENRICHMENT & TIER 2 SHIPPING) ======
        # Busca seller/preco real via PDP e calcula frete (Tier 2). Ver _enrich_pdp_and_shipping.
        await self._enrich_pdp_and_shipping(top_filtered, target_zipcode)

        # Monta os dicts de saída (contrato do frontend) — ver relevance_gates.build_formatted_results.
        formatted_results = relevance_gates.build_formatted_results(top_filtered)

        # ====== DEDUPLICAÇÃO (DEDUP-01, DEDUP-02) ======
        # Colapsa produtos com a mesma (marketplace, url), incrementando variant_count.
        formatted_results = relevance_gates.dedup_results(formatted_results)



        # Buybox = mais barato, mas RESTRITO aos matches estritos quando existirem: um "similar"
        # de baixo score (fallback per-plataforma) não pode ser anunciado como o melhor preço do
        # produto buscado. mark_buybox_winner trata o caso "só similares" (usa todos).
        relevance_gates.mark_buybox_winner(formatted_results)

        # cheapest_price (métrica) segue a mesma regra: reflete o match estrito quando houver.
        pool_para_preco = [r for r in formatted_results if not r.get("is_similar")] or formatted_results
        cheapest_price = min((r["price"] for r in pool_para_preco), default=float("inf"))

        status = "success" if formatted_results or not errors else "error"

        from core.pricing import calculate_price_averages
        averages = calculate_price_averages(formatted_results)

        return {
            "target_sku": target_sku or "N/A",
            "search_query": strict_query,
            "vision_active": ref_embed is not None,
            "similar_fallback": used_similar_fallback,
            "status": status,
            "metrics": {
                "total_found_strict": len(formatted_results),
                "cheapest_price": cheapest_price
                if cheapest_price != float("inf")
                else 0.0,
                "average_price": averages["average_price"],
                "average_price_original": averages["average_price_original"],
            },
            "average_price": averages["average_price"],
            "average_price_original": averages["average_price_original"],
            "brand_averages": averages["brand_averages"],
            "results": formatted_results,
            "errors": errors,
        }


    async def _fetch_all_engines(self, broad_query: str):
        """
        Executa todos os engines em paralelo (timeout por motor) e normaliza a
        saida. Engines que retornam BrandSearchResult (ex: Netshoes) sao
        convertidos via relevance_gates.normalize_engine_products.

        Returns:
            (todos_produtos, errors) — lista de dicts de produto e lista de
            {"marketplace", "reason"}.
        """
        async def fetch(name, engine):
            try:
                timeout_limit = (
                    relevance_settings.ML_TIMEOUT_PLAYWRIGHT_SECONDS
                    if name in ("Mercado Livre", "Amazon")
                    else relevance_settings.ENGINE_DEFAULT_TIMEOUT_SECONDS
                )
                results = await asyncio.wait_for(
                    engine.search(
                        query=broad_query,
                        max_results=relevance_settings.CROSS_MAX_RESULTS_PER_ENGINE,
                    ),
                    timeout=timeout_limit,
                )
                return name, results, None
            except asyncio.TimeoutError:
                logger.warning(f"Timeout no motor {name}")
                return (
                    name,
                    [],
                    f"Timeout (busca demorou mais de {int(timeout_limit)}s)",
                )
            except Exception as e:
                logger.error(f"Erro no motor {name}: {e}")
                return name, [], str(e)

        raw_results = await asyncio.gather(
            *(fetch(name, engine) for name, engine in self._active_engines().items())
        )

        todos_produtos = []
        errors = []
        for name, prods, error in raw_results:
            # Handle BrandSearchResult from engines that return it (e.g. NetshoesEngine)
            if prods and hasattr(prods, "products"):
                error = error or getattr(prods, "error", None)
                prods = relevance_gates.normalize_engine_products(prods, name)

            if prods:
                logger.info(f"{name}: {len(prods)} produtos encontrados")
                todos_produtos.extend(prods)
            elif error:
                errors.append({"marketplace": name, "reason": error})
            else:
                logger.warning(f"{name}: 0 resultados para '{broad_query}'")
                errors.append(
                    {
                        "marketplace": name,
                        "reason": "Nenhum resultado encontrado (possível bloqueio ou sem estoque)",
                    }
                )

        return todos_produtos, errors

    async def _fetch_reference_image(self, target_sku: Optional[str], brand_key: Optional[str] = None):
        """
        Busca o produto canonico pelo SKU usando a engine da marca especificada e baixa os
        bytes da imagem oficial de referencia para o matching visual. Retorna os bytes ou
        None (sem SKU, sem imagem, ou erro).
        """
        if not target_sku:
            return None
        try:
            from services.engines.factory import engine_factory
            from services.image_ai_service import image_ai_service

            engine = engine_factory.get_engine(brand_key or "aramis")
            search_res = await engine.search(query=target_sku, max_results=1)

            if search_res and search_res.products and search_res.products[0].image_url:
                ref_image = search_res.products[0].image_url
                logger.info(f"Baixando imagem de referência oficial ({ref_image}) para marca '{brand_key or 'aramis'}'...")
                return await image_ai_service.download_image_bytes(ref_image)
        except Exception as e:
            logger.warning(f"Erro ao tentar obter produto canônico para marca '{brand_key}': {e}")
        return None

    async def _run_visual_matching(self, top_candidates, reference_image_bytes):
        """
        Computa o score visual (CLIP) dos candidatos e compoe o score final via
        relevance_gates.compute_final_match_score. Sem imagem de referencia ou com
        IA indisponivel/falha, mantem final = score de texto. Muta top_candidates
        in-place (image_match_score, final_match_score).

        Returns:
            ref_embed — embedding da imagem de referencia (ou None). O chamador usa
            ``ref_embed is not None`` como flag de "visao ativa".
        """
        ref_embed = None
        if not reference_image_bytes:
            for p in top_candidates:
                p["final_match_score"] = p["text_match_score"]
            return ref_embed

        from services.image_ai_service import image_ai_service, AI_AVAILABLE

        if AI_AVAILABLE:
            logger.info("Extraindo características visuais (embedding) da imagem de referência...")
            try:
                ref_embed = await image_ai_service.get_embedding_async(reference_image_bytes)
            except Exception as e:
                logger.warning(f"Erro ao inicializar IA visual: {e}")
                ref_embed = None
        else:
            logger.warning("Dependências de IA Visual (Torch/Transformers) não instaladas. Ignorando validação visual.")
            ref_embed = None

        if ref_embed is None:
            # Fallback se a IA visual falhar
            for p in top_candidates:
                p["final_match_score"] = p["text_match_score"]
            return ref_embed

        logger.info(f"Baixando imagens de {len(top_candidates)} candidatos em paralelo...")

        # VIS-01.5: Limite de Concorrência para evitar bloqueios WAF (Netshoes/ML)
        sem = asyncio.Semaphore(5)

        async def fetch_image(p):
            async with sem:
                if not p.get("imagem"):
                    return None
                import random
                await asyncio.sleep(random.uniform(0.1, 0.5))  # Delay sutil anti-bot
                return await image_ai_service.download_image_bytes(p["imagem"])

        # VIS-01: Download concorrente controlado
        downloaded_bytes = await asyncio.gather(*(fetch_image(p) for p in top_candidates))

        logger.info("Executando inferência Visual em lote com CLIP (HuggingFace)...")
        # VIS-02, VIS-03: Processamento em lote
        embeddings_batch = await image_ai_service.get_embeddings_batch_async(list(downloaded_bytes))

        for idx, p in enumerate(top_candidates):
            target_embed = embeddings_batch[idx]
            if target_embed is not None:
                image_score = await image_ai_service.calculate_score_from_embeddings(
                    ref_embed, target_embed
                )
                p["image_match_score"] = image_score * 100.0
            else:
                p["image_match_score"] = 0.0

            # Regua de decisao (gates) em services.relevance_gates: texto forte domina;
            # gate anti-WAF salva imagem indisponivel; senao media ponderada (.env).
            p["final_match_score"] = relevance_gates.compute_final_match_score(
                p["text_match_score"], p["image_match_score"]
            )

        return ref_embed

    async def _enrich_pdp_and_shipping(self, top_filtered, target_zipcode):
        """
        Para cada produto, busca seller/preco autoritativo via PDP e calcula o frete
        (Tier 2) usando o engine da plataforma. Muta os produtos in-place.
        """
        async def fetch_pdp_seller_and_shipping(p):
            plat = p["plataforma"]
            if plat in self._by_display:
                engine = self._by_display[plat]
                # 1. Fetch Seller + preço autoritativo da PDP
                try:
                    details = await engine.get_product_details(p["url"])
                    # Pitfall 4 (CR-02): a degraded read from the resilient wrapper
                    # is a TRUTHY dict carrying `_marketplace_read_state` — never
                    # treat a blocked/unsupported read as real PDP data, mirroring
                    # the gate every other caller (shipping/*.py, routes_search.py)
                    # already applies before the truthiness check.
                    detail_read_state = (
                        details.get("_marketplace_read_state") if isinstance(details, dict) else None
                    )
                    if detail_read_state not in {"blocked", "unsupported"} and details:
                        pdp_seller = details.get("seller")
                        current_seller = p.get("seller")
                        # Regra de precedência: PDP só sobrescreve quando traz lojista REAL.
                        # Se a PDP retornar o default do marketplace (ou None/vazio),
                        # o seller real obtido na listagem é preservado.
                        if pdp_seller and not is_marketplace_default(pdp_seller, plat):
                            p["seller"] = pdp_seller
                        # else: mantém current_seller (real da listagem ou default)

                        # Preço da PDP é a fonte da verdade da variante/vendedor exibidos;
                        # corrige divergência com o preço da listagem de busca.
                        pdp_price = details.get("price")
                        if isinstance(pdp_price, (int, float)) and pdp_price > 0:
                            p["preco"] = pdp_price
                except Exception as e:
                    logger.warning(f"Erro ao buscar seller/preço via PDP para {plat}: {e}")

                # 2. Fetch Shipping (Tier 2)
                try:
                    shipping_info = await engine.calculate_shipping(p, target_zipcode)
                    # Pitfall 4 (CR-02): same gate as above — ResilientMarketplaceEngine
                    # never returns None anymore, a blocked/unsupported read comes back
                    # as a truthy dict carrying `_marketplace_read_state`. Check it
                    # before the truthiness check so the UI's blocked indicator
                    # (`isBlocked = p._shipping_state === 'blocked'` in App.tsx) is not
                    # silently swallowed into "no shipping info, not free".
                    shipping_read_state = (
                        shipping_info.get("_marketplace_read_state")
                        if isinstance(shipping_info, dict)
                        else None
                    )
                    if shipping_read_state in {"blocked", "unsupported"}:
                        # Blocked (circuit open) or unsupported (engine's Tier 2 is a
                        # documented no-op, e.g. Amazon): never fake a free/zero
                        # shipping value for either (T-42-03).
                        p["_shipping_state"] = shipping_read_state
                    elif shipping_info:
                        p["is_free_shipping"] = shipping_info.get("is_free_shipping", False)
                        p["shipping_price"] = shipping_info.get("shipping_price")
                        # Surface delivery-time (prazo) onto the product when the
                        # engine result carries it (T-42-03 / FRET-08 UI surface).
                        # .get() guards keep a missing field degrading to cost-only.
                        if "estimated_delivery_days" in shipping_info:
                            p["estimated_delivery_days"] = shipping_info.get("estimated_delivery_days")
                        if "delivery_raw_text" in shipping_info:
                            p["shipping_raw_text"] = shipping_info.get("delivery_raw_text")
                except Exception as e:
                    logger.debug(f"Erro ao calcular frete para {plat}: {e}")

        logger.info(
            f"Buscando seller real via PDP e calculando frete para {len(top_filtered)} produtos com CEP {target_zipcode}..."
        )
        await asyncio.gather(*(fetch_pdp_seller_and_shipping(p) for p in top_filtered))




cross_marketplace_service = CrossMarketplaceService()
