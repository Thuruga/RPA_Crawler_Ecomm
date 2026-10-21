/* eslint-disable @typescript-eslint/no-explicit-any */
import { getClerkToken, notifyUnauthorized } from './clerkTokenBridge';

const API_BASE_URL = import.meta.env.VITE_API_URL || '';
// Janela para o browser iniciar o download antes de liberar o blob URL (IN-03).
const BLOB_REVOKE_DELAY_MS = 100;

export type StockRuptureSummary = {
  brand: string;
  total_products: number;
  in_stock_count: number;
  out_of_stock_count: number;
  unknown_stock_count: number;
  verified_stock_count: number;
  rupture_pct: number | null;
  scan_id?: string | null;
  monitor_id?: string | null;
  scanned_at: string;
};

export type StockDepthResult = {
  stock_depth_estimate?: number | null;
  stock_depth_state: string;
  stock_depth_checked_at?: string | null;
  stock_depth_source?: string | null;
  stock_depth_label?: string | null;
};

export type ReviewComment = {
  review_id: string;
  rating?: number | null;
  title?: string | null;
  text?: string | null;
  author?: string | null;
  created_at?: string | null;
  source_provider: string;
  source_ref?: string | null;
};

export type ReviewCommentsResult = {
  reviews_state: string;
  comments: ReviewComment[];
  rating?: number | null;
  review_count?: number | null;
  review_product_id?: string | null;
  source_provider?: string | null;
  max_pages: number;
};

export type ProductDetail = {
  state: string;
  product: {
    raw_title?: string;
    image_url?: string | null;
    price_full?: number | null;
    price_discount?: number | null;
    stock_availability?: boolean | null;
    available_colors?: string[];
    available_sizes?: string[];
    raw_description?: string | null;
    composition?: string | null;
    breadcrumb?: string[];
    specifications?: Record<string, string>;
    url?: string;
    rating?: number | null;
    review_count?: number | null;
    reviews_state?: string | null;
    review_product_id?: string | null;
  } | null;
  // Presentes nos estados blocked/unsupported/stale/ok (plano 08-05); ausentes
  // em error/unavailable — null é o valor honesto quando o backend não envia.
  last_success_at?: string | null;
  next_retry_at?: string | null;
};

// Ponto de série de preço histórico de um produto de categoria (MON-02).
export type CategoryPriceHistoryPoint = {
  timestamp: string;
  price: number;
  price_original?: number | null;
  available?: boolean | null;
};

// Estado de uma tentativa individual (attempt) dentro do ledger de um job
// durável (category_scan hoje; formato compartilhado por outros kinds no
// backend — JobAttemptRecord). completed_units nunca vira 0 quando ausente;
// total_units/percent/result/error ficam null quando não fabricados.
export type JobAttemptSnapshot = {
  attempt: number;
  status: string;
  progress: JobProgress;
  created_at: string;
  started_at?: string | null;
  updated_at: string;
  completed_at?: string | null;
  result?: unknown;
  error?: string | null;
};

// Registro de scan de um monitor de categoria — usado por deriveCategoryLifecycle
// e pelo polling (useMonitorJobPolling). Espelha CategoryScanRecord do backend
// (core/models.py): progress/attempts preservam nullability e INTERRUPTED nunca
// vira FAILED (D-05 da Fase 7).
export type CategoryScanRecord = {
  scan_id: string;
  monitor_id: string;
  brand: string;
  category_url: string;
  origin: 'manual' | 'scheduled';
  status: string;
  kind: string;
  input: Record<string, unknown>;
  attempt: number;
  progress: JobProgress;
  attempts: JobAttemptSnapshot[];
  created_at: string;
  started_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  error_message?: string | null;
  product_count: number;
  logical_cycle_id?: string | null;
  scheduled_for?: string | null;
  [key: string]: unknown;
};

export interface CategoryProductItem {
  product_name: string;
  price: number;
  price_original?: number | null;
  url: string;
  image_url?: string | null;
  sku?: string | null;
  brand: string;
  is_available?: boolean;
  [key: string]: unknown;
}

export interface BrandPriceAverage {
  average_price: number | null;
  average_price_original?: number | null;
  count: number;
}

export type BrandAveragesMap = Record<string, BrandPriceAverage>;

export type ConditionalOfferState = 'advertised' | 'confirmed' | 'requires_auth' | 'expired';
export type ConditionalOfferStatus = 'found' | 'none_detected' | 'unsupported' | 'blocked' | 'requires_auth' | 'error';
export type ConditionalOfferDiscountUnit = '%' | 'amount' | null;

type WireDecimal = number | string | null;

export type ConditionalOffer = {
  offer_key?: string | null;
  condition_type: 'membership' | 'payment_method' | 'quantity' | 'coupon' | 'checkout' | 'authentication' | 'other';
  condition_label: string;
  eligibility: 'public' | 'requires_membership' | 'requires_payment_method' | 'requires_quantity' | 'requires_coupon' | 'requires_checkout' | 'requires_auth' | 'other';
  price?: number | null;
  effective_unit_price?: number | null;
  discount_value?: number | null;
  discount_unit?: ConditionalOfferDiscountUnit;
  currency: string;
  quantity_required?: number | null;
  coupon_code?: string | null;
  payment_method?: string | null;
  state: ConditionalOfferState;
  raw_text?: string | null;
  valid_from?: string | null;
  valid_until?: string | null;
  observed_from?: string | null;
  observed_until?: string | null;
};

export type WireConditionalOffer = Omit<
  ConditionalOffer,
  'price' | 'effective_unit_price' | 'discount_value' | 'discount_unit'
> & {
  price?: WireDecimal;
  effective_unit_price?: WireDecimal;
  discount_value?: WireDecimal;
  discount_unit?: string | null;
};

export type ConditionalOfferResult = {
  status: ConditionalOfferStatus;
  offers: ConditionalOffer[];
  checked_at: string;
  probe_level: 'fast' | 'full' | 'confirm';
  sources_attempted: string[];
  diagnostics: string[];
  cache: { hit: boolean; stale: boolean; expires_at?: string | null };
};

export type WireConditionalOfferResult = Omit<ConditionalOfferResult, 'offers'> & {
  offers?: WireConditionalOffer[] | null;
};

export type ConditionalOfferEvent = {
  event_type: 'started' | 'changed' | 'ended' | 'state_changed';
  offer_key: string;
  before?: ConditionalOffer | null;
  after?: ConditionalOffer | null;
  observed_from?: string | null;
  observed_until?: string | null;
  created_at: string;
};

export type WireConditionalOfferEvent = Omit<ConditionalOfferEvent, 'before' | 'after'> & {
  before?: WireConditionalOffer | null;
  after?: WireConditionalOffer | null;
};

// Vocabulário terminal de um ciclo de price-check (D-02/D-05 da Fase 7):
// null enquanto o ciclo está reivindicado/em voo; setado exatamente uma vez
// no estado terminal. STALE existe no vocabulário mas pode nunca ser emitido
// pelo engine hoje — o cliente preserva o literal de qualquer forma.
export type PriceCheckState = 'ok' | 'blocked' | 'error' | 'stale' | 'interrupted';

// Uma entrada durável do ledger check_runs — mesmo contrato do PriceCheckRun
// do backend. status null é honesto (ciclo em voo), nunca vira um fallback
// fabricado.
export type PriceCheckRun = {
  run_id: string;
  logical_cycle_id: string;
  scheduled_for: string;
  status: PriceCheckState | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
};

export type PriceMonitorConfig = {
  job_id: string;
  url: string;
  brand: string;
  last_checked_at?: string | null;
  last_success_at?: string | null;
  // Instante em que o coordenador de marketplace permitirá a próxima leitura
  // externa; null/ausente para marcas próprias, que não passam pelo coordenador.
  next_retry_at?: string | null;
  // Cursor durável do monitor para a próxima verificação recorrente. Não deve
  // ser confundido com o retry temporário do coordenador de marketplace.
  next_check_due_at?: string | null;
  last_status?: string | null;
  history: Array<{ timestamp: string; price: number }>;
  conditional_offers_current: ConditionalOffer[];
  conditional_offers_status?: ConditionalOfferStatus | null;
  conditional_offers_checked_at?: string | null;
  conditional_offers_diagnostics: string[];
  conditional_offer_history: ConditionalOfferEvent[];
  // Ledger completo por ciclo (D-02, JOB-01/JOB-02), sem cap de retenção —
  // preserva a ordem recebida do backend (T-07-49).
  check_runs: PriceCheckRun[];
  [key: string]: unknown;
};

export type WirePriceMonitorConfig = {
  job_id: string;
  url: string;
  brand: string;
  last_checked_at?: string | null;
  last_success_at?: string | null;
  // Instante em que o coordenador de marketplace permitirá a próxima leitura
  // externa; null/ausente para marcas próprias, que não passam pelo coordenador.
  next_retry_at?: string | null;
  // Cursor durável do monitor para a próxima verificação recorrente.
  next_check_due_at?: string | null;
  last_status?: string | null;
  history: Array<{ timestamp: string; price: number }>;
  conditional_offers_current?: WireConditionalOffer[] | null;
  conditional_offers_status?: ConditionalOfferStatus | null;
  conditional_offers_checked_at?: string | null;
  conditional_offers_diagnostics?: string[] | null;
  conditional_offer_history?: WireConditionalOfferEvent[] | null;
  check_runs?: PriceCheckRun[] | null;
  [key: string]: unknown;
};

// Resposta imediata de um POST de busca que agora roda em background (D-08):
// o job_id vira a identidade durável navegável antes de qualquer worker rodar.
export type JobAccepted = {
  job_id: string;
  status: string;
  status_url: string;
  poll_after_ms: number;
};

export type JobProgress = {
  stage?: string | null;
  completed_units: number;
  total_units?: number | null;
  percent?: number | null;
  message?: string | null;
};

// Snapshot durável de GET /history/{job_id} (SearchHistorySnapshot): mesmo
// contrato de progresso PENDING/RUNNING/COMPLETED/FAILED/INTERRUPTED consumido
// hoje por useDeepLinkedHistory; result/error só existem em estado terminal.
// Somente `status` é garantido — os demais campos seguem opcionais pois
// useDeepLinkedHistory já os trata defensivamente (optional chaining/fallback)
// e o polling completo (Plan 07-08) é quem vai consumi-los amplamente.
export type JobSnapshot = {
  job_id?: string;
  query?: string;
  target_sku?: string | null;
  type?: string;
  kind?: string;
  brands?: string[];
  status: string;
  attempt?: number;
  progress?: JobProgress;
  created_at?: string;
  started_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  results?: any;
  error?: string | null;
  poll_after_ms?: number;
};

export type MonitorMutationResult = {
  job_id?: string;
  status: string;
  config?: PriceMonitorConfig;
  [key: string]: unknown;
};

type WireMonitorMutationResult = {
  job_id?: string;
  status: string;
  config?: WirePriceMonitorConfig | null;
  [key: string]: unknown;
};

const STRICT_DECIMAL = /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/;

export const normalizeWireDecimal = (value: WireDecimal | undefined): number | null => {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!STRICT_DECIMAL.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
};

export const normalizeDiscountUnit = (value: string | null | undefined): ConditionalOfferDiscountUnit => {
  const normalized = value?.trim().toLowerCase();
  if (normalized === 'percent' || normalized === '%') return '%';
  if (normalized === 'amount') return 'amount';
  return null;
};

export const normalizeConditionalOffer = (offer: WireConditionalOffer): ConditionalOffer => ({
  ...offer,
  price: normalizeWireDecimal(offer.price),
  effective_unit_price: normalizeWireDecimal(offer.effective_unit_price),
  discount_value: normalizeWireDecimal(offer.discount_value),
  discount_unit: normalizeDiscountUnit(offer.discount_unit),
});

export const normalizeConditionalOfferResult = (
  result: WireConditionalOfferResult,
): ConditionalOfferResult => ({
  ...result,
  offers: (result.offers ?? []).map(normalizeConditionalOffer),
});

export const normalizeConditionalOfferEvent = (
  event: WireConditionalOfferEvent,
): ConditionalOfferEvent => ({
  ...event,
  before: event.before ? normalizeConditionalOffer(event.before) : null,
  after: event.after ? normalizeConditionalOffer(event.after) : null,
});

export const normalizePriceMonitorConfig = (
  config: WirePriceMonitorConfig,
): PriceMonitorConfig => ({
  ...config,
  conditional_offers_current: (config.conditional_offers_current ?? []).map(normalizeConditionalOffer),
  conditional_offers_diagnostics: config.conditional_offers_diagnostics ?? [],
  conditional_offer_history: (config.conditional_offer_history ?? []).map(normalizeConditionalOfferEvent),
  // Preserva a ordem recebida e os estados literais (incluindo status: null
  // em voo) — ausência vira lista vazia, nunca um run fabricado.
  check_runs: config.check_runs ?? [],
});

// Vocabulário unificado de lifecycle (MON-01, D-01 frontend-only): resolve os
// dois shapes de wire (monitor individual e monitor de categoria + scans) para
// uma única representação consumida pela UI.
export type UnifiedLifecycleState = 'pending' | 'running' | 'success' | 'stale' | 'error_blocked';

export type UnifiedLifecycle = {
  state: UnifiedLifecycleState;
  lastAttemptAt: string | null;
  lastSuccessAt: string | null;
  nextRetryAt: string | null;
  nextScheduledAttemptAt: string | null;
};

// Individual: PriceMonitorConfig já tem last_checked_at/last_success_at/last_status.
// A função é total — um last_status ausente ou não reconhecido (legado) cai em 'pending'.
export const deriveIndividualLifecycle = (m: PriceMonitorConfig): UnifiedLifecycle => {
  const stateMap: Record<string, UnifiedLifecycleState> = {
    ok: 'success', stale: 'stale', blocked: 'error_blocked', error: 'error_blocked',
  };
  return {
    state: (m.last_status && stateMap[m.last_status]) || 'pending',
    lastAttemptAt: m.last_checked_at ?? null,
    lastSuccessAt: m.last_success_at ?? null,
    nextRetryAt: m.next_retry_at ?? null,
    nextScheduledAttemptAt: m.next_check_due_at ?? null,
  };
};

// Categoria: combina o monitor (last_scraped_at) com a lista de scans, sem confiar
// na ordem do array recebido (Pitfall 2) — sempre ordena por created_at antes de
// escolher o mais recente e o último completed.
export const deriveCategoryLifecycle = (
  monitor: { last_scraped_at?: string | null },
  scans: Array<{ status: string; created_at: string; completed_at?: string | null }>,
): UnifiedLifecycle => {
  if (scans.length === 0) return {
    state: 'pending',
    lastAttemptAt: null,
    lastSuccessAt: null,
    nextRetryAt: null,
    nextScheduledAttemptAt: null,
  };
  const sorted = [...scans].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const latest = sorted[sorted.length - 1];
  const lastSuccess = [...sorted].reverse().find(s => s.status === 'completed') ?? null;
  const state: UnifiedLifecycleState =
    latest.status === 'running' ? 'running' :
    latest.status === 'pending' ? 'pending' :
    latest.status === 'completed' ? 'success' :
    lastSuccess ? 'stale' : 'error_blocked'; // failed com sucesso anterior = stale; sem sucesso anterior = erro
  return {
    state,
    lastAttemptAt: latest.completed_at || latest.created_at,
    lastSuccessAt: lastSuccess?.completed_at ?? monitor.last_scraped_at ?? null,
    // O backend não expõe próxima tentativa para monitor de categoria —
    // null é o valor honesto em todos os branches, nunca derivado de outro campo.
    nextRetryAt: null,
    nextScheduledAttemptAt: null,
  };
};

export const normalizeMonitorMutationResult = (
  result: WireMonitorMutationResult,
): MonitorMutationResult => {
  const { config, ...status } = result;
  if (!config) return status;
  return { ...status, config: normalizePriceMonitorConfig(config) };
};

export type MapRule = {
  id: string;
  scope: 'product' | 'category' | 'brand';
  target: string;
  min_price: number;
  active: boolean;
  brand?: string | null;
  category?: string | null;
  product_code?: string | null;
  product_url?: string | null;
  normalized_url?: string | null;
  notes?: string | null;
  created_at: string;
  updated_at: string;
};

export type MapRulePayload = {
  scope: 'product' | 'category' | 'brand';
  target: string;
  min_price: number;
  active?: boolean;
  brand?: string | null;
  category?: string | null;
  product_code?: string | null;
  product_url?: string | null;
  notes?: string | null;
};

const safeApiDetail = (payload: unknown): string | undefined => {
  if (!payload || typeof payload !== 'object') return undefined;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail.slice(0, 500);
  const message = (payload as { message?: unknown }).message;
  if (typeof message === 'string') return message.slice(0, 500);
  return undefined;
};

export class ApiHttpError extends Error {
  readonly status: number;
  readonly detail?: string;

  constructor(status: number, detail?: string) {
    super(detail || `API Error: ${status}`);
    this.name = 'ApiHttpError';
    this.status = status;
    this.detail = detail;
  }
}

export class ApiClient {
  private static async getAuthHeaders(): Promise<{ token: string | null; headers: Record<string, string> }> {
    const token = await getClerkToken();
    const headers: Record<string, string> = {};
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }
    return { token, headers };
  }

  // ------------------------------------------------------------------
  // Core request
  // ------------------------------------------------------------------
  public static async request<T>(endpoint: string, options: RequestInit = {}, signal?: AbortSignal): Promise<T> {
    const { token, headers: authHeaders } = await this.getAuthHeaders();
    const headers: any = {
      'Content-Type': 'application/json',
      ...authHeaders,
      ...options.headers,
    };

    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      ...options,
      headers,
      ...(signal ? { signal } : {}),
    });

    let data: any;
    try {
      data = await response.json();
    } catch {
      // Ignored
    }

    if (!response.ok) {
      if (response.status === 401) {
        notifyUnauthorized(Boolean(token));
      }
      throw new ApiHttpError(response.status, safeApiDetail(data));
    }

    return data as T;
  }

  // ------------------------------------------------------------------
  // Brands
  // ------------------------------------------------------------------
  static getBrands() {
    return this.request<any[]>('/brands/');
  }

  static saveBrand(data: any) {
    return this.request('/brands/', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  static deleteBrand(brand: string) {
    return this.request(`/brands/${encodeURIComponent(brand)}`, {
      method: 'DELETE',
    });
  }

  static setBrandActive(brandKey: string, isActive: boolean) {
    return this.request(`/brands/${encodeURIComponent(brandKey)}/active`, {
      method: 'PATCH',
      body: JSON.stringify({ is_active: isActive }),
    });
  }

  static identifyBrand(url: string) {
    return this.request<{
      engine: string;
      inferred_name: string;
      domain: string;
      warning?: string;
    }>('/brands/identify', {
      method: 'POST',
      body: JSON.stringify({ url }),
    });
  }

  // ------------------------------------------------------------------
  // MAP rules
  // ------------------------------------------------------------------
  static listMapRules() {
    return this.request<MapRule[]>('/map-rules');
  }

  // ------------------------------------------------------------------
  // Search
  // ------------------------------------------------------------------
  static crossMarketplaceSearch(payload: { target_sku: string; search_query?: string; broad_query?: string; min_score?: number; zipcode?: string }, signal?: AbortSignal) {
    return this.request<JobAccepted>('/search/cross-marketplace', {
      method: 'POST',
      body: JSON.stringify(payload),
    }, signal);
  }

  static calculateSingleShipping(payload: { marketplace: string; url: string; zipcode: string }) {
    return this.request<any>('/search/calculate-shipping', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  // Frete VTEX sob demanda para um produto da busca comparativa. O domínio é
  // resolvido no backend a partir da marca persistida (nunca do caller — T-33-01).
  static calculateVtexShipping(payload: { brand_key: string; sku_id: string; seller_id?: string; zipcode: string }) {
    return this.request<{ state: string; shipping_options: any[] }>('/search/calculate-shipping-vtex', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  static calculateShippingBrand(payload: { brand_key: string; product_url: string; zipcode: string; price?: number | null }) {
    return this.request<{ state: string; shipping_options: any[]; shipping?: any; shipping_price?: number | null; is_free_shipping?: boolean; message?: string }>('/search/calculate-shipping-brand', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  // Detalhes completos de um produto sob demanda (modal de produto — Quick task 260712-iiz).
  // Carrega cores/tamanhos/descrição/composição/especificações/breadcrumb no clique,
  // reaproveitando o mesmo caminho do monitor de preço no backend (engine.get_pdp_product).
  static getProductDetails(payload: { brand: string; url: string }) {
    return this.request<ProductDetail>('/search/product/details', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  static getConditionalOffers(
    payload: { brand: string; url: string; trigger: 'product_opened' | 'manual_revalidate'; force: boolean },
    signal?: AbortSignal,
  ) {
    return this.request<WireConditionalOfferResult>('/search/product/conditional-offers', {
      method: 'POST',
      body: JSON.stringify(payload),
    }, signal).then(normalizeConditionalOfferResult);
  }

  // Matriz de frete multi-regional sob demanda (FRET-09). Sem CEP no payload —
  // os 5 CEPs curados (uma capital por região) são resolvidos no backend.

  static search(payload: { query: string; brands?: string[]; max_per_brand?: number | null; sort?: string; zipcode?: string; include_shipping?: boolean }, signal?: AbortSignal) {
    return this.request<JobAccepted>('/search', {
      method: 'POST',
      body: JSON.stringify(payload),
    }, signal);
  }

  static getHistoryList() {
    return this.request<any[]>('/history');
  }

  static getHistoryDetail(jobId: string, signal?: AbortSignal) {
    return this.request<JobSnapshot>(`/history/${encodeURIComponent(jobId)}`, {}, signal);
  }

  static deleteHistory(jobId: string) {
    return this.request(`/history/${encodeURIComponent(jobId)}`, {
      method: 'DELETE',
    });
  }

  // ------------------------------------------------------------------
  // Desktop banners (Phase 34)
  // ------------------------------------------------------------------
  static startBannerJob(brands: string[]) {
    return this.request<any>('/banners/jobs', {
      method: 'POST',
      body: JSON.stringify({ brands }),
    });
  }

  static getBannerJob(jobId: string) {
    return this.request<any>(`/banners/jobs/${encodeURIComponent(jobId)}`);
  }

  static stopBannerJob(jobId: string) {
    return this.request(`/banners/jobs/${encodeURIComponent(jobId)}/stop`, { method: 'POST' });
  }

  static approveBannerJob(jobId: string, bannerIds: string[]) {
    return this.request<any>(`/banners/jobs/${encodeURIComponent(jobId)}/approve`, {
      method: 'POST',
      body: JSON.stringify({ banner_ids: bannerIds }),
    });
  }

  static getBannerHistory() {
    return this.request<any[]>('/banners/history');
  }

  static getBannerHistoryDetail(jobId: string) {
    return this.request<any>(`/banners/history/${encodeURIComponent(jobId)}`);
  }

  static deleteBannerHistory(jobId: string) {
    return this.request(`/banners/history/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
  }

  private static async protectedBlob(endpoint: string): Promise<Blob> {
    const { token, headers: authHeaders } = await this.getAuthHeaders();
    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      headers: authHeaders,
    });
    if (!response.ok) {
      if (response.status === 401) {
        notifyUnauthorized(Boolean(token));
      }
      let detail = `API Error: ${response.status}`;
      try {
        const payload = await response.json();
        const extracted = safeApiDetail(payload);
        if (extracted) detail = extracted;
      } catch { /* non-JSON body */ }
      throw new Error(detail);
    }
    return response.blob();
  }

  static getBannerAssetBlob(jobId: string, bannerId: string) {
    return this.protectedBlob(`/banners/assets/${encodeURIComponent(jobId)}/${encodeURIComponent(bannerId)}`);
  }

  static getBannerScreenshotBlob(jobId: string, brandKey: string) {
    return this.protectedBlob(`/banners/screenshots/${encodeURIComponent(jobId)}/${encodeURIComponent(brandKey)}`);
  }

  static getBannerReportBlob(jobId: string, format: 'json' | 'csv' | 'html') {
    return this.protectedBlob(`/banners/runs/${encodeURIComponent(jobId)}/reports/${format}`);
  }

  static async openProtectedBlob(blobPromise: Promise<Blob>) {
    const target = window.open('about:blank', '_blank');
    if (target) target.opener = null;
    try {
      const blob = await blobPromise;
      const url = window.URL.createObjectURL(blob);
      if (target) target.location.href = url;
      else window.location.href = url;
      setTimeout(() => window.URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      target?.close();
      throw error;
    }
  }

  // Helper compartilhado de export → download de blob (IN-02): POST autenticado,
  // tratamento de erro, extração de filename do content-disposition e download via <a>.
  private static async downloadExport(
    endpoint: string,
    payload: any,
    defaultFilename: string,
    context: string,
  ): Promise<void> {
    const { token, headers: authHeaders } = await this.getAuthHeaders();
    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders,
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      if (response.status === 401) {
        notifyUnauthorized(Boolean(token));
      }
      let errorMsg = `Export failed: ${response.status}`;
      try {
        const data = await response.json();
        const extracted = safeApiDetail(data);
        if (extracted) errorMsg = extracted;
      } catch (_parseErr) {
        // Non-JSON error body; fall through with the status-based message.
        console.warn(`${context}: could not parse error response body`, _parseErr);
      }
      throw new Error(errorMsg);
    }

    let filename = defaultFilename;
    const disposition = response.headers.get('content-disposition');
    if (disposition && disposition.includes('filename=')) {
      const matches = disposition.match(/filename="([^"]+)"/);
      if (matches && matches[1]) filename = matches[1];
    }

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    // Allow the browser to initiate the download before releasing the blob URL.
    setTimeout(() => {
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    }, BLOB_REVOKE_DELAY_MS);
  }

  static exportSearch(payload: { query: string; brands?: string[]; max_per_brand?: number | null; sort?: string; zipcode?: string; include_shipping?: boolean }): Promise<void> {
    return this.downloadExport('/search/export', payload, 'busca_comparativa.xlsx', 'exportSearch');
  }

  static exportCrossMarketplace(payload: { items: any[]; search_query?: string; target_sku: string }): Promise<void> {
    return this.downloadExport('/search/cross-marketplace/export', payload, 'busca_sku.xlsx', 'exportCrossMarketplace');
  }

  static exportCategoryProducts(payload: { items: CategoryProductItem[]; filename?: string }): Promise<void> {
    return this.downloadExport('/scrape-category/export', payload, 'produtos_categoria.xlsx', 'exportCategoryProducts');
  }

  // ------------------------------------------------------------------
  // Monitors
  // ------------------------------------------------------------------
  static getMonitors() {
    return this.request<WirePriceMonitorConfig[]>('/monitors')
      .then(monitors => monitors.map(normalizePriceMonitorConfig));
  }

  static getMonitorHistory(jobId: string, signal?: AbortSignal) {
    return this.request<WirePriceMonitorConfig>(`/monitor/history/${encodeURIComponent(jobId)}`, {}, signal)
      .then(normalizePriceMonitorConfig);
  }

  static startMonitor(data: any) {
    return this.request<WireMonitorMutationResult>('/monitor/start', {
      method: 'POST',
      body: JSON.stringify(data),
    }).then(normalizeMonitorMutationResult);
  }

  static addToMonitor(url: string, brand: string) {
    return this.request<WireMonitorMutationResult>(
      '/monitor/start',
      {
        method: 'POST',
        body: JSON.stringify({ url, brand, interval: 10, duration: 24 }),
      }
    ).then(normalizeMonitorMutationResult);
  }

  static stopMonitor(jobId: string) {
    return this.request(`/monitor/stop/${jobId}`, {
      method: 'POST',
    });
  }

  static resumeMonitor(jobId: string) {
    return this.request<WireMonitorMutationResult>(`/monitor/resume/${encodeURIComponent(jobId)}`, {
      method: 'POST',
    }).then(normalizeMonitorMutationResult);
  }

  static deleteMonitor(jobId: string) {
    return this.request(`/monitor/${jobId}`, {
      method: 'DELETE',
    });
  }

  // ------------------------------------------------------------------
  // Monitored Categories (Fase 17)
  // ------------------------------------------------------------------
  static getMonitoredCategories() {
    return this.request<any[]>('/monitor/categories');
  }

  static createMonitoredCategory(data: { url: string; brand: string }) {
    return this.request('/monitor/category', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  }

  static triggerCategoryScan(brand: string): Promise<{ enqueued: string[], reason?: string }> {
    return this.request('/monitor/category/trigger-scan', {
      method: 'POST',
      body: JSON.stringify({ brand }),
    });
  }

  static pauseCategoryMonitor(monitorId: string) {
    return this.request(`/monitor/category/${monitorId}/pause`, { method: 'POST' });
  }

  static resumeCategoryMonitor(monitorId: string) {
    return this.request(`/monitor/category/${monitorId}/resume`, { method: 'POST' });
  }

  static deleteMonitoredCategory(monitorId: string) {
    return this.request(`/monitor/category/${monitorId}`, {
      method: 'DELETE',
    });
  }

  static getMonitoredCategoryProducts(monitorId: string) {
    return this.request<any[]>(`/monitor/category/${monitorId}/products`);
  }

  static requestMonitoredProductReviews(monitorId: string, scanProductId: string, maxPages?: number) {
    const options: RequestInit = { method: 'POST' };
    if (maxPages !== undefined) {
      options.body = JSON.stringify({ max_pages: maxPages });
    }
    return this.request<ReviewCommentsResult>(
      `/monitor/category/${encodeURIComponent(monitorId)}/products/${encodeURIComponent(scanProductId)}/reviews`,
      options,
    );
  }

  static getCategoryScans(monitorId: string, signal?: AbortSignal) {
    return this.request<CategoryScanRecord[]>(
      `/monitor/category/${encodeURIComponent(monitorId)}/scans`,
      {},
      signal,
    );
  }

  static getMonitoredProductPriceHistory(monitorId: string, scanProductId: string) {
    return this.request<{ points: CategoryPriceHistoryPoint[] }>(
      `/monitor/category/${encodeURIComponent(monitorId)}/products/${encodeURIComponent(scanProductId)}/price-history`,
    );
  }

  // Reviews sob demanda para um produto individual (MON-03) — mesmo endpoint do
  // modal de detalhes, mas sem depender de um monitor de categoria.
  static getProductReviews(payload: { brand: string; product_id: string; max_pages?: number }) {
    return this.request<ReviewCommentsResult>('/search/product/reviews', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  // ------------------------------------------------------------------
  // Scrape
  // ------------------------------------------------------------------
  static startScrape(payload: any, multi = false): Promise<{ job_id: string; url?: string; urls?: Record<string, string>; message?: string }> {
    const endpoint = multi ? '/scrape-category-multi' : '/scrape-category';
    return this.request<{ job_id: string; url?: string; urls?: Record<string, string>; message?: string }>(endpoint, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  }

  static async getCanonicalCategories() {
    const data: any = await this.request('/canonical-categories');
    return data?.categories || [];
  }

  // ------------------------------------------------------------------
  // System
  // ------------------------------------------------------------------
  static getSystemStatus() {
    return this.request<any>('/system/status');
  }
}
