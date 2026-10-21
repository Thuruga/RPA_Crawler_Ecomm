import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';

import fixtureDocument from '../../../backend/tests/fixtures/phase03/conditional_offer_wire.json';
import {
  resetClerkTokenBridge,
  setClerkTokenGetter,
  setUnauthorizedHandler,
} from './clerkTokenBridge';
import {
  ApiClient,
  ApiHttpError,
  deriveCategoryLifecycle,
  deriveIndividualLifecycle,
  normalizeConditionalOffer,
  normalizeDiscountUnit,
  normalizePriceMonitorConfig,
  normalizeWireDecimal,
  type CategoryScanRecord,
  type ConditionalOffer,
  type PriceCheckRun,
  type WireConditionalOfferResult,
  type WirePriceMonitorConfig,
} from './client';


type MutationWire = {
  job_id?: string;
  status: string;
  config?: WirePriceMonitorConfig;
};

type WireFixture = {
  conditional_offer_result: WireConditionalOfferResult;
  price_monitor_config: WirePriceMonitorConfig;
  start_monitor: MutationWire;
  add_to_monitor: MutationWire;
  resume_status_only: MutationWire;
  resume_with_config: MutationWire;
};

const wire = fixtureDocument.wire as unknown as WireFixture;

const expectNormalizedOffer = (offer: ConditionalOffer | null | undefined, price = 509.15) => {
  expect(offer?.price).toBe(price);
  expect(offer?.effective_unit_price).toBe(price);
  expect(offer?.discount_value).toBe(15);
  expect(offer?.discount_unit).toBe('%');
  expect(Number.isFinite(offer?.price)).toBe(true);
};

const expectNormalizedConfig = (config: WirePriceMonitorConfig | undefined) => {
  const domain = config as unknown as {
    history: Array<{ price: number }>;
    conditional_offers_current: ConditionalOffer[];
    conditional_offer_history: Array<{ before?: ConditionalOffer | null; after?: ConditionalOffer | null }>;
  };
  expect(domain.history[0].price).toBe(599);
  expectNormalizedOffer(domain.conditional_offers_current[0]);
  expectNormalizedOffer(domain.conditional_offer_history[0].before, 599);
  expectNormalizedOffer(domain.conditional_offer_history[0].after);
};

afterEach(() => {
  vi.restoreAllMocks();
  resetClerkTokenBridge();
});

describe('history HTTP contract', () => {
  it('preserves 404 and a bounded safe detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: vi.fn().mockResolvedValue({ detail: 'Historico ausente', internal: { secret: true } }),
    }));

    await expect(ApiClient.getHistoryDetail('job/id 1')).rejects.toMatchObject({
      name: 'ApiHttpError',
      status: 404,
      detail: 'Historico ausente',
    });
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/history/job%2Fid%201'), expect.any(Object));
  });

  it('preserves 500 without exposing non-string payloads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: vi.fn().mockResolvedValue({ detail: { stack: 'private' } }),
    }));

    const failure = ApiClient.getHistoryDetail('job');
    await expect(failure).rejects.toBeInstanceOf(ApiHttpError);
    await expect(failure).rejects.toMatchObject({ status: 500, detail: undefined, message: 'API Error: 500' });
  });

  it('keeps network and abort failures distinguishable', async () => {
    const networkError = new TypeError('Failed to fetch');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValueOnce(networkError));
    await expect(ApiClient.getHistoryDetail('job')).rejects.toBe(networkError);

    const abortError = new DOMException('aborted', 'AbortError');
    vi.mocked(fetch).mockRejectedValueOnce(abortError);
    await expect(ApiClient.request('/history/job', {}, new AbortController().signal)).rejects.toBe(abortError);
  });

  it('encodes history identifiers for GET and DELETE', async () => {
    const request = vi.spyOn(ApiClient, 'request').mockResolvedValue({} as never);
    await ApiClient.getHistoryDetail('job/id 1');
    await ApiClient.deleteHistory('job/id 1');
    expect(request).toHaveBeenNthCalledWith(1, '/history/job%2Fid%201', {}, undefined);
    expect(request).toHaveBeenNthCalledWith(2, '/history/job%2Fid%201', { method: 'DELETE' });
  });
});

describe('conditional-offer wire normalization', () => {
  it('accepts only finite numbers or strict decimal strings', () => {
    expect(normalizeWireDecimal(509.15)).toBe(509.15);
    expect(normalizeWireDecimal('509.15')).toBe(509.15);
    expect(normalizeWireDecimal(' 15 ')).toBe(15);
    expect(normalizeWireDecimal('')).toBeNull();
    expect(normalizeWireDecimal('R$ 509,15')).toBeNull();
    expect(normalizeWireDecimal('1e3')).toBeNull();
    expect(normalizeWireDecimal(Number.NaN)).toBeNull();
    expect(normalizeWireDecimal(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it('converges percent and % to the sole domain literal, and preserves amount', () => {
    expect(normalizeDiscountUnit('percent')).toBe('%');
    expect(normalizeDiscountUnit('%')).toBe('%');
    expect(normalizeDiscountUnit(' Percent ')).toBe('%');
    expect(normalizeDiscountUnit('amount')).toBe('amount');
    expect(normalizeDiscountUnit(' Amount ')).toBe('amount');
    expect(normalizeDiscountUnit('unknown')).toBeNull();
    expect(normalizeDiscountUnit(undefined)).toBeNull();
    expectTypeOf<ConditionalOffer['discount_unit']>().toEqualTypeOf<'%' | 'amount' | null | undefined>();
  });

  it('turns invalid nested offer values into null rather than zero or NaN', () => {
    const normalized = normalizeConditionalOffer({
      ...wire.conditional_offer_result.offers?.[0],
      condition_type: 'membership',
      condition_label: 'Cliente Prime',
      eligibility: 'requires_membership',
      state: 'confirmed',
      currency: 'BRL',
      price: '',
      effective_unit_price: 'R$ 509,15',
      discount_value: Number.NaN,
      discount_unit: 'unknown-unit',
    });
    expect(normalized.price).toBeNull();
    expect(normalized.effective_unit_price).toBeNull();
    expect(normalized.discount_value).toBeNull();
    expect(normalized.discount_unit).toBeNull();
  });

  it('preserves the amount discount unit through normalizeConditionalOffer', () => {
    const normalized = normalizeConditionalOffer({
      ...wire.conditional_offer_result.offers?.[0],
      condition_type: 'checkout',
      condition_label: 'Desconto no checkout',
      eligibility: 'requires_checkout',
      state: 'confirmed',
      currency: 'BRL',
      discount_value: 10,
      discount_unit: 'amount',
    });
    expect(normalized.discount_unit).toBe('amount');
  });
});

describe('ApiClient six-method wiring matrix', () => {
  it('normalizes getConditionalOffers and preserves its request contract', async () => {
    const request = vi.spyOn(ApiClient, 'request').mockResolvedValue(wire.conditional_offer_result as never);
    const payload = {
      brand: 'reserva',
      url: 'https://www.usereserva.com/camisa/p',
      trigger: 'product_opened' as const,
      force: false,
    };

    const result = await ApiClient.getConditionalOffers(payload);

    expect(request).toHaveBeenCalledWith(
      '/search/product/conditional-offers',
      { method: 'POST', body: JSON.stringify(payload) },
      undefined,
    );
    expectNormalizedOffer(result.offers[0]);
  });

  it('normalizes getMonitors current and history without changing public history', async () => {
    const request = vi.spyOn(ApiClient, 'request').mockResolvedValue([wire.price_monitor_config] as never);

    const result = await ApiClient.getMonitors();

    expect(request).toHaveBeenCalledWith('/monitors');
    expectNormalizedConfig(result[0] as unknown as WirePriceMonitorConfig);
  });

  it('normalizes getMonitorHistory current, before and after', async () => {
    const request = vi.spyOn(ApiClient, 'request').mockResolvedValue(wire.price_monitor_config as never);

    const result = await ApiClient.getMonitorHistory('job/wire 1');

    expect(request).toHaveBeenCalledWith('/monitor/history/job%2Fwire%201', {}, undefined);
    expectNormalizedConfig(result as unknown as WirePriceMonitorConfig);
  });

  it('forwards an AbortSignal to getMonitorHistory without changing id encoding', async () => {
    const request = vi.spyOn(ApiClient, 'request').mockResolvedValue(wire.price_monitor_config as never);
    const controller = new AbortController();

    await ApiClient.getMonitorHistory('job/wire 1', controller.signal);

    expect(request).toHaveBeenCalledWith('/monitor/history/job%2Fwire%201', {}, controller.signal);
  });

  it('normalizes startMonitor and addToMonitor mutation configs', async () => {
    const startWire = { ...wire.start_monitor, config: wire.price_monitor_config };
    const addWire = { ...wire.add_to_monitor, config: wire.price_monitor_config };
    const request = vi.spyOn(ApiClient, 'request')
      .mockResolvedValueOnce(startWire as never)
      .mockResolvedValueOnce(addWire as never);
    const startPayload = {
      url: 'https://www.usereserva.com/camisa/p',
      brand: 'reserva',
      interval: 5,
      duration: 12,
    };

    const started = await ApiClient.startMonitor(startPayload);
    const added = await ApiClient.addToMonitor(startPayload.url, startPayload.brand);

    expect(request).toHaveBeenNthCalledWith(1, '/monitor/start', {
      method: 'POST',
      body: JSON.stringify(startPayload),
    });
    expect(request).toHaveBeenNthCalledWith(2, '/monitor/start', {
      method: 'POST',
      body: JSON.stringify({
        url: startPayload.url,
        brand: startPayload.brand,
        interval: 10,
        duration: 24,
      }),
    });
    expectNormalizedConfig(started.config as unknown as WirePriceMonitorConfig);
    expectNormalizedConfig(added.config as unknown as WirePriceMonitorConfig);
  });

  it('keeps resume status-only intact and normalizes an optional config', async () => {
    const resumeWithConfig = { ...wire.resume_with_config, config: wire.price_monitor_config };
    const request = vi.spyOn(ApiClient, 'request')
      .mockResolvedValueOnce(wire.resume_status_only as never)
      .mockResolvedValueOnce(resumeWithConfig as never);

    const statusOnly = await ApiClient.resumeMonitor('job-wire-1');
    const enriched = await ApiClient.resumeMonitor('job-wire-1');

    expect(request).toHaveBeenNthCalledWith(1, '/monitor/resume/job-wire-1', { method: 'POST' });
    expect(request).toHaveBeenNthCalledWith(2, '/monitor/resume/job-wire-1', { method: 'POST' });
    expect(statusOnly).toEqual({ status: 'resumed' });
    expect('config' in statusOnly).toBe(false);
    expectNormalizedConfig(enriched.config as unknown as WirePriceMonitorConfig);
  });
});

describe('deriveIndividualLifecycle', () => {
  const individualConfig = (overrides: Record<string, unknown>) => ({
    job_id: 'job-1',
    url: 'https://www.usereserva.com/camisa/p',
    brand: 'reserva',
    last_checked_at: '2026-07-20T10:00:00Z',
    last_success_at: '2026-07-19T10:00:00Z',
    ...overrides,
  }) as unknown as import('./client').PriceMonitorConfig;

  it('maps ok/stale/blocked/error to the 5-state vocabulary', () => {
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok' })).state).toBe('success');
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'stale' })).state).toBe('stale');
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'blocked' })).state).toBe('error_blocked');
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'error' })).state).toBe('error_blocked');
  });

  it('falls back to pending for null/undefined/unrecognized last_status (totality)', () => {
    expect(deriveIndividualLifecycle(individualConfig({ last_status: null })).state).toBe('pending');
    expect(deriveIndividualLifecycle(individualConfig({ last_status: undefined })).state).toBe('pending');
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'legacy-unknown' })).state).toBe('pending');
  });

  it('echoes lastAttemptAt from last_checked_at and lastSuccessAt from last_success_at', () => {
    const result = deriveIndividualLifecycle(individualConfig({ last_status: 'ok' }));
    expect(result.lastAttemptAt).toBe('2026-07-20T10:00:00Z');
    expect(result.lastSuccessAt).toBe('2026-07-19T10:00:00Z');
  });

  it('echoes nextRetryAt from next_retry_at', () => {
    const result = deriveIndividualLifecycle(individualConfig({
      last_status: 'stale',
      next_retry_at: '2026-07-30T01:00:00Z',
      next_check_due_at: '2026-07-30T01:10:00Z',
    }));
    expect(result.nextRetryAt).toBe('2026-07-30T01:00:00Z');
    expect(result.nextScheduledAttemptAt).toBe('2026-07-30T01:10:00Z');
    expect(result.state).toBe('stale');
  });

  it('echoes nextScheduledAttemptAt from the monitor durable next_check_due_at cursor', () => {
    const result = deriveIndividualLifecycle(individualConfig({
      last_status: 'ok',
      next_check_due_at: '2026-07-30T02:00:00Z',
    }));

    expect(result.nextScheduledAttemptAt).toBe('2026-07-30T02:00:00Z');
  });

  it('returns null nextScheduledAttemptAt when the backend omits or clears the durable cursor', () => {
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok' })).nextScheduledAttemptAt).toBeNull();
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok', next_check_due_at: null })).nextScheduledAttemptAt).toBeNull();
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok', next_check_due_at: undefined })).nextScheduledAttemptAt).toBeNull();
  });

  it('returns null nextRetryAt when the backend omits it', () => {
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok' })).nextRetryAt).toBeNull();
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok', next_retry_at: null })).nextRetryAt).toBeNull();
    expect(deriveIndividualLifecycle(individualConfig({ last_status: 'ok', next_retry_at: undefined })).nextRetryAt).toBeNull();
  });
});

describe('deriveCategoryLifecycle', () => {
  const monitor = { last_scraped_at: '2026-07-18T09:00:00Z' };

  it('returns pending with null timestamps for zero scans', () => {
    expect(deriveCategoryLifecycle(monitor, [])).toEqual({
      state: 'pending',
      lastAttemptAt: null,
      lastSuccessAt: null,
      nextRetryAt: null,
      nextScheduledAttemptAt: null,
    });
  });

  it('maps the latest scan status to running/pending/success', () => {
    expect(deriveCategoryLifecycle(monitor, [{ status: 'running', created_at: '2026-07-01T00:00:00Z' }]).state).toBe('running');
    expect(deriveCategoryLifecycle(monitor, [{ status: 'pending', created_at: '2026-07-01T00:00:00Z' }]).state).toBe('pending');
    expect(deriveCategoryLifecycle(monitor, [{ status: 'completed', created_at: '2026-07-01T00:00:00Z', completed_at: '2026-07-01T00:05:00Z' }]).state).toBe('success');
  });

  it('derives stale when the latest scan failed with a prior completed scan, error_blocked when there is none', () => {
    const withPriorSuccess = [
      { status: 'completed', created_at: '2026-07-01T00:00:00Z', completed_at: '2026-07-01T00:05:00Z' },
      { status: 'failed', created_at: '2026-07-02T00:00:00Z', completed_at: '2026-07-02T00:01:00Z' },
    ];
    expect(deriveCategoryLifecycle(monitor, withPriorSuccess).state).toBe('stale');

    const withoutPriorSuccess = [
      { status: 'failed', created_at: '2026-07-02T00:00:00Z', completed_at: '2026-07-02T00:01:00Z' },
    ];
    expect(deriveCategoryLifecycle(monitor, withoutPriorSuccess).state).toBe('error_blocked');
  });

  it('sorts scans by created_at before choosing latest/last-success, never trusting array position', () => {
    const outOfOrder = [
      { status: 'completed', created_at: '2026-07-03T00:00:00Z', completed_at: '2026-07-03T00:05:00Z' },
      { status: 'completed', created_at: '2026-07-01T00:00:00Z', completed_at: '2026-07-01T00:05:00Z' },
      { status: 'failed', created_at: '2026-07-02T00:00:00Z', completed_at: '2026-07-02T00:01:00Z' },
    ];

    const result = deriveCategoryLifecycle(monitor, outOfOrder);

    expect(result.state).toBe('success');
    expect(result.lastAttemptAt).toBe('2026-07-03T00:05:00Z');
    expect(result.lastSuccessAt).toBe('2026-07-03T00:05:00Z');
  });

  it('derives lastAttemptAt from the latest scan and falls back lastSuccessAt to monitor.last_scraped_at', () => {
    const scans = [{ status: 'running', created_at: '2026-07-05T00:00:00Z' }];

    const result = deriveCategoryLifecycle(monitor, scans);

    expect(result.lastAttemptAt).toBe('2026-07-05T00:00:00Z');
    expect(result.lastSuccessAt).toBe(monitor.last_scraped_at);
  });

  it('always returns null nextRetryAt because the backend has no category next-retry', () => {
    expect(deriveCategoryLifecycle(monitor, []).nextRetryAt).toBeNull();
    expect(deriveCategoryLifecycle(monitor, [{ status: 'running', created_at: '2026-07-01T00:00:00Z' }]).nextRetryAt).toBeNull();
    expect(deriveCategoryLifecycle(monitor, [{ status: 'pending', created_at: '2026-07-01T00:00:00Z' }]).nextRetryAt).toBeNull();
    expect(deriveCategoryLifecycle(monitor, [{ status: 'completed', created_at: '2026-07-01T00:00:00Z', completed_at: '2026-07-01T00:05:00Z' }]).nextRetryAt).toBeNull();
    const withPriorSuccess = [
      { status: 'completed', created_at: '2026-07-01T00:00:00Z', completed_at: '2026-07-01T00:05:00Z' },
      { status: 'failed', created_at: '2026-07-02T00:00:00Z', completed_at: '2026-07-02T00:01:00Z' },
    ];
    expect(deriveCategoryLifecycle(monitor, withPriorSuccess).nextRetryAt).toBeNull();
    const withoutPriorSuccess = [
      { status: 'failed', created_at: '2026-07-02T00:00:00Z', completed_at: '2026-07-02T00:01:00Z' },
    ];
    expect(deriveCategoryLifecycle(monitor, withoutPriorSuccess).nextRetryAt).toBeNull();
  });

  it('always returns null nextScheduledAttemptAt because the backend has no category schedule cursor', () => {
    expect(deriveCategoryLifecycle(monitor, []).nextScheduledAttemptAt).toBeNull();
    expect(deriveCategoryLifecycle(monitor, [{ status: 'running', created_at: '2026-07-01T00:00:00Z' }]).nextScheduledAttemptAt).toBeNull();
  });
});

// JOB-01 (Plan 07-13): category_scan e price_check completos, sem coerção de
// null->0/false e com INTERRUPTED distinto de FAILED — espelha os payloads
// que backend/tests/test_monitor_job_snapshots.py já prova sobre HTTP.
describe('getCategoryScans wire fidelity (JOB-01)', () => {
  const inFlightScan: CategoryScanRecord = {
    scan_id: 'scan-1',
    monitor_id: 'monitor-1',
    brand: 'aramis',
    category_url: 'https://example.test/camisas',
    origin: 'manual',
    status: 'running',
    kind: 'category_scan',
    input: {},
    attempt: 1,
    progress: {
      stage: 'listing',
      completed_units: 2,
      total_units: null,
      percent: null,
      message: null,
    },
    attempts: [],
    created_at: '2026-07-26T20:00:00+00:00',
    started_at: '2026-07-26T20:00:01+00:00',
    updated_at: '2026-07-26T20:00:02+00:00',
    completed_at: null,
    error_message: null,
    product_count: 2,
    logical_cycle_id: 'category_scan:monitor-1:scan-1',
    scheduled_for: null,
  };

  it('encodes the monitor id and forwards an AbortSignal', async () => {
    const request = vi.spyOn(ApiClient, 'request').mockResolvedValue([inFlightScan] as never);
    const controller = new AbortController();

    await ApiClient.getCategoryScans('monitor/id 1', controller.signal);

    expect(request).toHaveBeenCalledWith(
      '/monitor/category/monitor%2Fid%201/scans',
      {},
      controller.signal,
    );
  });

  it('returns in-flight progress with total_units/percent honestly null, never coerced to zero', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue([inFlightScan] as never);

    const [scan] = await ApiClient.getCategoryScans('monitor-1');

    expect(scan.progress.completed_units).toBe(2);
    expect(scan.progress.total_units).toBeNull();
    expect(scan.progress.percent).toBeNull();
    expect(scan.logical_cycle_id).toBe('category_scan:monitor-1:scan-1');
  });

  it('keeps INTERRUPTED distinct from FAILED and preserves product_count', async () => {
    const interrupted: CategoryScanRecord = {
      ...inFlightScan,
      status: 'interrupted',
      product_count: 5,
      error_message: null,
    };
    vi.spyOn(ApiClient, 'request').mockResolvedValue([interrupted] as never);

    const [scan] = await ApiClient.getCategoryScans('monitor-1');

    expect(scan.status).toBe('interrupted');
    expect(scan.status).not.toBe('failed');
    expect(scan.product_count).toBe(5);
  });

  it('preserves a sanitized error_message for a failed scan without altering it', async () => {
    const failed: CategoryScanRecord = {
      ...inFlightScan,
      status: 'failed',
      error_message: 'anti-bot bloqueou a listagem',
    };
    vi.spyOn(ApiClient, 'request').mockResolvedValue([failed] as never);

    const [scan] = await ApiClient.getCategoryScans('monitor-1');

    expect(scan.error_message).toBe('anti-bot bloqueou a listagem');
  });

  it('returns an empty list for a monitor with no scans rather than inventing one', async () => {
    vi.spyOn(ApiClient, 'request').mockResolvedValue([] as never);

    const scans = await ApiClient.getCategoryScans('does-not-exist');

    expect(scans).toEqual([]);
  });
});

describe('normalizePriceMonitorConfig check_runs fidelity (JOB-01)', () => {
  const baseWireConfig: WirePriceMonitorConfig = {
    job_id: 'job-1',
    url: 'https://example.test/produto',
    brand: 'aramis',
    history: [],
  };

  it('defaults to an empty ledger when check_runs is absent, never fabricating a run', () => {
    const normalized = normalizePriceMonitorConfig(baseWireConfig);
    expect(normalized.check_runs).toEqual([]);
  });

  it('passes next_retry_at through without fabricating a default', () => {
    const withNextRetry = normalizePriceMonitorConfig({ ...baseWireConfig, next_retry_at: '2026-07-30T01:00:00Z' });
    expect(withNextRetry.next_retry_at).toBe('2026-07-30T01:00:00Z');

    const withoutNextRetry = normalizePriceMonitorConfig(baseWireConfig);
    expect(withoutNextRetry.next_retry_at == null).toBe(true);
  });

  it('preserves an in-flight run with status null and completed_at null (never coerced)', () => {
    const inFlightRun: PriceCheckRun = {
      run_id: 'run-1',
      logical_cycle_id: 'price_check:job-1:2026-07-26T20:10:00+00:00',
      scheduled_for: '2026-07-26T20:10:00+00:00',
      status: null,
      created_at: '2026-07-26T20:09:00+00:00',
      started_at: '2026-07-26T20:09:01+00:00',
      completed_at: null,
      error: null,
    };
    const normalized = normalizePriceMonitorConfig({ ...baseWireConfig, check_runs: [inFlightRun] });

    expect(normalized.check_runs).toEqual([inFlightRun]);
    expect(normalized.check_runs[0].status).toBeNull();
  });

  it('keeps ok/blocked/error/stale/interrupted distinct and in received order', () => {
    const terminalStates: Array<PriceCheckRun['status']> = ['ok', 'blocked', 'error', 'stale', 'interrupted'];
    const runs: PriceCheckRun[] = terminalStates.map((status, index) => ({
      run_id: `run-${index}`,
      logical_cycle_id: `price_check:job-1:cycle-${index}`,
      scheduled_for: `2026-07-26T20:0${index}:00+00:00`,
      status,
      created_at: `2026-07-26T20:0${index}:00+00:00`,
      started_at: null,
      completed_at: `2026-07-26T20:0${index}:30+00:00`,
      error: status === 'error' ? 'sanitized failure' : null,
    }));

    const normalized = normalizePriceMonitorConfig({ ...baseWireConfig, check_runs: runs });

    expect(normalized.check_runs.map(run => run.status)).toEqual(terminalStates);
    expect(normalized.check_runs.find(run => run.status === 'error')?.error).toBe('sanitized failure');
  });
});

describe('ApiClient bearer auth and 401 notification contract', () => {
  it('sends Authorization: Bearer <token> and no legacy API key when token is present', async () => {
    setClerkTokenGetter(async () => 'tok-123');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ data: 'ok' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await ApiClient.request('/test-endpoint');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer tok-123');

    const headerKeys = Object.keys(headers).map(k => k.toLowerCase());
    expect(headerKeys).not.toContain('x-api-key');
  });

  it('sends no Authorization header and no legacy API key when token is null', async () => {
    setClerkTokenGetter(async () => null);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue({ data: 'ok' }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await ApiClient.request('/test-endpoint');

    const [, init] = fetchMock.mock.calls[0];
    const headers = init.headers as Record<string, string>;
    expect(headers['Authorization']).toBeUndefined();
    const headerKeys = Object.keys(headers).map(k => k.toLowerCase());
    expect(headerKeys).not.toContain('x-api-key');
  });

  it('sends bearer token in protectedBlob and downloadExport', async () => {
    setClerkTokenGetter(async () => 'blob-token');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: vi.fn().mockResolvedValue(new Blob(['test'])),
      headers: { get: () => null },
    });
    vi.stubGlobal('fetch', fetchMock);

    await ApiClient.getBannerAssetBlob('job1', 'banner1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const headersBlob = fetchMock.mock.calls[0][1]?.headers as Record<string, string>;
    expect(headersBlob['Authorization']).toBe('Bearer blob-token');
    expect(Object.keys(headersBlob).map(k => k.toLowerCase())).not.toContain('x-api-key');

    fetchMock.mockClear();
    const originalWindow = (globalThis as any).window;
    const originalDocument = (globalThis as any).document;
    (globalThis as any).window = {
      URL: {
        createObjectURL: vi.fn().mockReturnValue('blob:mock-url'),
        revokeObjectURL: vi.fn(),
      },
    };
    (globalThis as any).document = {
      createElement: vi.fn().mockReturnValue({ click: vi.fn() }),
      body: { appendChild: vi.fn(), removeChild: vi.fn() },
    };

    try {
      await ApiClient.exportSearch({ target_sku: 'sku1' } as any);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const headersExport = fetchMock.mock.calls[0][1]?.headers as Record<string, string>;
      expect(headersExport['Authorization']).toBe('Bearer blob-token');
      expect(Object.keys(headersExport).map(k => k.toLowerCase())).not.toContain('x-api-key');
    } finally {
      (globalThis as any).window = originalWindow;
      (globalThis as any).document = originalDocument;
    }
  });

  it('notifies unauthorized with hadToken=true on 401 when token was sent', async () => {
    setClerkTokenGetter(async () => 'valid-format-token');
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: vi.fn().mockResolvedValue({ error: 'UNAUTHENTICATED', message: 'Token inválido' }),
    }));

    await expect(ApiClient.request('/protected')).rejects.toBeInstanceOf(ApiHttpError);
    expect(handler).toHaveBeenCalledWith(true);
  });

  it('notifies unauthorized with hadToken=false on 401 when no token was sent', async () => {
    setClerkTokenGetter(async () => null);
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: vi.fn().mockResolvedValue({ error: 'UNAUTHENTICATED', message: 'Autenticação necessária' }),
    }));

    await expect(ApiClient.request('/protected')).rejects.toBeInstanceOf(ApiHttpError);
    expect(handler).toHaveBeenCalledWith(false);
  });

  it('never calls unauthorized handler on non-401 errors', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: vi.fn().mockResolvedValue({ detail: 'Forbidden' }),
    }));

    await expect(ApiClient.request('/forbidden')).rejects.toBeInstanceOf(ApiHttpError);
    expect(handler).not.toHaveBeenCalled();
  });

  it('parses flat Aramis {error, message} response body in safeApiDetail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: vi.fn().mockResolvedValue({ error: 'UNAUTHENTICATED', message: 'Autenticação necessária' }),
    }));

    await expect(ApiClient.request('/test')).rejects.toMatchObject({
      status: 401,
      detail: 'Autenticação necessária',
      message: 'Autenticação necessária',
    });
  });

  it('prefers detail over message when both exist', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: vi.fn().mockResolvedValue({ detail: 'Specific detail', message: 'General message' }),
    }));

    await expect(ApiClient.request('/test')).rejects.toMatchObject({
      status: 400,
      detail: 'Specific detail',
    });
  });
});

