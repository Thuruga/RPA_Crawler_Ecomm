// @vitest-environment jsdom

import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiClient } from '../../api/client'
import { formatTimeGlobal } from '../../shared/lib'
import { MonitorPage } from './MonitorPage'

vi.mock('../product/ProductDetailModal', () => ({
  ProductDetailModal: ({ open, seed, showMonitorAction, onClose }: { open: boolean; seed?: { product_name?: string }; showMonitorAction?: boolean; onClose: () => void }) => (
    open ? <div data-testid="product-detail" data-show-monitor-action={String(showMonitorAction)}><span>{seed?.product_name}</span><button onClick={onClose}>Fechar</button></div> : null
  ),
  MonitorConditionalOffers: () => null,
}))

const monitor = {
  job_id: 'monitor/id 1',
  brand: 'aramis',
  url: 'https://aramis.test/camisa',
  product_name: 'Camisa Monitorada',
  history: [],
  conditional_offers_current: [],
  conditional_offers_diagnostics: [],
  conditional_offer_history: [],
  check_runs: [],
}

const LocationProbe = () => <output data-testid="location">{useLocation().search}</output>

describe('MonitorPage notification deep link', () => {
  let container: HTMLDivElement
  let root: ReturnType<typeof createRoot>

  beforeEach(() => {
    vi.spyOn(ApiClient, 'getMonitors').mockResolvedValue([monitor])
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    container.remove()
    vi.restoreAllMocks()
  })

  it('opens the matching monitor once and consumes the target query', async () => {
    await act(async () => {
      root.render(<MemoryRouter initialEntries={['/monitor?monitorId=monitor%2Fid%201']}><MonitorPage brands={[]} /><LocationProbe /></MemoryRouter>)
    })
    await act(async () => { await Promise.resolve() })

    expect(container.querySelector('[data-testid="product-detail"]')?.textContent).toContain('Camisa Monitorada')
    expect(container.querySelector('[data-testid="product-detail"]')?.getAttribute('data-show-monitor-action')).toBe('false')
    expect(container.querySelector('[data-testid="location"]')?.textContent).toBe('')

    await act(async () => {
      (container.querySelector('[data-testid="product-detail"] button') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    expect(container.querySelector('[data-testid="product-detail"]')).toBeNull()
  })

  it('leaves an unknown monitor target inert', async () => {
    await act(async () => {
      root.render(<MemoryRouter initialEntries={['/monitor?monitorId=unknown']}><MonitorPage brands={[]} /><LocationProbe /></MemoryRouter>)
    })
    await act(async () => { await Promise.resolve() })

    expect(container.querySelector('[data-testid="product-detail"]')).toBeNull()
    expect(container.querySelector('[data-testid="location"]')?.textContent).toBe('?monitorId=unknown')
  })
})

describe('MonitorPage lifecycle readings', () => {
  let container: HTMLDivElement
  let root: ReturnType<typeof createRoot>

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    container.remove()
    vi.restoreAllMocks()
  })

  const renderMonitors = async (monitors: typeof monitor[]) => {
    vi.spyOn(ApiClient, 'getMonitors').mockResolvedValue(monitors)
    await act(async () => {
      root.render(<MemoryRouter><MonitorPage brands={[]} /></MemoryRouter>)
    })
    await act(async () => {
      await Promise.resolve()
    })
  }

  it('shows the successful reading and the durable scheduled attempt, not the generic last attempt', async () => {
    const lastSuccessAt = '2026-09-25T13:48:00Z'
    const nextCheckDueAt = '2026-09-25T14:03:00Z'
    await renderMonitors([{
      ...monitor,
      last_checked_at: '2026-09-25T13:50:00Z',
      last_success_at: lastSuccessAt,
      next_check_due_at: nextCheckDueAt,
      next_retry_at: '2026-09-25T14:30:00Z',
    }])

    const text = container.textContent ?? ''
    expect(text).toContain(`Último sucesso: ${formatTimeGlobal(lastSuccessAt)}`)
    expect(text).toContain(`Próxima tentativa: ${formatTimeGlobal(nextCheckDueAt)}`)
    expect(text).not.toContain('Última tentativa:')
    expect(text).not.toContain(formatTimeGlobal('2026-09-25T14:30:00Z'))
  })

  it('states explicitly when the monitor has no next scheduled attempt', async () => {
    await renderMonitors([{
      ...monitor,
      last_success_at: '2026-09-25T13:48:00Z',
      next_check_due_at: null,
    }])

    expect(container.textContent).toContain('Próxima tentativa: não agendada')
  })
})
