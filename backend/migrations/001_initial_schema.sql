BEGIN;

CREATE SCHEMA IF NOT EXISTS scraping;
SET search_path TO scraping, public;

COMMENT ON SCHEMA scraping IS
  'Persistencia PostgreSQL do scraper. Frete operacional permanece no Redis.';

CREATE OR REPLACE FUNCTION scraping.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;


-- =============================================================================
-- CATALOGO DE MARCAS E CATEGORIAS
-- =============================================================================

CREATE TABLE IF NOT EXISTS brands (
  id            bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  brand_key     varchar(63)  NOT NULL,
  brand_name    varchar(160) NOT NULL,
  domain        text         NOT NULL,
  engine        varchar(63),
  configuration jsonb        NOT NULL DEFAULT '{}'::jsonb,
  is_active     boolean      NOT NULL DEFAULT true,
  created_at    timestamptz  NOT NULL DEFAULT now(),
  updated_at    timestamptz  NOT NULL DEFAULT now(),

  CONSTRAINT uq_brands_brand_key UNIQUE (brand_key),
  CONSTRAINT uq_brands_domain UNIQUE (domain),
  CONSTRAINT chk_brands_configuration_object
    CHECK (jsonb_typeof(configuration) = 'object')
);

CREATE TABLE IF NOT EXISTS brand_category_mappings (
  id             bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  brand_id       bigint       NOT NULL,
  canonical_slug varchar(160) NOT NULL,
  source_path    text,
  label          varchar(200),
  created_at     timestamptz  NOT NULL DEFAULT now(),
  updated_at     timestamptz  NOT NULL DEFAULT now(),

  CONSTRAINT fk_brand_category_mappings_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT uq_brand_category_mappings
    UNIQUE (brand_id, canonical_slug)
);


-- =============================================================================
-- MONITORAMENTO DE CATEGORIA
-- =============================================================================

CREATE TABLE IF NOT EXISTS category_monitors (
  id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  brand_id         bigint      NOT NULL,
  category_url     text        NOT NULL,
  normalized_url   text        NOT NULL,
  status           varchar(20) NOT NULL DEFAULT 'active',
  last_scanned_at  timestamptz,
  last_scan_id     bigint,
  last_scan_status varchar(20),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT fk_category_monitors_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT uq_category_monitors_brand_url
    UNIQUE (brand_id, normalized_url),
  CONSTRAINT chk_category_monitors_status
    CHECK (status IN ('active', 'paused', 'disabled')),
  CONSTRAINT chk_category_monitors_last_scan_status
    CHECK (
      last_scan_status IS NULL
      OR last_scan_status IN ('pending', 'running', 'completed', 'failed')
    )
);

CREATE TABLE IF NOT EXISTS category_scans (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  monitor_id    bigint      NOT NULL,
  brand_id      bigint      NOT NULL,
  category_url  text        NOT NULL,
  origin        varchar(20) NOT NULL,
  status        varchar(20) NOT NULL DEFAULT 'pending',
  product_count integer     NOT NULL DEFAULT 0,
  error_message text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  started_at    timestamptz,
  completed_at  timestamptz,

  CONSTRAINT fk_category_scans_monitor
    FOREIGN KEY (monitor_id) REFERENCES category_monitors (id) ON DELETE CASCADE,
  CONSTRAINT fk_category_scans_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT chk_category_scans_origin
    CHECK (origin IN ('manual', 'scheduled')),
  CONSTRAINT chk_category_scans_status
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  CONSTRAINT chk_category_scans_product_count
    CHECK (product_count >= 0),
  CONSTRAINT chk_category_scans_error_length
    CHECK (error_message IS NULL OR length(error_message) < 5000),
  CONSTRAINT chk_category_scans_timestamps
    CHECK (
      (started_at IS NULL OR started_at >= created_at)
      AND (completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at)
    )
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'fk_category_monitors_last_scan'
      AND conrelid = 'scraping.category_monitors'::regclass
  ) THEN
    ALTER TABLE category_monitors
      ADD CONSTRAINT fk_category_monitors_last_scan
      FOREIGN KEY (last_scan_id)
      REFERENCES category_scans (id)
      ON DELETE SET NULL
      DEFERRABLE INITIALLY DEFERRED;
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS scan_products (
  id                      bigint         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scan_id                 bigint         NOT NULL,
  scan_product_id         uuid           NOT NULL,
  source_product_id       text,
  product_code            varchar(160),
  sku_id                  varchar(160),
  seller_id               varchar(160),
  product_url             text           NOT NULL,
  normalized_url          text           NOT NULL,
  raw_title               text           NOT NULL,
  raw_description         text           NOT NULL DEFAULT '',
  category                varchar(200),
  sub_category            varchar(200),
  breadcrumb              text[]         NOT NULL DEFAULT '{}'::text[],
  composition             text,
  collection_ids          text[]         NOT NULL DEFAULT '{}'::text[],
  price_full              numeric(12,2),
  price_discount          numeric(12,2),
  price_without_discount  numeric(12,2),
  effective_price         numeric(12,2),
  shipping_price          numeric(12,2),
  landed_price            numeric(12,2),
  is_free_shipping        boolean        NOT NULL DEFAULT false,
  stock_availability      boolean,
  available_colors        text[]         NOT NULL DEFAULT '{}'::text[],
  available_sizes         text[]         NOT NULL DEFAULT '{}'::text[],
  raw_colors              text[]         NOT NULL DEFAULT '{}'::text[],
  raw_sizes               text[]         NOT NULL DEFAULT '{}'::text[],
  rating                  numeric(2,1),
  review_count            integer,
  reviews_state           varchar(30),
  review_product_id       varchar(200),
  specifications          jsonb          NOT NULL DEFAULT '{}'::jsonb,
  conditional_offers      jsonb          NOT NULL DEFAULT '[]'::jsonb,
  image_url               text,
  seller                  varchar(200),
  captured_at             timestamptz    NOT NULL DEFAULT now(),

  CONSTRAINT fk_scan_products_scan
    FOREIGN KEY (scan_id) REFERENCES category_scans (id) ON DELETE CASCADE,
  CONSTRAINT uq_scan_products_scan_product
    UNIQUE (scan_id, scan_product_id),
  CONSTRAINT uq_scan_products_scan_code
    UNIQUE (scan_id, product_code),
  CONSTRAINT chk_scan_products_prices
    CHECK (
      (price_full IS NULL OR price_full >= 0)
      AND (price_discount IS NULL OR price_discount >= 0)
      AND (price_without_discount IS NULL OR price_without_discount >= 0)
      AND (effective_price IS NULL OR effective_price >= 0)
      AND (shipping_price IS NULL OR shipping_price >= 0)
      AND (landed_price IS NULL OR landed_price >= 0)
    ),
  CONSTRAINT chk_scan_products_rating
    CHECK (rating IS NULL OR rating BETWEEN 0 AND 5),
  CONSTRAINT chk_scan_products_review_count
    CHECK (review_count IS NULL OR review_count >= 0),
  CONSTRAINT chk_scan_products_specifications_object
    CHECK (jsonb_typeof(specifications) = 'object'),
  CONSTRAINT chk_scan_products_conditional_offers_array
    CHECK (jsonb_typeof(conditional_offers) = 'array')
);

COMMENT ON COLUMN scan_products.shipping_price IS
  'Snapshot monetario usado na captura; cotacoes operacionais e TTL permanecem no Redis.';
COMMENT ON COLUMN scan_products.stock_availability IS
  'Disponibilidade individual observada. Nao representa ruptura agregada.';

CREATE TABLE IF NOT EXISTS review_comments (
  id                bigint        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scan_product_id   bigint        NOT NULL,
  source_provider   varchar(50)   NOT NULL,
  review_id         varchar(200)  NOT NULL,
  rating            numeric(2,1),
  title             text,
  body              text,
  author_name       varchar(200),
  review_created_at timestamptz,
  source_ref        text,
  captured_at       timestamptz   NOT NULL DEFAULT now(),

  CONSTRAINT fk_review_comments_scan_product
    FOREIGN KEY (scan_product_id) REFERENCES scan_products (id) ON DELETE CASCADE,
  CONSTRAINT uq_review_comments_provider_review
    UNIQUE (source_provider, review_id),
  CONSTRAINT chk_review_comments_rating
    CHECK (rating IS NULL OR rating BETWEEN 0 AND 5)
);


-- =============================================================================
-- MONITORAMENTO DE PRECO
-- =============================================================================

CREATE TABLE IF NOT EXISTS price_monitors (
  id                             bigint         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id                         uuid           NOT NULL,
  brand_id                       bigint         NOT NULL,
  source_key                     varchar(63)    NOT NULL,
  product_url                    text           NOT NULL,
  normalized_url                 text           NOT NULL,
  status                         varchar(20)    NOT NULL DEFAULT 'active',
  interval_minutes               integer        NOT NULL DEFAULT 10,
  duration_hours                 integer        NOT NULL DEFAULT 24,
  started_at                     timestamptz    NOT NULL DEFAULT now(),
  last_price                     numeric(12,2),
  last_price_original            numeric(12,2),
  last_price_discount            numeric(12,2),
  last_check_state               varchar(20),
  last_error                     text,
  last_checked_at                timestamptz,
  product_name                   text,
  image_url                      text,
  available_colors               text[]         NOT NULL DEFAULT '{}'::text[],
  available_sizes                text[]         NOT NULL DEFAULT '{}'::text[],
  conditional_offers_current     jsonb          NOT NULL DEFAULT '[]'::jsonb,
  conditional_offers_status      varchar(30),
  conditional_offers_checked_at  timestamptz,
  conditional_offers_diagnostics jsonb          NOT NULL DEFAULT '[]'::jsonb,
  conditional_offer_history      jsonb          NOT NULL DEFAULT '[]'::jsonb,
  created_at                     timestamptz    NOT NULL DEFAULT now(),
  updated_at                     timestamptz    NOT NULL DEFAULT now(),

  CONSTRAINT fk_price_monitors_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT uq_price_monitors_job UNIQUE (job_id),
  CONSTRAINT uq_price_monitors_brand_source_url
    UNIQUE (brand_id, source_key, normalized_url),
  CONSTRAINT chk_price_monitors_status
    CHECK (status IN ('active', 'paused')),
  CONSTRAINT chk_price_monitors_last_check_state
    CHECK (last_check_state IS NULL OR last_check_state IN ('ok', 'blocked', 'error')),
  CONSTRAINT chk_price_monitors_interval
    CHECK (interval_minutes > 0),
  CONSTRAINT chk_price_monitors_duration
    CHECK (duration_hours > 0),
  CONSTRAINT chk_price_monitors_prices
    CHECK (
      (last_price IS NULL OR last_price >= 0)
      AND (last_price_original IS NULL OR last_price_original >= 0)
      AND (last_price_discount IS NULL OR last_price_discount >= 0)
    ),
  CONSTRAINT chk_price_monitors_conditional_offers_array
    CHECK (jsonb_typeof(conditional_offers_current) = 'array'),
  CONSTRAINT chk_price_monitors_diagnostics_array
    CHECK (jsonb_typeof(conditional_offers_diagnostics) = 'array'),
  CONSTRAINT chk_price_monitors_offer_history_array
    CHECK (jsonb_typeof(conditional_offer_history) = 'array')
);

CREATE TABLE IF NOT EXISTS price_history (
  monitor_id          bigint        NOT NULL,
  price               numeric(12,2) NOT NULL,
  price_original      numeric(12,2),
  last_price_discount numeric(12,2),
  available           boolean       NOT NULL,
  available_colors    text[]        NOT NULL DEFAULT '{}'::text[],
  available_sizes     text[]        NOT NULL DEFAULT '{}'::text[],
  observed_at         timestamptz   NOT NULL DEFAULT now(),

  CONSTRAINT chk_price_history_prices
    CHECK (
      price >= 0
      AND (price_original IS NULL OR price_original >= 0)
      AND (last_price_discount IS NULL OR last_price_discount >= 0)
    )
) PARTITION BY RANGE (observed_at);

CREATE TABLE price_history_2026 PARTITION OF price_history
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE price_history_2027 PARTITION OF price_history
    FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');
CREATE TABLE price_history_2028 PARTITION OF price_history
    FOR VALUES FROM ('2028-01-01') TO ('2029-01-01');


-- =============================================================================
-- BUSCAS
-- =============================================================================

CREATE TABLE IF NOT EXISTS search_jobs (
  id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id         uuid        NOT NULL,
  query          text        NOT NULL,
  target_sku     text,
  search_type    varchar(20) NOT NULL DEFAULT 'search',
  status         varchar(20) NOT NULL DEFAULT 'PENDING',
  result_payload jsonb,
  error_message  text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_search_jobs_job UNIQUE (job_id),
  CONSTRAINT chk_search_jobs_type
    CHECK (search_type IN ('search', 'cross')),
  CONSTRAINT chk_search_jobs_status
    CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
  CONSTRAINT chk_search_jobs_result_payload
    CHECK (result_payload IS NULL OR jsonb_typeof(result_payload) IN ('object', 'array'))
);

CREATE TABLE IF NOT EXISTS search_job_brands (
  job_id       bigint      NOT NULL,
  source_key   varchar(63) NOT NULL,
  search_order integer,

  CONSTRAINT pk_search_job_brands PRIMARY KEY (job_id, source_key),
  CONSTRAINT fk_search_job_brands_job
    FOREIGN KEY (job_id) REFERENCES search_jobs (id) ON DELETE CASCADE,
  CONSTRAINT chk_search_job_brands_order
    CHECK (search_order IS NULL OR search_order >= 0)
);


-- =============================================================================
-- BANNERS
-- =============================================================================

CREATE TABLE IF NOT EXISTS banner_runs (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id        uuid        NOT NULL,
  status        varchar(20) NOT NULL DEFAULT 'RUNNING',
  error_message text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  approved_at   timestamptz,

  CONSTRAINT uq_banner_runs_run UNIQUE (run_id),
  CONSTRAINT chk_banner_runs_status
    CHECK (status IN ('RUNNING', 'REVIEW', 'COMPLETED', 'PARTIAL', 'CANCELLED', 'FAILED')),
  CONSTRAINT chk_banner_runs_approved_at
    CHECK (approved_at IS NULL OR approved_at >= created_at)
);

CREATE TABLE IF NOT EXISTS banner_run_brands (
  run_id        bigint      NOT NULL,
  brand_id      bigint      NOT NULL,
  status        varchar(20) NOT NULL DEFAULT 'PENDING',
  banner_count  integer     NOT NULL DEFAULT 0,
  video_count   integer     NOT NULL DEFAULT 0,
  error_message text,

  CONSTRAINT pk_banner_run_brands PRIMARY KEY (run_id, brand_id),
  CONSTRAINT fk_banner_run_brands_run
    FOREIGN KEY (run_id) REFERENCES banner_runs (id) ON DELETE CASCADE,
  CONSTRAINT fk_banner_run_brands_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT chk_banner_run_brands_status
    CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
  CONSTRAINT chk_banner_run_brands_counts
    CHECK (banner_count >= 0 AND video_count >= 0)
);

CREATE TABLE IF NOT EXISTS banner_assets (
  id           bigint       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sha256       char(64)     NOT NULL,
  object_key   text         NOT NULL,
  extension    varchar(20)  NOT NULL,
  content_type varchar(120) NOT NULL,
  byte_count   bigint       NOT NULL,
  created_at   timestamptz  NOT NULL DEFAULT now(),

  CONSTRAINT uq_banner_assets_sha256 UNIQUE (sha256),
  CONSTRAINT uq_banner_assets_object_key UNIQUE (object_key),
  CONSTRAINT chk_banner_assets_sha256_hex
    CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT chk_banner_assets_byte_count
    CHECK (byte_count >= 0)
);

CREATE TABLE IF NOT EXISTS banner_candidates (
  id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  banner_id         uuid        NOT NULL,
  run_id            bigint      NOT NULL,
  brand_id          bigint      NOT NULL,
  asset_id          bigint      NOT NULL,
  slide_order       integer     NOT NULL,
  friendly_filename text        NOT NULL,
  source_url        text        NOT NULL,
  rendered_url      text        NOT NULL,
  click_url         text,
  alt_text          text,
  dom_kind          varchar(30) NOT NULL DEFAULT 'img',
  rendered_width    integer,
  rendered_height   integer,
  natural_width     integer,
  natural_height    integer,
  approved          boolean     NOT NULL DEFAULT false,
  captured_at       timestamptz NOT NULL DEFAULT now(),
  created_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_banner_candidates_banner UNIQUE (banner_id),
  CONSTRAINT uq_banner_candidates_run_brand_slide
    UNIQUE (run_id, brand_id, slide_order),
  CONSTRAINT fk_banner_candidates_run
    FOREIGN KEY (run_id) REFERENCES banner_runs (id) ON DELETE CASCADE,
  CONSTRAINT fk_banner_candidates_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT fk_banner_candidates_asset
    FOREIGN KEY (asset_id) REFERENCES banner_assets (id) ON DELETE RESTRICT,
  CONSTRAINT chk_banner_candidates_slide_order
    CHECK (slide_order >= 1),
  CONSTRAINT chk_banner_candidates_dimensions
    CHECK (
      (rendered_width IS NULL OR rendered_width >= 0)
      AND (rendered_height IS NULL OR rendered_height >= 0)
      AND (natural_width IS NULL OR natural_width >= 0)
      AND (natural_height IS NULL OR natural_height >= 0)
    )
);

CREATE TABLE IF NOT EXISTS banner_video_slides (
  id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id      bigint      NOT NULL,
  brand_id    bigint      NOT NULL,
  slide_order integer     NOT NULL,
  source_url  text,

  CONSTRAINT uq_banner_video_slides_run_brand_slide
    UNIQUE (run_id, brand_id, slide_order),
  CONSTRAINT fk_banner_video_slides_run
    FOREIGN KEY (run_id) REFERENCES banner_runs (id) ON DELETE CASCADE,
  CONSTRAINT fk_banner_video_slides_brand
    FOREIGN KEY (brand_id) REFERENCES brands (id) ON DELETE RESTRICT,
  CONSTRAINT chk_banner_video_slides_order
    CHECK (slide_order >= 1)
);


-- =============================================================================
-- TRIGGERS DE AUDITORIA
-- =============================================================================

DROP TRIGGER IF EXISTS trg_brands_updated_at ON brands;
CREATE TRIGGER trg_brands_updated_at
  BEFORE UPDATE ON brands
  FOR EACH ROW EXECUTE FUNCTION scraping.set_updated_at();

DROP TRIGGER IF EXISTS trg_brand_category_mappings_updated_at ON brand_category_mappings;
CREATE TRIGGER trg_brand_category_mappings_updated_at
  BEFORE UPDATE ON brand_category_mappings
  FOR EACH ROW EXECUTE FUNCTION scraping.set_updated_at();

DROP TRIGGER IF EXISTS trg_category_monitors_updated_at ON category_monitors;
CREATE TRIGGER trg_category_monitors_updated_at
  BEFORE UPDATE ON category_monitors
  FOR EACH ROW EXECUTE FUNCTION scraping.set_updated_at();

DROP TRIGGER IF EXISTS trg_price_monitors_updated_at ON price_monitors;
CREATE TRIGGER trg_price_monitors_updated_at
  BEFORE UPDATE ON price_monitors
  FOR EACH ROW EXECUTE FUNCTION scraping.set_updated_at();

DROP TRIGGER IF EXISTS trg_search_jobs_updated_at ON search_jobs;
CREATE TRIGGER trg_search_jobs_updated_at
  BEFORE UPDATE ON search_jobs
  FOR EACH ROW EXECUTE FUNCTION scraping.set_updated_at();

DROP TRIGGER IF EXISTS trg_banner_runs_updated_at ON banner_runs;
CREATE TRIGGER trg_banner_runs_updated_at
  BEFORE UPDATE ON banner_runs
  FOR EACH ROW EXECUTE FUNCTION scraping.set_updated_at();


-- =============================================================================
-- INDICES
-- =============================================================================

-- Foreign keys que ainda nao sao cobertas pelo inicio de PK/UNIQUE composto.
CREATE INDEX IF NOT EXISTS ix_brand_category_mappings_brand
  ON brand_category_mappings (brand_id);

CREATE INDEX IF NOT EXISTS ix_category_monitors_brand
  ON category_monitors (brand_id);

CREATE INDEX IF NOT EXISTS ix_category_scans_monitor_time
  ON category_scans (monitor_id, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_category_scans_brand_time
  ON category_scans (brand_id, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_scan_products_code
  ON scan_products (product_code);

CREATE INDEX IF NOT EXISTS ix_scan_products_normalized_url
  ON scan_products (normalized_url);

CREATE INDEX IF NOT EXISTS ix_review_comments_scan_product_time
  ON review_comments (scan_product_id, review_created_at DESC);

CREATE INDEX IF NOT EXISTS ix_price_monitors_brand
  ON price_monitors (brand_id);

CREATE INDEX IF NOT EXISTS ix_price_history_monitor_time
  ON price_history (monitor_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_search_jobs_created_at
  ON search_jobs (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_search_jobs_status_created_at
  ON search_jobs (status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_search_job_brands_source
  ON search_job_brands (source_key);

CREATE INDEX IF NOT EXISTS ix_banner_runs_status_created_at
  ON banner_runs (status, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_banner_run_brands_brand
  ON banner_run_brands (brand_id);

CREATE INDEX IF NOT EXISTS ix_banner_candidates_brand
  ON banner_candidates (brand_id);

CREATE INDEX IF NOT EXISTS ix_banner_candidates_asset
  ON banner_candidates (asset_id);

CREATE INDEX IF NOT EXISTS ix_banner_video_slides_brand
  ON banner_video_slides (brand_id);

-- Linhas quentes dos schedulers e da aprovacao de banners.
CREATE INDEX IF NOT EXISTS ix_category_monitors_active
  ON category_monitors (brand_id, last_scanned_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ix_price_monitors_active
  ON price_monitors (brand_id, last_checked_at)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS ix_banner_candidates_approved
  ON banner_candidates (run_id, brand_id, slide_order)
  WHERE approved = true;


-- =============================================================================
-- DECISOES DE CAPACIDADE
-- =============================================================================

-- Nao criar inicialmente:
--   * GIN em specifications/result_payload: nao ha filtros internos em jsonb.
--   * BRIN em price_history/scan_products: aguardar volume e EXPLAIN ANALYZE.
--   * shipping_quotes: cache operacional com TTL permanece no Redis.
--   * map_rules, stock_summaries e rupture_ratio: fora do escopo do produto.

CREATE INDEX IF NOT EXISTS ix_scan_products_colors ON scan_products USING GIN (available_colors);
CREATE INDEX IF NOT EXISTS ix_scan_products_sizes ON scan_products USING GIN (available_sizes);
CREATE INDEX IF NOT EXISTS ix_price_history_colors ON price_history USING GIN (available_colors);
CREATE INDEX IF NOT EXISTS ix_price_history_sizes ON price_history USING GIN (available_sizes);

COMMIT;
