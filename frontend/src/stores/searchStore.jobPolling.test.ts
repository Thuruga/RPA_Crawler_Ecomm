// W0-05 (parcial, Plan 07-07): contrato assíncrono 202 de D-08 no frontend —
// submissão devolve job_id sem fingir conclusão, e resposta obsoleta não
// clobbera o job vigente. O polling em si (setTimeout/setInterval sobre
// GET /history/{job_id}) entra no Plan 07-08; este arquivo cobre apenas a
// metade de submissão/identidade/navegação do W0-05.
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { deepSearchPath } from '../routing/paths'
import { useNotificationStore } from './notificationStore'
import { useSearchStore } from './searchStore'

vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}))

// Import depois do mock (hoisting do vi.mock garante que o módulo real nunca roda).
import { toast } from 'sonner'

type FetchResponseInit = { status: number; body: unknown }

// fetch hermético: nenhuma chamada real de rede, apenas a forma exata que
// ApiClient.request espera (response.ok + response.json()).
const fakeFetchResponse = ({ status, body }: FetchResponseInit) => ({
  ok: status >= 200 && status < 300,
  status,
  json: vi.fn().mockResolvedValue(body),
})

const jobAcceptedBody = (jobId: string) => ({
  job_id: jobId,
  status: 'pending',
  status_url: `/history/${jobId}`,
  poll_after_ms: 1500,
})

describe('submissão assíncrona de busca — contrato 202 (D-08/W0-05)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    useNotificationStore.getState().clearNotifications()
    useSearchStore.getState().resetSearchSlice()
    useSearchStore.getState().resetCrossSlice()
  })

  it.each([
    ['search', () => useSearchStore.getState().startSearch({ query: 'tenis' }), () => useSearchStore.getState().search] as const,
    ['cross', () => useSearchStore.getState().startCrossSearch({ target_sku: 'SKU-1' }), () => useSearchStore.getState().cross] as const,
  ])('%s: devolve o job_id aceito sem preencher results nem notificar conclusão prematura', async (_kind, start, slice) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(fakeFetchResponse({ status: 202, body: jobAcceptedBody('job-abc') })))

    await expect(start()).resolves.toEqual({ status: 'success', jobId: 'job-abc' })

    // results continua null após 202 — o backend é o único produtor do terminal.
    expect(slice().results).toBeNull()
    // Nenhum toast de conclusão é emitido na aceitação (D-08).
    expect(toast.success).not.toHaveBeenCalled()
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })

  it('resposta tardia de uma submissão anterior não clobbera o job vigente (identity guard)', async () => {
    let resolveFirst!: (value: unknown) => void
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockImplementationOnce(() => Promise.resolve(fakeFetchResponse({ status: 202, body: jobAcceptedBody('job-second') })))
    vi.stubGlobal('fetch', fetchMock)

    // Primeira submissão fica pendente; a segunda substitui o AbortController
    // antes da primeira resolver (mesmo padrão de searchStore.routing.test.ts).
    const first = useSearchStore.getState().startSearch({ query: 'primeira' })
    const second = useSearchStore.getState().startSearch({ query: 'segunda' })

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())

    resolveFirst(fakeFetchResponse({ status: 202, body: jobAcceptedBody('job-first') }))

    await expect(first).resolves.toEqual({ status: 'aborted' })
    await expect(second).resolves.toEqual({ status: 'success', jobId: 'job-second' })
    // O job vigente é sempre o da segunda submissão — a resposta tardia da
    // primeira nunca chega a mutar o estado do job já substituído.
    expect(useSearchStore.getState().search.results).toBeNull()
  })

  it('erro de submissão ainda limpa loading e notifica localmente como erro (comportamento preservado)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(fakeFetchResponse({ status: 500, body: { detail: 'falha controlada' } })))

    await expect(useSearchStore.getState().startCrossSearch({ target_sku: 'SKU-1' })).resolves.toEqual({ status: 'error' })

    expect(useSearchStore.getState().cross.loading).toBe(false)
    expect(useNotificationStore.getState().notifications).toHaveLength(1)
    expect(useNotificationStore.getState().notifications[0]).toMatchObject({ kind: 'error' })
  })

  it.each([
    ['search', 'job-nav-1'],
    ['cross', 'job-nav-2'],
  ] as const)('%s: o jobId aceito compõe a mesma URL profunda canônica usada pela navegação (deepSearchPath)', (kind, jobId) => {
    // Prova de contrato leve (sem DOM/JSX — este arquivo é .ts): o jobId devolvido
    // pela action é exatamente o segmento que App.tsx usa em transition.navigate.
    // A prova end-to-end com router real vive em routing/routes.test.tsx (Task 2).
    const expectedBase = kind === 'search' ? '/comparativa' : '/sku'
    expect(deepSearchPath(kind, jobId)).toBe(`${expectedBase}/${jobId}`)
  })
})
