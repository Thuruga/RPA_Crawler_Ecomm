import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  SNAPSHOT_CACHE_LIMIT,
  captureEntrySnapshot,
  clearEntrySnapshots,
  restoreEntrySnapshot,
  type CrossEntrySnapshot,
  type SearchEntrySnapshot,
} from '../routing/entrySnapshots'
import { ApiClient } from '../api/client'
import { useNotificationStore } from './notificationStore'
import { useSearchStore } from './searchStore'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

describe('entry snapshots por history entry key', () => {
  beforeEach(() => {
    clearEntrySnapshots()
  })

  it('isola snapshots de entradas diferentes e devolve clones', () => {
    const first: SearchEntrySnapshot = {
      kind: 'search',
      query: 'tenis',
      sort: 'price_asc',
      zipcode: '01001000',
      selectedBrands: ['Nike'],
      results: { results: [{ id: 'first' }] },
    }
    const second: CrossEntrySnapshot = {
      kind: 'cross',
      targetSku: 'SKU-2',
      zipcode: '20040002',
      results: { results: [{ id: 'second' }] },
    }

    captureEntrySnapshot('entry-a', first)
    captureEntrySnapshot('entry-b', second)

    const restoredFirst = restoreEntrySnapshot('entry-a')
    const restoredSecond = restoreEntrySnapshot('entry-b')

    expect(restoredFirst).toEqual(first)
    expect(restoredSecond).toEqual(second)
    expect(restoredFirst).not.toBe(first)
    expect(restoredSecond).not.toBe(second)
  })

  it('mantem somente os campos transitorios autorizados', () => {
    const search: SearchEntrySnapshot = {
      kind: 'search',
      query: 'camiseta',
      sort: 'relevance',
      zipcode: '',
      selectedBrands: [],
      results: null,
    }
    const cross: CrossEntrySnapshot = {
      kind: 'cross',
      targetSku: 'ABC',
      zipcode: '',
      results: null,
    }

    expect(Object.keys(search).sort()).toEqual([
      'kind', 'query', 'results', 'selectedBrands', 'sort', 'zipcode',
    ])
    expect(Object.keys(cross).sort()).toEqual([
      'kind', 'results', 'targetSku', 'zipcode',
    ])
  })

  it('limita o cache em memoria com descarte FIFO', () => {
    for (let index = 0; index <= SNAPSHOT_CACHE_LIMIT; index += 1) {
      captureEntrySnapshot(`entry-${index}`, {
        kind: 'search',
        query: `query-${index}`,
        sort: 'relevance',
        zipcode: '',
        selectedBrands: [],
        results: null,
      })
    }

    expect(restoreEntrySnapshot('entry-0')).toBeUndefined()
    expect(restoreEntrySnapshot(`entry-${SNAPSHOT_CACHE_LIMIT}`)).toBeDefined()
  })

  it('fica vazio apos recriacao do contexto do modulo simulada por reload', () => {
    captureEntrySnapshot('entry-a', {
      kind: 'search',
      query: 'antes do reload',
      sort: 'relevance',
      zipcode: '',
      selectedBrands: [],
      results: null,
    })

    clearEntrySnapshots()

    expect(restoreEntrySnapshot('entry-a')).toBeUndefined()
  })
})


describe('integração dos snapshots com o search store', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearEntrySnapshots()
    useNotificationStore.getState().clearNotifications()
    useSearchStore.getState().resetSearchSlice()
    useSearchStore.getState().resetCrossSlice()
  })

  it('captura apenas o estado navegavel do slice comparativo', () => {
    useSearchStore.getState().setSearch({
      query: 'tenis',
      sort: 'price_desc',
      zipcode: '01001000',
      selectedBrands: ['Nike'],
      results: { results: [{ id: 'saved' }] },
      loading: true,
      abortController: new AbortController(),
      loadingPreloadId: 'preload-id',
    })
    useSearchStore.getState().captureNavigableSnapshot('base-a', 'search')
    expect(restoreEntrySnapshot('base-a')).toEqual({
      kind: 'search',
      query: 'tenis',
      sort: 'price_desc',
      zipcode: '01001000',
      selectedBrands: ['Nike'],
      results: { results: [{ id: 'saved' }] },
    })
  })

  it('restaura o slice comparativo atomicamente e reinicia campos visuais', () => {
    useSearchStore.getState().setSearch({
      query: 'estado salvo',
      sort: 'relevance',
      zipcode: '',
      selectedBrands: ['Reserva'],
      results: { results: [{ id: 'original' }] },
    })
    useSearchStore.getState().captureNavigableSnapshot('base-a', 'search')

    const controller = new AbortController()
    const abort = vi.spyOn(controller, 'abort')
    useSearchStore.getState().setSearch({
      query: 'deep link',
      sort: 'price_asc',
      selectedBrands: [],
      results: { results: [{ id: 'deep' }] },
      loading: true,
      abortController: controller,
      loadingPreloadId: 'job-id',
    })

    expect(useSearchStore.getState().restoreNavigableSnapshot('base-a', 'search')).toBe(true)
    expect(abort).toHaveBeenCalledOnce()
    expect(useSearchStore.getState().search).toEqual({
      query: 'estado salvo',
      sort: 'relevance',
      zipcode: '',
      selectedBrands: ['Reserva'],
      results: { results: [{ id: 'original' }] },
      loading: false,
      abortController: null,
      loadingPreloadId: null,
    })
  })

  it('restaura cross sem selecao de exportacao ou modo visual', () => {
    useSearchStore.getState().setCross({
      targetSku: 'SKU-1',
      zipcode: '20040002',
      results: { results: [{ id: 'saved' }] },
      selectedItems: new Set(['saved']),
      selectionMode: true,
    })
    useSearchStore.getState().captureNavigableSnapshot('sku-a', 'cross')
    useSearchStore.getState().setCross({
      targetSku: 'SKU-2',
      selectedItems: new Set(['deep']),
      selectionMode: true,
    })

    expect(useSearchStore.getState().restoreNavigableSnapshot('sku-a', 'cross')).toBe(true)
    expect(useSearchStore.getState().cross).toEqual({
      targetSku: 'SKU-1',
      zipcode: '20040002',
      results: { results: [{ id: 'saved' }] },
      selectedItems: new Set(),
      selectionMode: false,
      loading: false,
      abortController: null,
      loadingPreloadId: null,
    })
  })

  it('limpa o slice quando a entry key e desconhecida ou de outro tipo', () => {
    captureEntrySnapshot('cross-only', {
      kind: 'cross',
      targetSku: 'SKU',
      zipcode: '',
      results: null,
    })
    useSearchStore.getState().setSearch({
      query: 'nao deve sobreviver',
      results: { results: [{ id: 'stale' }] },
    })

    expect(useSearchStore.getState().restoreNavigableSnapshot('missing', 'search')).toBe(false)
    expect(useSearchStore.getState().search.query).toBe('')

    useSearchStore.getState().setSearch({ query: 'tambem nao sobrevive' })
    expect(useSearchStore.getState().restoreNavigableSnapshot('cross-only', 'search')).toBe(false)
    expect(useSearchStore.getState().search.query).toBe('')
  })

  it('impede que resposta tardia clobbere o snapshot restaurado', async () => {
    let resolveRequest!: (value: unknown) => void
    vi.spyOn(ApiClient, 'search').mockImplementation(() => new Promise((resolve) => {
      resolveRequest = resolve
    }) as never)

    useSearchStore.getState().setSearch({
      query: 'estado anterior',
      results: { results: [{ id: 'restored' }] },
    })
    useSearchStore.getState().captureNavigableSnapshot('base-a', 'search')

    const pending = useSearchStore.getState().startSearch({ query: 'nova busca' })
    useSearchStore.getState().restoreNavigableSnapshot('base-a', 'search')
    resolveRequest({ results: [{ id: 'late' }] })

    await expect(pending).resolves.toEqual({ status: 'aborted' })
    expect(useSearchStore.getState().search.results).toEqual({
      results: [{ id: 'restored' }],
    })
  })
  it('aceita a busca comparativa (202) sem criar notificacao local nem preencher results', async () => {
    // D-08 (Plan 07-07): 'success' agora e aceitacao do job, nao conclusao —
    // o backend e o unico produtor da notificacao/resultado terminal.
    vi.spyOn(ApiClient, 'search').mockResolvedValue({
      job_id: 'job-search-1', status: 'pending', status_url: '/history/job-search-1', poll_after_ms: 1500,
    } as never)

    await expect(useSearchStore.getState().startSearch({ query: 'tenis' })).resolves.toEqual({ status: 'success', jobId: 'job-search-1' })

    expect(useSearchStore.getState().search.results).toBeNull()
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })

  it('aceita a busca SKU (202) sem criar notificacao local nem preencher results', async () => {
    vi.spyOn(ApiClient, 'crossMarketplaceSearch').mockResolvedValue({
      job_id: 'job-cross-1', status: 'pending', status_url: '/history/job-cross-1', poll_after_ms: 1500,
    } as never)

    await expect(useSearchStore.getState().startCrossSearch({ target_sku: 'SKU-1' })).resolves.toEqual({ status: 'success', jobId: 'job-cross-1' })

    expect(useSearchStore.getState().cross.results).toBeNull()
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })

  it.each([
    ['search', () => useSearchStore.getState().startSearch({ query: 'tenis' })],
    ['cross', () => useSearchStore.getState().startCrossSearch({ target_sku: 'SKU-1' })],
  ] as const)('mantem erro local nao navegavel para %s', async (kind, start) => {
    const method = kind === 'search' ? 'search' : 'crossMarketplaceSearch'
    vi.spyOn(ApiClient, method).mockRejectedValue(new Error('falha controlada'))

    await expect(start()).resolves.toEqual({ status: 'error' })

    const notifications = useNotificationStore.getState().notifications
    expect(notifications).toHaveLength(1)
    expect(notifications[0]).toMatchObject({ kind: 'error', message: 'falha controlada' })
    expect(notifications[0].routeTarget).toBeUndefined()
  })
})
