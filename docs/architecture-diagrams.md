# Diagramas de Arquitetura (corrigidos)

> Gerado a partir de auditoria do diagrama original contra o código real (2026-07-11).
> Cada correção relevante está anotada com `%% FIX:` no bloco Mermaid correspondente.
> Diagrama 8 (Extração de Banners) foi criado do zero em 2026-07-11 — não existia no material original, só aparecia como componente (diagrama 1) e entidades (diagrama 2).

## 1. Visão geral de componentes

```mermaid
flowchart TB
    subgraph FRONTEND["🖥 FRONTEND — Vite + React + TypeScript"]
        AppTsx["App.tsx (177KB — SPA monolítico)"]
        ApiClient["api/ — HTTP Client (Axios + interceptors)"]
        Stores["stores/ — State Management"]
        AppCss["App.css — Design System"]
        AppTsx --> ApiClient
        AppTsx --> Stores
    end

    subgraph GATEWAY["🔐 API GATEWAY — FastAPI + Uvicorn"]
        Auth["auth.py — verify_api_key\n%% FIX: é uma FastAPI Dependency\n(Depends), NÃO um @app.middleware.\nHá um middleware real em app.py,\nmas só adiciona headers no-cache."]
        WS["WebSocket /ws/{job_id}\n%% FIX: definido em api/__init__.py,\nnão em auth.py. Autenticação via\n?api_key= query param + verify_ws_api_key(),\npode fechar com code=4003."]

        subgraph ROUTES["📡 REST Routes"]
            RSearch["routes_search.py\nPOST/GET /search\nPOST /search/export\nPOST /search/cross-marketplace\nPOST /search/cross-marketplace/export\nPOST /search/calculate-shipping\nPOST /search/calculate-shipping-vtex\nPOST /search/calculate-shipping-brand\nGET /search/config"]
            RBrands["routes_brands.py\nPOST /brands/\nPOST /brands/identify\nGET /brands/\nGET /brands/{key}/discover\nGET/PUT /brands/{key}/mappings\nPATCH /brands/{key}/active\nDELETE /brands/{key}"]
            RCategory["routes_category.py\nGET /brands/{brand}/categories\nGET /canonical-categories\nPOST /category-preview\nPOST /scrape-category\nPOST /scrape-category-multi"]
            RMonitor["routes_monitor.py\nPOST /monitor/category\nGET /monitor/categories\nDELETE /monitor/category/{id}\nGET /monitor/category/{id}/products\nPOST /monitor/category/{id}/products/{scan_product_id}/reviews"]
            RProduct["routes_product.py\nPOST /monitor/start\nPOST /monitor/stop/{job_id}\nPOST /monitor/resume/{job_id}\nGET /monitor/history/{job_id}\nGET /monitors\nDELETE /monitor/{job_id}"]
            RHistory["routes_history.py\nGET /history\nGET /history/{job_id}\nDELETE /history/{job_id}"]
            RBanners["routes_banners.py\n%% FIX: NÃO existe POST /banners/scan.\nAPI real de 10 rotas:\nPOST /banners/jobs\nGET /banners/jobs/{run_id}\nPOST /banners/jobs/{run_id}/stop\nPOST /banners/jobs/{run_id}/approve\nGET /banners/history[/{run_id}]\nDELETE /banners/history/{run_id}\nGET /banners/assets/{run_id}/{banner_id}\nGET /banners/screenshots/{run_id}/{brand_key}\nGET /banners/runs/{run_id}/reports/{format}"]
            RJobs["routes_jobs.py\nDELETE /jobs/{id}"]
            RSystem["routes_system.py\nGET /system/status\nPOST /system/notifications/{id}/read"]
        end
    end

    subgraph SERVICES["⚙️ SERVICES — Camada de Negócio"]
        subgraph BIZ["📦 Business Services"]
            BrandSvc["BrandService — CRUD de marcas"]
            CategoryMonitorSvc["CategoryMonitorService\nAPScheduler a cada 10 min\n(app.py: AsyncIOScheduler.add_job)\n+ 1a varredura via BackgroundTasks no create"]
            PriceMonitorSvc["PriceMonitorService\nasyncio loop interno por job\n(_monitor_loop, com jitter\n5-45s inicial e ±5% por ciclo)"]
            Orchestrator["run_orchestrator()\n%% FIX: função async, não classe\nPipeline bulk single-brand"]
            OrchestratorMulti["run_multi_orchestrator()\n%% FIX: função async, não classe\nPipeline bulk multi-brand\nasyncio.gather por marca"]
            SearchHistorySvc["SearchHistoryService → search_history.json"]
            NotificationStore["NotificationStore — in-memory (max 50)"]
            BannerSvcs["BannerExtraction/Job/Report/StorageService\n%% FIX: único pipeline bulk que NÃO é asyncio-nativo —\nrun_job faz asyncio.to_thread(_run_sync) rodando\nPlaywright SÍNCRONO numa thread separada.\nReusa JOB_CANCEL_FLAGS do Job Manager (mesmo dict),\nmas de BrowserManager reusa só a constante\nCHROMIUM_ARGS, não a classe/pool.\nNÃO usa Intelligence Layer (sem NLP/CLIP/OCR)."]
            ReviewSvc["ReviewService\nMulti-provider: Trustvox + VTEX Native"]
            VtexClient["VtexApiClient\nSearch / Catalog / Shipping Simulation"]
            CategoryMapping["CategoryMapping — resolução de categorias\n%% FIX: não é arquivo próprio,\nvive embutido em brands.json[brand].mappings[]"]
            CrossMarketplaceSvc["CrossMarketplaceService\ncompare_product()"]
        end

        subgraph INTEL["🧠 Intelligence Layer"]
            NlpSvc["NLP Service\nFuzzy Matching (WRatio, token_sort_ratio,\npartial_token_set_ratio via rapidfuzz)\n+ brand_is_present()"]
            ImageAiSvc["Image AI Service\nCLIP Embeddings (HuggingFace transformers)\nget_embedding_async()"]
            OcrSvc["OCR Service — EasyOCR"]
            RelevanceGates["relevance_gates.py\nScore Cutoff, Similar Fallback,\nDedup + Buybox\n%% FIX: Brand Gate e Visual Tiebreak\nNÃO estão aqui — vivem em\ncross_marketplace_service.py"]
        end

        subgraph SHIPPING["🚚 Shipping Layer"]
            BaseShipping["BaseShipping\n%% FIX: nome real, não 'ShippingCalculator'"]
            ShippingResolver["ShippingResolver\n%% FIX: NÃO tem branch VTEX.\nVTEX shipping vive fora deste pacote,\nem vtex_shipping.py + VtexApiClient.simulate_shipping()"]
            ShopifyShip["Shopify Shipping"]
            WakeShip["Wake Shipping"]
            MlShip["ML Shipping"]
            NetshoesShip["Netshoes Shipping"]
            AmazonShip["Amazon Shipping"]
            ShippingResolver --> ShopifyShip & WakeShip & MlShip & NetshoesShip & AmazonShip
            BaseShipping -.-> ShippingResolver
        end
    end

    subgraph ENGINES["🏭 Engine Layer"]
        BaseEngine["BaseEngine (ABC)\nvalidate_single() → validate_and_filter()\n(herdado por todos os engines)"]
        VtexEngine["VTEXEngine"]
        SfccEngine["SFCCEngine"]
        WakeEngine["WakeEngine\n%% FIX: gate extra filter_mens_fashion()\nantes de validate_single()"]
        ShopifyEngine["ShopifyEngine"]
        ZaraEngine["ZaraEngine"]
        MlEngine["MercadoLivreEngine"]
        NetshoesEngine["NetshoesEngine"]
        AmazonEngine["AmazonEngine"]
        EngineFactory["EngineFactory\nsearch_all_brands()\n%% FIX: 1 task asyncio POR MARCA,\nnão 1 chamada por tipo de engine"]
        EngineFactory --> VtexEngine & SfccEngine & WakeEngine & ShopifyEngine & ZaraEngine & MlEngine & NetshoesEngine & AmazonEngine
        BaseEngine -.-> VtexEngine & SfccEngine & WakeEngine & ShopifyEngine & ZaraEngine & MlEngine & NetshoesEngine & AmazonEngine
    end

    subgraph CORE["🔧 CORE — Infraestrutura"]
        Models["models.py — Pydantic Contracts"]
        BrowserManager["BrowserManager\n%% FIX: NÃO é pool persistente —\nabre um Chromium novo por chamada\ne fecha no finally (fetch_html()).\nget_browser() cacheado existe mas não é usado\nno caminho de scraping."]
        SessionManager["SessionManager\n%% FIX: usa só aiohttp.\ncurl_cffi está em\ninfrastructure/http/session_factory.py.\nhttpx NÃO é usado no projeto."]
        Config["config.py\n%% FIX: fica em backend/config.py\n(raiz do pacote), não em core/\nSettings + RelevanceSettings"]
        WsManager["WebSocket Manager (websocket.py)"]
        JobManager["Job Manager — cancel flags (job_manager.py)"]
        Identity["Identity — User-Agent Rotation"]
    end

    subgraph PERSIST["💾 PERSISTÊNCIA — Arquivos JSON Locais"]
        BrandsJson["data/brands.json"]
        SearchHistJson["data/search_history.json"]
        PriceMonJson["data/price_monitors.json"]
        MonCatJson["data/monitored_categories.json"]
        BannersDir["data/banners/"]
        AuditsDir["data/audits/"]
        NlpVocabJson["data/nlp_vocabulary.json"]
        ShipCacheJson["data/shipping_matrix_cache.json"]
        XlsxExports["*.xlsx — Exports"]
    end

    subgraph EXTERNAL["🌐 EXTERNAL APIs"]
        VtexApi["VTEX APIs"]
        SfccApi["Salesforce Commerce Search API"]
        WakeApi["Wake Commerce GraphQL API"]
        ShopifyApi["Shopify Search/Products API"]
        ZaraApi["Zara API"]
        MlHtml["Mercado Livre — HTML Scraping"]
        NetshoesHtml["Netshoes — HTML Scraping"]
        AmazonHtml["Amazon — HTML Scraping"]
        TrustvoxApi["Trustvox Reviews API"]
        VtexReviewsApi["VTEX Reviews API"]
    end

    ApiClient --> Auth
    Auth --> ROUTES
    ROUTES --> BIZ
    CrossMarketplaceSvc --> INTEL
    %% FIX: BIZ --> INTEL genérico superestimava o acoplamento.
    %% Confirmado (auditoria do diagrama 8): BannerSvcs NÃO toca
    %% Intelligence Layer (sem NLP/CLIP/OCR) — só CrossMarketplaceSvc
    %% realmente depende dela.
    BIZ --> ENGINES
    BIZ --> SHIPPING
    ENGINES --> EXTERNAL
    SHIPPING --> VtexApi
    BIZ --> PERSIST
    CORE -.-> BIZ
    CORE -.-> ENGINES
```

---

## 2. Modelo de dados (ERD)

```mermaid
erDiagram
    BRANDS_JSON {
        string brand_key PK "ex: aramis, reserva"
        string brand_name
        string domain
        string engine "vtex|shopify|sfcc|wake|zara|mercadolivre|netshoes|amazon (str livre, não enum)"
        boolean is_active
        string review_provider "trustvox|vtex_native|none (str livre)"
        string review_provider_evidence "FIX: campo real ausente no diagrama original"
        string review_unsupported_reason "FIX: campo real ausente no diagrama original"
        string review_store_id
        string vtex_account
        string logo_url
        string search_url_template
        string proxy_url
        string wake_access_token
    }

    CATEGORY_MAPPING {
        string canonical_slug "FIX: NÃO é PK global — se repete entre marcas.\nChave natural real = (brand_key, canonical_slug).\nVive embutido em brands.json[brand].mappings[], não em arquivo próprio"
        string vtex_fq_path
        string label
    }

    SEARCH_HISTORY_JSON {
        string job_id PK
        string query
        string target_sku
        string type "search|cross"
        string_array brands
        string status "PENDING|COMPLETED|FAILED"
        string created_at
        json results "FIX: shape difere totalmente por type — ver SEARCH_RESULT vs CROSS_RESULT"
        string error
    }

    SEARCH_RESULT {
        string brand
        string product_name
        string url
        float price_full
        float price_discount
        boolean price_discount_is_delta "FIX: campo real ausente"
        string image_url
        string category
        boolean available
        string product_code
        float rating
        int review_count
        string_array available_colors
        string_array available_sizes
        string sku_id
        string seller_id
        string scan_product_id "FIX: campo real ausente"
        json shipping "FIX: campo real ausente (ShippingInfo aninhado)"
        json_array shipping_options "FIX: campo real ausente"
        float landed_price "FIX: campo real ausente (calculado)"
        json_array review_comments "FIX: campo real ausente"
        string _obs "usado apenas quando type=search"
    }

    CROSS_RESULT {
        string marketplace "FIX: entidade nova — type=cross usa shape TOTALMENTE diferente de SEARCH_RESULT"
        string seller
        string title
        float price
        float text_match_score
        float image_match_score
        float final_match_score
        boolean is_buybox_winner
        int variant_count
        string shipping_state
    }

    PRICE_MONITORS_JSON {
        string job_id PK
        string url
        string brand
        int interval_minutes
        int duration_hours
        string start_time
        float last_price
        float last_price_original
        float last_price_discount "FIX: campo real ausente no diagrama original"
        string last_status "ok|blocked|error|null"
        string last_error
        string last_checked_at
        boolean active
        string image_url
        string product_name
        string_array available_colors
        string_array available_sizes
    }

    PRICE_HISTORY_ENTRY {
        string timestamp
        float price
        float price_original
        float price_discount "FIX: campo real ausente no diagrama original"
        boolean available
        string_array available_colors
        string_array available_sizes
    }

    NLP_VOCABULARY_JSON {
        string_array colors "FIX: campos totalmente renomeados — nenhum dos 3 campos originais bate com o arquivo real"
        string_array brand_names
        string_array category_words
        string_array stop_words
        string_array noise_words
        string_array known_brands_for_brand_detection
        json category_synonyms
    }

    MONITORED_CATEGORIES_JSON {
        string id PK "FIX: PK real é 'id' (UUID), não 'category_slug'. Arquivo é ARRAY, não dict"
        string url
        string brand "FIX: string singular, não brands[]"
        string status
        string last_scraped_at "FIX: nome real, não 'last_scan'"
        json last_stock_summary "FIX: objeto aninhado, não 'product_count' flat"
        int last_map_violation_count "FIX: campo real ausente"
    }

    SHIPPING_MATRIX_CACHE_JSON {
        string cache_key PK "FIX: formato real é '{product_url}|{cep}', não 'brand:sku:cep'"
        json result "FIX: nome real do campo, não 'shipping_data'. Contém region/capital/cep/state/shipping/message/cached"
        string checked_at "FIX: nome real, não 'cached_at'"
    }

    BANNER_RUN {
        string run_id PK
        string_array selected_brands
        string status "RUNNING|REVIEW|COMPLETED|PARTIAL|CANCELLED|FAILED"
        json brand_progress
        string created_at
        string approved_at
        string updated_at "FIX: campo real ausente"
        string error "FIX: campo real ausente"
    }

    BANNER_CANDIDATE {
        string banner_id PK "brand-order-sha256"
        string brand_key
        string brand_name
        int slide_order
        string friendly_filename
        json asset "sha256, extension, content_type, byte_count (FIX: não 'bytes')"
        string source_url
        string rendered_url "FIX: campo real ausente (distinto de source_url)"
        string click_url
        string alt_text
        string dom_kind "img|background|background-pseudo (FIX: str livre, não enum enforced, default 'img')"
        int rendered_width
        int rendered_height
        int natural_width "FIX: campo real ausente"
        int natural_height "FIX: campo real ausente"
        string captured_at "FIX: campo real ausente"
        boolean approved
    }

    BANNER_VIDEO_SLIDE {
        string brand_key
        int slide_order
        string source_url
    }

    STOCK_SUMMARY {
        string scan_id "FIX: frequentemente null na prática — identidade real é monitor_id"
        string brand "FIX: campo real ausente"
        string monitor_id "FIX: campo real ausente"
        int total_products
        int in_stock_count "FIX: nome real, não 'available_count'"
        int out_of_stock_count "FIX: nome real, não 'unavailable_count'"
        int unknown_stock_count "FIX: campo real ausente"
        int verified_stock_count "FIX: campo real ausente"
        float rupture_pct "FIX: campo real ausente"
        string scanned_at "FIX: nome real, não 'generated_at'"
    }

    CATEGORY_SCAN_SUMMARY {
        string scan_id PK "formato real 'uuid:brand' — FIX: é registro POR MARCA, não agregado por scan"
        string brand
        int total_products
        int in_stock_count
        int out_of_stock_count
        float rupture_pct
        string scanned_at
        string _obs "FIX: shape documentado originalmente (brand_results json + category + completed_at) não existe. Real = lista plana, mesmo shape de STOCK_SUMMARY"
    }

    RAW_PRODUCT_BRONZE {
        string url
        string brand
        string raw_title
        string raw_description
        float price_full
        float price_discount
        boolean price_discount_is_delta
        boolean stock_availability "opcional (Optional[bool]), não bool obrigatório"
        string category
        string sub_category
        string composition
        string product_code
        string_array raw_colors "FIX: campo real ausente"
        string_array raw_sizes "FIX: campo real ausente"
        string_array available_colors
        string_array available_sizes
        float rating
        int review_count
        string image_url
        json specifications "FIX: campo real ausente"
        string scan_product_id "FIX: campo real ausente"
        json shipping "FIX: campo real ausente (ShippingInfo aninhado)"
        boolean is_free_shipping "FIX: campo real ausente"
        float shipping_price "FIX: campo real ausente"
        string reviews_state "FIX: campo real ausente"
        json_array review_comments "FIX: campo real ausente"
        float landed_price "calculado"
    }

    SHIPPING_INFO {
        float price
        string status "FIX: str livre (visto: Gratis, Calculado no checkout), não enum fechado Disponível|Indisponível"
        int estimated_delivery_days
        string raw_text
        string service_name
        string service_id
        string estimate_display
        string estimate_unit "bd|d|h|m"
        boolean is_free_shipping
    }

    REVIEW_COMMENT {
        string review_id PK
        float rating
        string title
        string text
        string author
        string created_at
        string source_provider "trustvox|vtex_native (str livre, não enum enforced)"
        string source_ref
    }

    BRANDS_JSON ||--o{ CATEGORY_MAPPING : "mappings[] embutido"
    SEARCH_HISTORY_JSON ||--o{ SEARCH_RESULT : "results[] quando type=search"
    SEARCH_HISTORY_JSON ||--o{ CROSS_RESULT : "results[] quando type=cross"
    PRICE_MONITORS_JSON ||--o{ PRICE_HISTORY_ENTRY : history
    MONITORED_CATEGORIES_JSON ||--o{ RAW_PRODUCT_BRONZE : "monitored_products_{id}.json"
    MONITORED_CATEGORIES_JSON ||--o{ CATEGORY_SCAN_SUMMARY : gera
    BANNER_RUN ||--o{ BANNER_CANDIDATE : banners
    BANNER_RUN ||--o{ BANNER_VIDEO_SLIDE : video_slides
    RAW_PRODUCT_BRONZE ||--o| SHIPPING_INFO : shipping
    RAW_PRODUCT_BRONZE ||--o{ REVIEW_COMMENT : review_comments
```

---

## 3. Sequence: Category Monitor

```mermaid
sequenceDiagram
    actor Usuario as Usuário
    participant FE as Frontend
    participant API as FastAPI (routes_monitor.py)
    participant Svc as CategoryMonitorService
    participant Factory as EngineFactory
    participant Engine
    participant JSON as monitored_categories.json

    Usuario->>FE: Adiciona URL de categoria
    FE->>API: POST /monitor/category (url, brand)
    API->>API: Gera monitor_id
    API->>JSON: Salva monitor (array de monitores)
    API->>Svc: background_tasks.add_task(run_category_scan)
    Note right of API: 1ª varredura roda logo após o create,\nFORA do ciclo do APScheduler
    API-->>FE: { id, url, status: "active" }

    loop A cada 10 minutos (APScheduler — app.py, AsyncIOScheduler.add_job)
        Svc->>JSON: load_monitored_categories()
        loop Para cada monitor ativo
            Svc->>Factory: get_engine(brand)
            Factory-->>Svc: Engine
            Svc->>Engine: run_bulk_scrape(url)
            Engine-->>Svc: yield RawProductBronze
            Note right of Svc: Cap de 1000 é um "break" no loop\nconsumidor do Service, não um limite\nimposto pelo engine
            Svc->>Svc: _ensure_scan_product_ids()
            Note right of Svc: FIX: função privada\n(_ensure_scan_product_ids), não pública
            Svc->>JSON: Salva monitored_products_{id}.json
            Svc->>JSON: Atualiza last_scraped_at
        end
        Svc->>Svc: notification_store.set_last_category_run()
        Note right of Svc: FIX: chamado 1x por CICLO do scheduler\n(depois do loop de todas as categorias),\nnão por monitor individual
    end

    Usuario->>FE: Acessa dashboard da categoria
    FE->>API: GET /monitor/category/{id}/products
    API->>JSON: Lê monitored_products_{id}.json
    API-->>FE: Lista de produtos

    Usuario->>FE: Expande avaliações de um produto
    FE->>API: POST /monitor/category/{id}/products/{scan_product_id}/reviews
    API->>API: fetch_scan_product_review_comments()
    API-->>FE: ReviewCommentsResult (Trustvox/VTEX)
    FE-->>Usuario: JSON com comentários e nota
```

---

## 4. Sequence: Price Monitor

```mermaid
sequenceDiagram
    actor Usuario as Usuário
    participant FE as Frontend
    participant API as FastAPI (routes_product.py)
    participant Svc as PriceMonitorService
    participant Factory as EngineFactory
    participant Engine
    participant JSON as price_monitors.json
    participant WS as WebSocket Manager

    Usuario->>FE: Adiciona produto ao monitoramento
    FE->>API: POST /monitor/start (url, brand, interval, duration)
    API->>Svc: start_monitor(job_id, url, brand, interval, duration)
    Svc->>Svc: Detecta duplicata via normalize_url(url) +\nbrand.lower() == brand.lower()
    Note right of Svc: FIX: comparação normaliza URL e\nignora case da marca, não é match flat

    alt Já existe monitor para esta URL+marca
        Svc-->>API: { job_id existente, status: "already_active" | "reactivated" }
    else Novo monitor
        Svc->>JSON: Persiste PriceMonitorConfig
        Svc-->>API: { job_id, status: "created" }
    end
    API-->>FE: { job_id, status, config }

    Svc->>Svc: asyncio.create_task(_monitor_loop)
    loop A cada N minutos (asyncio loop interno, com jitter)
        Note right of Svc: FIX: jitter inicial 5-45s (se já há last_price)\n+ jitter ±5% em cada sleep — não é intervalo fixo
        Svc->>Factory: get_engine(brand)
        Factory-->>Svc: Engine
        Svc->>Engine: get_pdp_product(url)
        Engine-->>Svc: preço, disponibilidade, cores, tamanhos
        Svc->>Svc: Compara price + price_original + price_discount +\navailable_colors + available_sizes
        Note right of Svc: FIX: has_change considera 5 campos,\nnão só o preço

        alt Mudança detectada
            Svc->>JSON: Adiciona PriceHistoryEntry
            Svc->>JSON: Atualiza last_price*, last_status = "ok"
        else Sem mudança
            Svc->>JSON: last_status = "ok"
        else Falha de extração (anti-bot etc.)
            Svc->>JSON: last_status = "blocked"
            Note right of Svc: FIX: estado real e frequente\n(ex.: Netshoes/Akamai) — ausente do\ndiagrama original
        else Exceção inesperada
            Svc->>JSON: last_status = "error"
        end
        Svc->>WS: send_message (price_update / info / error / done)
        Note right of WS: FIX: push em tempo real via WebSocket,\nem paralelo ao polling REST — ausente\ndo diagrama original
    end

    Usuario->>FE: Consulta histórico
    FE->>API: GET /monitor/history/{job_id}
    API->>Svc: get_monitor(job_id)
    Svc->>JSON: Lê dados
    Svc-->>API: PriceMonitorConfig com history[]
    API-->>FE: JSON com gráfico de preços

    Note over API: Outros endpoints reais:<br/>POST /monitor/stop/{job_id}<br/>POST /monitor/resume/{job_id}<br/>GET /monitors<br/>DELETE /monitor/{job_id}
```

---

## 5. Sequence: Orquestração Multi-marca

```mermaid
sequenceDiagram
    actor Usuario as Usuário
    participant FE as Frontend
    participant WS as WebSocket
    participant API as FastAPI (routes_category.py)
    participant Orch as OrchestratorMulti (run_multi_orchestrator)
    participant Factory as EngineFactory
    participant Engine as Engine (VTEX/SFCC/Wake/...)
    participant Excel as Excel Export

    Usuario->>FE: Seleciona categoria + marcas
    FE->>API: POST /scrape-category-multi (brands[], category_slug?, brand_category_map?)
    API->>API: Gera job_id + cancel_event = asyncio.Event()\n(JOB_CANCEL_FLAGS[job_id])
    API->>Orch: background_tasks.add_task(run_multi_orchestrator,\njob_id, brand_url_map, category_label, cancel_event)
    Note right of API: FIX: agendado via BackgroundTasks\n(roda após a resposta HTTP ser enviada),\nnão chamado sincronamente.\nAssinatura real tem 4 parâmetros, não 2.
    API-->>FE: { job_id }
    FE->>WS: Conecta ws://host/ws/{job_id}?api_key=...
    Note right of WS: FIX: exige query param api_key.\nSe inválido, fecha com code=4003.

    par Pipeline por Marca (asyncio.gather, paralelo real)
        Orch->>Factory: get_engine(brand_key)
        Factory-->>Orch: Engine instanciado
        Orch->>Engine: run_bulk_scrape(category_url)
        Note right of Engine: FIX: chamado UMA VEZ por marca —\npaginação (chunk_size=50) é interna\nao engine (scrape_category_paged),\nnão um loop externo por página
        loop Produtos (async generator, todas as páginas)
            Engine-->>Orch: yield RawProductBronze
            Orch->>Engine: validate_single(produto)
            Note right of Engine: FIX: não é um serviço "Quality Gates"\nseparado — é método herdado de\nBaseEngine (validate_and_filter).\nWake tem passo extra filter_mens_fashion()\nantes de validate_single().
            Engine-->>Orch: dict validado ou None (rejeitado)
            Orch->>WS: Envia log de progresso
        end
    end

    Orch->>Orch: run_in_executor(consolidate_and_save)
    Note right of Orch: FIX: roda em thread pool executor\npara não bloquear o event loop
    Orch->>Excel: build_canonical_export_dataframe(all_products)
    Orch->>Excel: df.to_excel(arquivo_saida)
    Orch->>WS: { type, valid_products, total_success, total_errors,\noutput_file, brands_completed, message }
    Note right of WS: FIX: type varia — "done", "cancelled_done",\n"cancelled" ou "error_done" (sem output_file\nse zero produtos coletados), não só "done"
    WS-->>FE: Mensagem de conclusão
    FE-->>Usuario: Link para download do Excel
```

---

## 6. Sequence: Busca (single/multi-brand search)

```mermaid
sequenceDiagram
    actor Usuario as Usuário
    participant FE as Frontend (Vite/React)
    participant API as FastAPI Gateway
    participant Hist as SearchHistoryService
    participant Factory as EngineFactory
    participant Engines as Engines (1 instância por marca)
    participant Sort as Result Sort

    Usuario->>FE: Digita termo de busca + seleciona marcas
    FE->>API: POST /search (query, brands[], max_per_brand, sort, zipcode, include_shipping)
    API->>API: verify_api_key
    Note right of API: FIX: dependency global do router\n(api/__init__.py), não código\ninline em routes_search.py
    API->>Hist: create_job(job_id, query, brands, type=search)
    API->>Factory: search_all_brands(query, brands, max_per_brand, sort, zipcode, include_shipping)

    par Busca Paralela (asyncio.gather — 1 task POR MARCA)
        Note right of Factory: FIX: NÃO é "1 chamada por tipo de engine".\nget_engine(brand_key) cria uma instância nova\npor marca (ex.: 3 VTEXEngine distintos para\nAramis/Reserva/Foxton) e cada uma roda\n.search() em paralelo no mesmo gather.
        Factory->>Engines: engine("aramis").search(query)
        Factory->>Engines: engine("reserva").search(query)
        Factory->>Engines: engine("foxton").search(query)
        Factory->>Engines: engine("lacoste").search(query)
        Factory->>Engines: engine("...").search(query)
        Engines-->>Factory: BrandSearchResult (products[]) — 1 por marca
    end

    Factory->>Sort: sort_brand_products(products, sort_criteria)
    Sort-->>Factory: Produtos ordenados por marca
    Factory-->>API: List[BrandSearchResult]
    API->>Hist: update_job(job_id, status=COMPLETED, results)
    API-->>FE: JSON { results: [...], searched_at }
    FE-->>Usuario: Grid comparativo com produtos por marca
```

---

## 7. Sequence: Cross-marketplace

```mermaid
sequenceDiagram
    actor Usuario as Usuário
    participant FE as Frontend
    participant API as FastAPI (routes_search.py)
    participant Hist as SearchHistoryService
    participant VtexClient as VtexApiClient
    participant Cross as CrossMarketplaceService
    participant ML as Mercado Livre Engine
    participant NS as Netshoes Engine
    participant AZ as Amazon Engine
    participant Nlp as NLP Service
    participant Img as Image AI (CLIP)
    participant Gates as relevance_gates.py
    participant Ship as Shipping Calculator

    Usuario->>FE: Seleciona produto Aramis (SKU) + CEP
    FE->>API: POST /search/cross-marketplace (target_sku, search_query?, broad_query?, min_score, zipcode)
    API->>API: verify_api_key

    alt search_query não fornecida
        API->>VtexClient: search(target_sku, max=1) na Aramis
        VtexClient-->>API: Produto de referência (nome, imagem, preço)
    end
    API->>API: Monta strict_query (nome completo) e\nbroad_query (categoria + tokens + Aramis)
    API->>API: remove_colors(broad_query) + remove_colors(strict_query)
    API->>Hist: create_job(job_id, query, brands=[ml,ns,az], type=cross, target_sku)
    API->>Cross: compare_product(broad_query, strict_query, target_sku, min_score, zipcode)
    Note right of API: FIX: compare_product também recebe\nmin_score, ausente no diagrama original

    par Fetch All Engines (timeout por motor — valores confirmados corretos)
        Cross->>ML: search(broad_query, max=30) [timeout=100s]
        Note right of ML: 100s calibrado deliberadamente para o\nfallback curl_cffi→Playwright do ML\n(~92s pior caso) — config.py:\nML_TIMEOUT_PLAYWRIGHT_SECONDS
        Cross->>NS: search(broad_query, max=30) [timeout=30s]
        Cross->>AZ: search(broad_query, max=30) [timeout=30s]
        ML-->>Cross: Produtos brutos
        NS-->>Cross: Produtos brutos
        AZ-->>Cross: Produtos brutos
    end

    Cross->>ML: search(target_sku) → imagem de referência Aramis
    Note right of Cross: FIX: essa busca (via VTEXEngine("aramis"),\nclasse DIFERENTE da usada no passo inicial\nVtexApiClient) roda logo após o fetch dos\nengines, ANTES do scoring NLP — não\numa etapa de CV tardia

    Note over Cross: Etapa 1: NLP Scoring
    Cross->>Nlp: calculate_text_score() p/ cada produto
    Nlp-->>Cross: text_match_score (0-100)
    Cross->>Gates: select_top_candidates_per_platform()

    Note over Cross: Etapa 2: Computer Vision
    Cross->>Img: get_embedding_async(ref_image) + batch embeddings candidatos
    Note right of Img: FIX: nome real é get_embedding_async,\nnão get_embedding
    Img-->>Cross: image_match_score (0-100)
    Cross->>Gates: compute_final_match_score(text, image)

    Note over Cross: Etapa 3: Score Cutoff + Brand Gate (mesmo filtro) → Similar Fallback → Visual Tiebreak
    Cross->>Gates: compute_min_score_cutoff()
    Cross->>Nlp: passes_brand_gate() → brand_is_present()
    Note right of Nlp: FIX: brand_is_present() vive em\nnlp_service.py, chamado via wrapper\npasses_brand_gate() em\ncross_marketplace_service.py —\nNÃO em relevance_gates.py.\nOrdem real: cutoff+brand gate ANTES\ndo similar fallback (diagrama original\ninvertia essa ordem)
    Cross->>Gates: apply_similar_fallback()
    Cross->>Cross: apply_visual_tiebreak()
    Note right of Cross: FIX: vive em cross_marketplace_service.py,\nnão em relevance_gates.py
    Cross->>Gates: cap_results_per_platform()

    Note over Cross: Etapa 4: PDP Enrichment + Shipping
    par PDP + Frete (paralelo por produto)
        Cross->>ML: get_product_details(url) → seller real + preço PDP
        Cross->>NS: get_product_details(url) → seller real + preço PDP
        Cross->>AZ: get_product_details(url) → seller real + preço PDP
        Cross->>Ship: calculate_shipping(product, CEP) → "Tier 2"
    end
    Cross->>Gates: build_formatted_results()
    Cross->>Gates: dedup_results()
    Cross->>Gates: mark_buybox_winner()
    Cross-->>API: { results[], metrics, vision_active, similar_fallback, errors[] }
    API->>Hist: update_job(job_id, status=COMPLETED, results)
    API-->>FE: JSON response + reference_product
    FE-->>Usuario: Cards de produtos com seller, preço, frete, match %, buybox
```

---

## 8. Sequence: Extração de Banners

```mermaid
sequenceDiagram
    actor Usuario as Usuário
    participant FE as Frontend
    participant WS as WebSocket (/ws/{run_id})
    participant API as FastAPI (routes_banners.py)
    participant JobSvc as BannerJobService
    participant Extract as BannerExtractionService (Playwright)
    participant Storage as BannerStorageService
    participant Report as BannerReportService

    Usuario->>FE: Seleciona marcas para extrair banners
    FE->>API: POST /banners/jobs { brands: [...] }
    API->>JobSvc: create_job(brands)
    JobSvc->>JobSvc: normaliza/dedup brand_keys, valida contra\nbrand_service.list_brands(active_only=True)
    Note right of JobSvc: ValueError → HTTP 400 se marca\ninativa/desconhecida ou lista vazia
    JobSvc->>JobSvc: gera run_id, monta BannerRun\n(status=RUNNING, brand_progress={brand: PENDING})
    JobSvc->>Storage: save_run(run)
    JobSvc->>JobSvc: JOB_CANCEL_FLAGS[run_id] = asyncio.Event()
    Note right of JobSvc: mesmo dict global de cancelamento\nusado pelo scraping de categoria (job_manager.py)
    API->>JobSvc: background_tasks.add_task(run_job, run_id)
    Note right of API: fire-and-forget — NÃO é awaited,\nroda após a resposta HTTP
    API-->>FE: 202 { job_id, status: "RUNNING", run }
    FE->>WS: Conecta ws://host/ws/{run_id}?api_key=...
    Note right of WS: canal compartilhado com o scraping\nde categoria, não é exclusivo de banners

    rect rgb(245, 245, 235)
    Note over JobSvc,Extract: Execução em background (asyncio.to_thread)
    JobSvc->>JobSvc: run_job(run_id) → asyncio.to_thread(_run_sync)
    JobSvc->>Extract: chromium_session() → 1 Chromium headless\ncompartilhado para todo o job

    loop Para cada marca selecionada (sequencial, não paralelo)
        alt cancel_event já setado
            JobSvc->>JobSvc: marca → CANCELLED, pula extração
        else
            JobSvc->>WS: progress[marca] = RUNNING
            JobSvc->>Extract: extract_brand(browser, marca, cancel_event, callback)
            Extract->>Extract: is_safe_public_http_url(domínio) — guard SSRF
            Extract->>Extract: novo browser context isolado\n(viewport 1366x768, pt-BR, UA desktop)
            Extract->>Extract: page.goto(domínio, domcontentloaded, timeout=30s)
            Extract->>Extract: aguarda settle (~4s) + dispara JS\nde dismiss de cookie/consent (regex pt-BR)

            Extract->>Extract: screenshot above-the-fold (full_page=False)
            Extract->>Storage: store_asset(screenshot) → progress.screenshot_asset

            loop Até 12 tentativas (carrossel/slides)
                Extract->>Extract: CAROUSEL_INFO_JS - lê dots/slides declarados\ne candidatos de elemento video
                Extract->>Extract: COLLECT_BANNERS_JS - detecta candidatos\nimg (tamanho minimo 60pct viewport)\ne background/background-pseudo via seletor\nbanner/hero/slide/carousel/swiper e\nestilo computado dos pseudo-elementos before/after
                Note right of Extract: dedup por source_url, mantém só a\nhero row (y proximo do topo, ate 100px)
                Extract->>WS: progress "discovery" (contadores)
                alt achou slides novos e não atingiu o limite
                    Extract->>Extract: CLICK_NEXT_JS (botão próximo/seta) + aguarda 800ms
                else nada novo por 2 rounds OU slides declarados atingidos OU limite de 12
                    Extract->>Extract: encerra loop de slides
                end
            end

            loop Para cada candidato coletado
                Extract->>Extract: revalida SSRF na URL final (após redirect)
                Extract->>Extract: baixa asset via context.request.get\n(timeout 30s, limite 30MB, detecta MIME real)
                Extract->>Storage: store_asset(bytes, content_type)\n→ path content-addressed por SHA-256
                Extract->>Extract: monta BannerCandidate (approved=false,\ndom_kind = img|background|background-pseudo)
                Extract->>WS: progress "banner" → JobSvc anexa em run.banners
            end
            Extract->>Extract: vídeos: só metadata do elemento video\n(sem download/asset) → BannerVideoSlide
            Note right of Extract: nenhum OCR nem CLIP é usado aqui —\nsão serviços de outro pipeline (produto/review)

            alt InterruptedError (cancelamento no meio da marca)
                JobSvc->>JobSvc: marca → CANCELLED + seta cancel_event\n(propaga para as próximas marcas)
            else Exception isolada
                JobSvc->>JobSvc: marca → FAILED (não aborta as outras marcas)
            else sucesso
                JobSvc->>JobSvc: marca → COMPLETED (banner_count, video_count)
            end
            JobSvc->>WS: evento terminal da marca
        end
    end

    JobSvc->>JobSvc: calcula status final do run:
    Note right of JobSvc: cancel_event setado → CANCELLED<br/>senão, alguma marca FAILED → PARTIAL (se há banners)\nou FAILED (se não há)<br/>senão, run.banners vazio → FAILED<br/>senão → REVIEW (único caminho de sucesso)
    JobSvc->>Storage: save_run(run)
    JobSvc->>Report: generate(run) — gera json/csv/html
    JobSvc->>WS: evento terminal do job (status final)
    end

    par Frontend acompanha em paralelo
        FE->>API: GET /banners/jobs/{run_id} (polling)
        API-->>FE: BannerRun atual (qualquer status)
    and
        WS-->>FE: push de progresso em tempo real
    end

    opt Usuário cancela no meio da extração
        FE->>API: POST /banners/jobs/{run_id}/stop
        API->>JobSvc: stop_job(run_id) — exige status == RUNNING
        Note right of API: HTTP 409 se o run não estiver RUNNING
        JobSvc->>JobSvc: cancel_event.set()
        Note right of JobSvc: cooperativo — checkpoints no meio do\nscraping, não mata a operação em voo
        API-->>FE: 202
    end

    Note right of FE: status == REVIEW: candidatos aparecem\ncom approved=false, aguardando curadoria humana
    Usuario->>FE: Revisa candidatos e seleciona quais aprovar
    FE->>API: POST /banners/jobs/{run_id}/approve { banner_ids: [...] }
    API->>Storage: approve_run(run_id, banner_ids)
    Note right of Storage: exige status == REVIEW (senão ValueError → 409).<br/>Filtra run.banners só para os selecionados\n(approved=true) — os NÃO selecionados são\nREMOVIDOS do run, não apenas marcados false
    Storage->>Storage: status = COMPLETED, approved_at = now\nsave_run (runs COMPLETED viram imutáveis)
    API->>Report: generate(run) — regenera com o conjunto final aprovado
    API-->>FE: BannerRun atualizado (status=COMPLETED)

    Usuario->>FE: Consulta histórico / assets
    FE->>API: GET /banners/history
    API-->>FE: Lista de runs COMPLETED (resumo)
    FE->>API: GET /banners/history/{run_id}
    Note right of API: 404 se o run não estiver COMPLETED
    API-->>FE: BannerRun completo
    FE->>API: GET /banners/assets/{run_id}/{banner_id}
    API-->>FE: Arquivo do banner (por SHA-256)
    FE->>API: GET /banners/screenshots/{run_id}/{brand_key}
    API-->>FE: Screenshot above-the-fold da marca
    FE->>API: GET /banners/runs/{run_id}/reports/{format}
    Note right of API: format ∈ {json, csv, html} — pré-gerado,\nnão sob demanda. 404 se ausente. Sem PDF/XLSX.
    API-->>FE: Arquivo do relatório
```

