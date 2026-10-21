// @vitest-environment jsdom

import { act, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Link } from 'react-router'

import { CrossMarketplacePage, SearchPage, SidebarItem } from '../App'
import { ApiClient, ApiHttpError } from '../api/client'
import { useSearchStore } from '../stores/searchStore'
import { isNotificationRouteTarget, notificationRoutePath } from '../stores/notificationStore'
import { AppRoutes } from './AppRoutes'
import { NotFoundRoute } from './NotFoundRoute'
import { BASE_ROUTES, deepSearchPath, resolveRouteMetadata, type SearchKind } from './paths'
import { RouteTransitionCoordinator, useRouteTransitionCoordinator } from './RouteTransitionCoordinator'
import { navigateBrowser, renderRoute } from './testUtils'
import { useDeepLinkedHistory } from './useDeepLinkedHistory'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

export const BASE_ROUTE_CASES = [
  {
    path: '/monitor',
    title: 'Painel de Monitoramento',
    activeItem: 'Monitores',
    description: 'Acompanhe preços, disponibilidade e variações em uma leitura compacta.',
  },
  {
    path: '/comparativa',
    title: 'Busca Comparativa',
    activeItem: 'Comparativa',
    description: 'Compare preços, disponibilidade e frete entre marcas sem perder densidade de informação.',
  },
  {
    path: '/sku',
    title: 'Busca por SKU',
    activeItem: 'SKU',
    description: 'Compare ofertas, buy box e frete entre e-commerces e marketplaces concorrentes.',
  },
  {
    path: '/categorias',
    title: 'Varredura por Categoria',
    activeItem: 'Categorias',
    description: 'Selecione uma categoria por marca e acompanhe a extração em tempo real.',
  },
  {
    path: '/banners',
    title: 'Banners',
    activeItem: 'Banners',
    description: 'Extraia, revise e aprove os banners das marcas ativas em um fluxo protegido.',
  },
  {
    path: '/marcas',
    title: 'Gerenciar Marcas',
    activeItem: 'Marcas',
    description: 'Cadastre e gerencie as marcas disponíveis nas operações de monitoramento.',
  },
] as const

const flush = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

function DeepProbe({ kind, jobId }: { kind: SearchKind; jobId: string }) {
  const state = useDeepLinkedHistory(kind, jobId)
  return (
    <section>
      <output data-status={state.status}>{state.status}</output>
      <button type="button" onClick={state.retry}>retry</button>
    </section>
  )
}

async function renderDeep(kind: SearchKind, jobId: string) {
  return renderRoute(
    <RouteTransitionCoordinator><DeepProbe kind={kind} jobId={jobId} /></RouteTransitionCoordinator>,
    deepSearchPath(kind, jobId),
  )
}

describe('routing test harness', () => {
  it('mounts with the real BrowserRouter and follows native history events', async () => {
    const rendered = await renderRoute(<output data-path={window.location.pathname}>ready</output>, '/monitor')
    expect(rendered.container.querySelector('output')?.textContent).toBe('ready')
    await navigateBrowser('/comparativa')
    expect(window.location.pathname).toBe('/comparativa')
  })
})

describe('NAV-01 direct routes', () => {
  it('resolves all six base routes and canonical deep paths', () => {
    for (const expected of BASE_ROUTE_CASES) {
      expect(resolveRouteMetadata(expected.path)).toMatchObject({
        title: expected.title,
        sidebarLabel: expected.activeItem,
        description: expected.description,
      })
    }
    expect(BASE_ROUTES).toHaveLength(6)
    expect(deepSearchPath('search', 'job/id 1')).toBe('/comparativa/job%2Fid%201')
    expect(deepSearchPath('cross', 'job/id 1')).toBe('/sku/job%2Fid%201')
    expect(resolveRouteMetadata('/history/abc').id).toBe('not-found')
    const target = { kind: 'search' as const, jobId: 'job/id 1' }
    expect(isNotificationRouteTarget(target)).toBe(true)
    expect(notificationRoutePath(target)).toBe(deepSearchPath('search', 'job/id 1'))
    expect(isNotificationRouteTarget({ kind: 'search' })).toBe(false)
    expect(isNotificationRouteTarget({ kind: 'other', jobId: 'job/id 1' })).toBe(false)
  })
})

describe('NAV-02 browser history', () => {
  it('redirects / to /monitor with replace', async () => {
    const previousLength = window.history.length
    await renderRoute(
      <AppRoutes
        elements={{
          monitor: <output>monitor</output>, search: <output>search</output>,
          cross: <output>cross</output>, category: <output>category</output>,
          banners: <output>banners</output>, settings: <output>settings</output>,
        }}
        notFoundElement={<output>not-found</output>}
      />,
      '/',
    )
    expect(window.location.pathname).toBe('/monitor')
    expect(window.history.length).toBe(previousLength)
  })

  it('captures the abandoned entry and resets a sidebar PUSH to a base route', async () => {
    function CoordinatorProbe() {
      const coordinator = useRouteTransitionCoordinator()
      return <button onClick={() => coordinator.navigate('/comparativa', { targetKind: 'search', resetBase: true })}>base</button>
    }

    useSearchStore.getState().setSearch({ query: 'deep', results: { results: [{ id: 'deep' }] } })
    const rendered = await renderRoute(
      <RouteTransitionCoordinator><CoordinatorProbe /></RouteTransitionCoordinator>,
      '/comparativa/job',
    )
    await act(async () => rendered.container.querySelector('button')?.click())
    expect(window.location.pathname).toBe('/comparativa')
    expect(useSearchStore.getState().search.query).toBe('')
    expect(useSearchStore.getState().search.results).toBeNull()
  })

  it('removes a comparative deep jobId before the pending search action resolves, then navigates to the accepted job (D-08)', async () => {
    let resolveSearch!: (value: { status: 'success'; jobId: string }) => void
    const startSearch = vi.fn(() => new Promise<{ status: 'success'; jobId: string }>(resolve => {
      resolveSearch = resolve
    }))
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({ type: 'search', status: 'PENDING' })
    useSearchStore.setState(state => ({
      ...state,
      search: { ...state.search, query: 'camisa', selectedBrands: ['aramis'] },
      startSearch,
    }))

    const rendered = await renderRoute(
      <RouteTransitionCoordinator>
        <SearchPage brands={[{ brand_key: 'aramis', brand_name: 'Aramis' }]} jobId="job-search" />
      </RouteTransitionCoordinator>,
      '/comparativa/job-search',
    )

    act(() => rendered.container.querySelector('form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    expect(startSearch).toHaveBeenCalledOnce()
    // Old deep jobId is dropped immediately (05-07 order) — worker still pending.
    expect(window.location.pathname).toBe('/comparativa')

    // D-08: 202 acceptance navigates to the NEW durable job URL — not to a fake
    // completion state, and before the (still-fake) worker ever resolves.
    resolveSearch({ status: 'success', jobId: 'job-search-2' })
    await flush()
    expect(window.location.pathname).toBe(deepSearchPath('search', 'job-search-2'))
  })

  it('removes a SKU deep jobId before the pending cross action resolves, then navigates to the accepted job (D-08)', async () => {
    let resolveCross!: (value: { status: 'success'; jobId: string }) => void
    const startCrossSearch = vi.fn(() => new Promise<{ status: 'success'; jobId: string }>(resolve => {
      resolveCross = resolve
    }))
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({ type: 'cross', status: 'PENDING' })
    useSearchStore.setState(state => ({
      ...state,
      cross: { ...state.cross, targetSku: 'ML.05.0326046' },
      startCrossSearch,
    }))

    const rendered = await renderRoute(
      <RouteTransitionCoordinator>
        <CrossMarketplacePage jobId="job-cross" />
      </RouteTransitionCoordinator>,
      '/sku/job-cross',
    )

    act(() => rendered.container.querySelector('form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
    expect(startCrossSearch).toHaveBeenCalledOnce()
    expect(window.location.pathname).toBe('/sku')

    resolveCross({ status: 'success', jobId: 'job-cross-2' })
    await flush()
    expect(window.location.pathname).toBe(deepSearchPath('cross', 'job-cross-2'))
  })

  it('restores distinct source and target snapshots through real Back and Forward POPs', async () => {
    function CoordinatorProbe() {
      const coordinator = useRouteTransitionCoordinator()
      return <button data-push onClick={() => coordinator.navigate('/comparativa/job-b', { targetKind: 'search' })}>deep</button>
    }

    useSearchStore.getState().setSearch({ query: 'source-a', results: { marker: 'A' } })
    const rendered = await renderRoute(
      <RouteTransitionCoordinator><CoordinatorProbe /></RouteTransitionCoordinator>,
      '/comparativa',
    )
    const sourceKey = window.history.state?.key ?? 'default'
    await act(async () => rendered.container.querySelector<HTMLButtonElement>('[data-push]')?.click())
    const targetKey = window.history.state?.key
    expect(targetKey).toBeTruthy()
    expect(targetKey).not.toBe(sourceKey)

    useSearchStore.getState().setSearch({ query: 'target-b', results: { marker: 'B' } })
    useSearchStore.getState().captureNavigableSnapshot(targetKey, 'search')
    useSearchStore.getState().setSearch({ query: 'drift', results: { marker: 'drift' } })

    await act(async () => {
      const popped = new Promise<void>(resolve => window.addEventListener('popstate', () => resolve(), { once: true }))
      window.history.back()
      await popped
    })
    expect(window.location.pathname).toBe('/comparativa')
    expect(useSearchStore.getState().search.query).toBe('source-a')
    expect(useSearchStore.getState().search.results).toEqual({ marker: 'A' })

    await act(async () => {
      const popped = new Promise<void>(resolve => window.addEventListener('popstate', () => resolve(), { once: true }))
      window.history.forward()
      await popped
    })
    expect(window.location.pathname).toBe('/comparativa/job-b')
    expect(useSearchStore.getState().search.query).toBe('target-b')
    expect(useSearchStore.getState().search.results).toEqual({ marker: 'B' })
  })

  it('ignores modified sidebar clicks and leaves no intent for the next local PUSH', async () => {
    const prepared = vi.fn()
    const TestIcon = () => null

    function ModifiedClickProbe() {
      const coordinator = useRouteTransitionCoordinator()
      return <>
        <SidebarItem
          icon={TestIcon}
          label="Comparativa"
          to="/comparativa"
          onNavigate={(event) => {
            prepared(event)
            coordinator.prepareNavigation('search', true)
          }}
        />
        <Link data-local to="/comparativa">local</Link>
      </>
    }

    useSearchStore.getState().setSearch({ query: 'preserve-me', results: { marker: 'deep' } })
    const rendered = await renderRoute(
      <RouteTransitionCoordinator><ModifiedClickProbe /></RouteTransitionCoordinator>,
      '/comparativa/job-modified',
    )
    const sidebar = rendered.container.querySelector<HTMLAnchorElement>('a.sidebar-item')!

    for (const init of [
      { ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 },
    ]) {
      act(() => sidebar.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...init })))
    }
    expect(prepared).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe('/comparativa/job-modified')

    await act(async () => rendered.container.querySelector<HTMLAnchorElement>('[data-local]')?.click())
    expect(window.location.pathname).toBe('/comparativa')
    expect(useSearchStore.getState().search.query).toBe('preserve-me')
  })
})

describe('NAV-03 deep state machine', () => {
  it('restores a completed comparative result into the correct slice', async () => {
    let resolveCompleted!: (value: unknown) => void
    vi.spyOn(ApiClient, 'getHistoryDetail').mockImplementation(() => new Promise(resolve => {
      resolveCompleted = resolve
    }) as never)
    const rendered = await renderDeep('search', 'completed')
    expect(rendered.container.querySelector('output')?.dataset.status).toBe('loading')
    resolveCompleted({
      type: 'search', status: 'COMPLETED', query: 'camisa', brands: ['aramis'], results: [{ products: [] }],
    })
    await flush()
    expect(rendered.container.querySelector('output')?.dataset.status).toBe('ready')
    expect(useSearchStore.getState().search.query).toBe('camisa')
    expect(useSearchStore.getState().cross.results).toBeNull()
  })

  it('keeps a missing history URL and exposes not-found', async () => {
    vi.spyOn(ApiClient, 'getHistoryDetail').mockRejectedValue(new ApiHttpError(404, 'missing'))
    const rendered = await renderDeep('search', 'missing')
    await flush()
    expect(rendered.container.querySelector('output')?.dataset.status).toBe('not-found')
    expect(window.location.pathname).toBe('/comparativa/missing')
  })

  it('retries a temporary failure at the same URL', async () => {
    vi.spyOn(ApiClient, 'getHistoryDetail')
      .mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce({ type: 'search', status: 'COMPLETED', query: 'retry', results: [] })
    const rendered = await renderDeep('search', 'retry-id')
    await flush()
    expect(rendered.container.querySelector('output')?.dataset.status).toBe('temporary-error')
    await act(async () => rendered.container.querySelector('button')?.click())
    expect(window.location.pathname).toBe('/comparativa/retry-id')
    await flush()
    expect(rendered.container.querySelector('output')?.dataset.status).toBe('ready')
  })

  it('canonicalizes a wrong type with replace before writing results', async () => {
    const previousLength = window.history.length
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({ type: 'cross', status: 'COMPLETED', results: [{ id: 'wrong' }] })
    const rendered = await renderDeep('search', 'wrong')
    await flush()
    expect(rendered.container.querySelector('output')?.dataset.status).toBe('wrong-type')
    expect(window.location.pathname).toBe('/sku/wrong')
    expect(window.history.length).toBe(previousLength)
    expect(useSearchStore.getState().search.results).toBeNull()
  })

  it('ignores a stale response after the deep identity changes', async () => {
    let resolveOld!: (value: unknown) => void
    vi.spyOn(ApiClient, 'getHistoryDetail').mockImplementation((jobId: string) => {
      if (jobId === 'old') return new Promise(resolve => { resolveOld = resolve }) as never
      return Promise.resolve({ type: 'search', status: 'COMPLETED', query: 'new', results: [] }) as never
    })

    function ChangingProbe() {
      const [jobId, setJobId] = useState('old')
      return <><DeepProbe kind="search" jobId={jobId} /><button data-change onClick={() => setJobId('new')}>change</button></>
    }

    const rendered = await renderRoute(
      <RouteTransitionCoordinator><ChangingProbe /></RouteTransitionCoordinator>,
      '/comparativa/old',
    )
    await act(async () => rendered.container.querySelector<HTMLButtonElement>('[data-change]')?.click())
    await flush()
    resolveOld({ type: 'search', status: 'COMPLETED', query: 'old', results: [{ id: 'old' }] })
    await flush()
    expect(useSearchStore.getState().search.query).toBe('new')
  })

  describe('W0-05 sequential polling (pending/running/interrupted)', () => {
    beforeEach(() => {
      // getHistoryDetail is re-spied per test below; without a full restore
      // here vi.spyOn reuses the previous test's mock instance (same call
      // history), inflating these tests' exact-call-count assertions.
      vi.restoreAllMocks()
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    it.each(['PENDING', 'RUNNING', 'INTERRUPTED'] as const)(
      'schedules exactly one poll after %s settles, honoring the server poll_after_ms',
      async recordStatus => {
        const getHistoryDetail = vi.spyOn(ApiClient, 'getHistoryDetail')
          .mockResolvedValueOnce({ type: 'search', status: recordStatus, poll_after_ms: 2000 })
        const rendered = await renderDeep('search', `poll-${recordStatus.toLowerCase()}`)
        await flush()

        expect(rendered.container.querySelector('output')?.dataset.status).toBe(recordStatus.toLowerCase())
        expect(useSearchStore.getState().search.results).toBeNull()
        // Exactly one poll is scheduled after settle — never zero (D-05/D-06
        // keep consulting), never more than one in flight (T-07-29).
        expect(vi.getTimerCount()).toBe(1)
        expect(getHistoryDetail).toHaveBeenCalledTimes(1)

        getHistoryDetail.mockResolvedValueOnce({ type: 'search', status: 'COMPLETED', query: 'q', results: [] })
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2000)
        })

        expect(getHistoryDetail).toHaveBeenCalledTimes(2)
        expect(vi.getTimerCount()).toBe(0)
        expect(rendered.container.querySelector('output')?.dataset.status).toBe('ready')
      },
    )

    it.each(['COMPLETED', 'FAILED'] as const)('never schedules a timer once %s is reached', async recordStatus => {
      const getHistoryDetail = vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValueOnce(
        recordStatus === 'COMPLETED'
          ? { type: 'search', status: 'COMPLETED', query: 'terminal', results: [] }
          : { type: 'search', status: 'FAILED', error: 'raspagem falhou' },
      )
      const rendered = await renderDeep('search', `terminal-${recordStatus.toLowerCase()}`)
      await flush()

      // COMPLETED hydrates the store and reports 'ready' (pre-existing NAV-03
      // contract); FAILED is a new distinct honest terminal.
      const expectedStatus = recordStatus === 'COMPLETED' ? 'ready' : 'failed'
      expect(rendered.container.querySelector('output')?.dataset.status).toBe(expectedStatus)
      expect(vi.getTimerCount()).toBe(0)
      expect(getHistoryDetail).toHaveBeenCalledTimes(1)
    })

    it('never overlaps fetches: the next request only fires after the previous one settles', async () => {
      let resolveSecond!: (value: unknown) => void
      const getHistoryDetail = vi.spyOn(ApiClient, 'getHistoryDetail')
        .mockResolvedValueOnce({ type: 'search', status: 'RUNNING', poll_after_ms: 1000 })
        .mockImplementationOnce(() => new Promise(resolve => { resolveSecond = resolve }) as never)

      await renderDeep('search', 'no-overlap')
      await flush()
      expect(vi.getTimerCount()).toBe(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })
      // The second fetch is now in flight; no timer exists until it settles.
      expect(getHistoryDetail).toHaveBeenCalledTimes(2)
      expect(vi.getTimerCount()).toBe(0)

      await act(async () => {
        resolveSecond({ type: 'search', status: 'COMPLETED', query: 'q', results: [] })
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(vi.getTimerCount()).toBe(0)
    })

    it('aborts the in-flight fetch and clears the pending timer on unmount, ignoring a late response', async () => {
      let resolveFetch!: (value: unknown) => void
      vi.spyOn(ApiClient, 'getHistoryDetail').mockImplementation(() => new Promise(resolve => {
        resolveFetch = resolve
      }) as never)
      const rendered = await renderDeep('search', 'unmount-job')
      await flush()

      await rendered.unmount()
      expect(vi.getTimerCount()).toBe(0)

      resolveFetch({ type: 'search', status: 'COMPLETED', query: 'late', results: [{ id: 'late' }] })
      await flush()
      expect(useSearchStore.getState().search.results).toBeNull()
    })

    it('resumes polling on a fresh mount at the same deep URL without depending on prior store state (reload)', async () => {
      const getHistoryDetail = vi.spyOn(ApiClient, 'getHistoryDetail')
        .mockResolvedValueOnce({ type: 'search', status: 'RUNNING', poll_after_ms: 1500 })
      useSearchStore.getState().resetSearchSlice()

      const rendered = await renderDeep('search', 'reload-job')
      await flush()

      expect(rendered.container.querySelector('output')?.dataset.status).toBe('running')
      expect(getHistoryDetail).toHaveBeenCalledWith('reload-job', expect.anything())
      expect(vi.getTimerCount()).toBe(1)
    })
  })
})

describe('W0-05 progress and terminal rendering (real pages)', () => {
  it('renders persisted stage/units for RUNNING without fabricating a percent', async () => {
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({
      type: 'search',
      status: 'RUNNING',
      progress: { stage: 'Buscando na Aramis', completed_units: 3, total_units: null, percent: null },
    })

    const rendered = await renderRoute(
      <RouteTransitionCoordinator><SearchPage brands={[{ brand_key: 'aramis', brand_name: 'Aramis' }]} jobId="job-progress" /></RouteTransitionCoordinator>,
      '/comparativa/job-progress',
    )
    await flush()

    expect(rendered.container.textContent).toContain('Buscando na Aramis')
    expect(rendered.container.textContent).toContain('3')
    // Nenhum percentual é inventado quando o backend não o envia (T-07-32).
    expect(rendered.container.textContent).not.toMatch(/0\s?%/)
    expect(rendered.container.textContent).not.toMatch(/\d+\s?%/)
  })

  it('renders provided completed_units/total_units/percent verbatim once the backend sends them', async () => {
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({
      type: 'search',
      status: 'RUNNING',
      progress: { stage: 'Buscando na Aramis', completed_units: 2, total_units: 4, percent: 50 },
    })

    const rendered = await renderRoute(
      <RouteTransitionCoordinator><SearchPage brands={[{ brand_key: 'aramis', brand_name: 'Aramis' }]} jobId="job-progress-2" /></RouteTransitionCoordinator>,
      '/comparativa/job-progress-2',
    )
    await flush()

    expect(rendered.container.textContent).toContain('2')
    expect(rendered.container.textContent).toContain('4')
    expect(rendered.container.textContent).toContain('50%')
  })

  it('renders INTERRUPTED as a distinct auto-resuming state, not a failure', async () => {
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({ type: 'cross', status: 'INTERRUPTED' })

    const rendered = await renderRoute(
      <RouteTransitionCoordinator><CrossMarketplacePage jobId="job-interrupted" /></RouteTransitionCoordinator>,
      '/sku/job-interrupted',
    )
    await flush()

    expect(rendered.container.textContent).toMatch(/interromp/i)
    expect(rendered.container.textContent).toMatch(/retoma/i)
    expect(rendered.container.textContent).not.toMatch(/não foi possível/i)
  })

  it('renders FAILED with the sanitized snapshot error and a manual re-read retry (not a new search)', async () => {
    const getHistoryDetail = vi.spyOn(ApiClient, 'getHistoryDetail')
      .mockResolvedValue({ type: 'search', status: 'FAILED', error: 'Falha ao acessar o site da marca.' })
    const startSearch = vi.fn()
    useSearchStore.setState(state => ({ ...state, startSearch }))

    const rendered = await renderRoute(
      <RouteTransitionCoordinator><SearchPage brands={[{ brand_key: 'aramis', brand_name: 'Aramis' }]} jobId="job-failed" /></RouteTransitionCoordinator>,
      '/comparativa/job-failed',
    )
    await flush()

    expect(rendered.container.textContent).toContain('Falha ao acessar o site da marca.')
    const retryButton = [...rendered.container.querySelectorAll('button')].find(b => /tentar novamente/i.test(b.textContent ?? ''))
    expect(retryButton).toBeTruthy()

    const callsBefore = getHistoryDetail.mock.calls.length
    await act(async () => retryButton?.click())
    await flush()

    // O retry lê o snapshot de novo (mesma GET) — nunca dispara uma nova execução.
    expect(getHistoryDetail.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(startSearch).not.toHaveBeenCalled()
  })

  it('COMPLETED renders results and stops the deep-history indicator/loading', async () => {
    vi.spyOn(ApiClient, 'getHistoryDetail').mockResolvedValue({
      type: 'search', status: 'COMPLETED', query: 'camisa', brands: ['aramis'], results: [],
    })

    const rendered = await renderRoute(
      <RouteTransitionCoordinator><SearchPage brands={[{ brand_key: 'aramis', brand_name: 'Aramis' }]} jobId="job-completed" /></RouteTransitionCoordinator>,
      '/comparativa/job-completed',
    )
    await flush()

    expect(useSearchStore.getState().search.loading).toBe(false)
    expect(rendered.container.textContent).not.toMatch(/em andamento/i)
    expect(rendered.container.textContent).not.toMatch(/interromp/i)
  })
})

describe('unknown route fallback', () => {
  it('renders an unknown URL without reflecting it in body copy', async () => {
    await renderRoute(
      <AppRoutes
        elements={{
          monitor: <output>monitor</output>, search: <output>search</output>,
          cross: <output>cross</output>, category: <output>category</output>,
          banners: <output>banners</output>, settings: <output>settings</output>,
        }}
        notFoundElement={<NotFoundRoute />}
      />,
      '/unknown-route',
    )
    expect(document.querySelector('.not-found-route h2')?.textContent).toBe(resolveRouteMetadata('/unknown-route').title)
    expect(document.body.textContent).not.toContain('/unknown-route')
  })
})

describe('route accessibility and responsive contracts', () => {
  it('keeps long identifiers encoded and exposes native link semantics', async () => {
    const longJobId = `${'job'.repeat(80)}/com espaço`
    const href = deepSearchPath('search', longJobId)
    expect(href).toContain('%2F')
    expect(href).toContain('%20')

    const rendered = await renderRoute(<a href={href} aria-current="page">Busca concluída</a>, '/comparativa')
    const link = rendered.container.querySelector('a')
    expect(link?.getAttribute('href')).toBe(href)
    expect(link?.getAttribute('aria-current')).toBe('page')
  })
})
