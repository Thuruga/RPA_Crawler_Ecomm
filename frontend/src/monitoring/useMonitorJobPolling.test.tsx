// @vitest-environment jsdom

import { act, useEffect } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiClient } from '../api/client'
import {
  useMonitorJobPolling,
  type MonitorJobPollingOptions,
  type MonitorJobPollingState,
} from './useMonitorJobPolling'

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

type Harness = {
  state: () => MonitorJobPollingState
  rerender: (options: MonitorJobPollingOptions) => Promise<void>
  unmount: () => Promise<void>
}

function Probe({ options, onState }: { options: MonitorJobPollingOptions; onState: (s: MonitorJobPollingState) => void }) {
  const state = useMonitorJobPolling(options)
  useEffect(() => {
    onState(state)
  })
  return null
}

const mounted: Array<{ root: Root; container: HTMLDivElement }> = []

async function mountHook(options: MonitorJobPollingOptions): Promise<Harness> {
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)
  mounted.push({ root, container })

  let latest: MonitorJobPollingState
  const onState = (state: MonitorJobPollingState) => {
    latest = state
  }

  await act(async () => {
    root.render(<Probe options={options} onState={onState} />)
  })

  return {
    state: () => latest,
    rerender: async (nextOptions: MonitorJobPollingOptions) => {
      await act(async () => {
        root.render(<Probe options={nextOptions} onState={onState} />)
      })
    },
    unmount: async () => {
      await act(async () => root.unmount())
      container.remove()
    },
  }
}

const flushMicrotasks = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

afterEach(async () => {
  for (const { root, container } of mounted.splice(0)) {
    await act(async () => root.unmount())
    container.remove()
  }
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useMonitorJobPolling', () => {
  it('queries every visible id on mount even without a prior in-memory snapshot, waits for full settle before scheduling the next cycle, and never overlaps', async () => {
    vi.useFakeTimers()
    const historyCalls: string[] = []
    const scanCalls: string[] = []
    vi.spyOn(ApiClient, 'getMonitorHistory').mockImplementation(async (jobId: string) => {
      historyCalls.push(jobId)
      return { job_id: jobId, check_runs: [] } as never
    })
    vi.spyOn(ApiClient, 'getCategoryScans').mockImplementation(async (monitorId: string) => {
      scanCalls.push(monitorId)
      return [] as never
    })

    const harness = await mountHook({ priceMonitorIds: ['p1'], categoryMonitorIds: ['c1'], enabled: true })
    await flushMicrotasks()

    expect(historyCalls).toEqual(['p1'])
    expect(scanCalls).toEqual(['c1'])
    expect(harness.state().loading).toBe(false)

    // Advancing less than the poll interval must not trigger a second cycle.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(historyCalls).toEqual(['p1'])

    // Crossing the 1500ms interval fires exactly one more cycle (no overlap).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600)
    })
    expect(historyCalls).toEqual(['p1', 'p1'])
    expect(scanCalls).toEqual(['c1', 'c1'])

    await harness.unmount()
  })

  it('aborts in-flight fetches, clears the timer and cancels the generation when ids change or on unmount', async () => {
    vi.useFakeTimers()
    // Records every signal handed to getMonitorHistory across calls (one per
    // cycle) — the assertion below targets specifically the FIRST cycle's
    // (p1) signal, not whichever call happens to run last.
    const signalsByCall: AbortSignal[] = []
    vi.spyOn(ApiClient, 'getMonitorHistory').mockImplementation(
      (_jobId: string, signal?: AbortSignal) =>
        new Promise((_resolve, reject) => {
          if (signal) signalsByCall.push(signal)
          signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        }) as never,
    )
    vi.spyOn(ApiClient, 'getCategoryScans').mockResolvedValue([] as never)

    const harness = await mountHook({ priceMonitorIds: ['p1'], categoryMonitorIds: [], enabled: true })
    await flushMicrotasks()

    expect(signalsByCall).toHaveLength(1)
    const firstCycleSignal = signalsByCall[0]
    expect(firstCycleSignal.aborted).toBe(false)

    await harness.rerender({ priceMonitorIds: ['p2'], categoryMonitorIds: [], enabled: true })
    await flushMicrotasks()

    expect(firstCycleSignal.aborted).toBe(true)

    await harness.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('ignores a stale response from a superseded generation so it never clobbers the current snapshot', async () => {
    vi.useFakeTimers()
    let resolveStale: ((value: unknown) => void) | null = null
    let call = 0
    vi.spyOn(ApiClient, 'getMonitorHistory').mockImplementation(async (jobId: string) => {
      call += 1
      if (call === 1) {
        return new Promise(resolve => {
          resolveStale = resolve
        }) as never
      }
      return { job_id: jobId, check_runs: [{ run_id: `${jobId}-run`, logical_cycle_id: 'x', scheduled_for: 'now', status: 'ok', created_at: 'now', started_at: null, completed_at: 'now', error: null }] } as never
    })
    vi.spyOn(ApiClient, 'getCategoryScans').mockResolvedValue([] as never)

    const harness = await mountHook({ priceMonitorIds: ['p1'], categoryMonitorIds: [], enabled: true })
    await flushMicrotasks()

    // Change ids before the first (stale) fetch resolves.
    await harness.rerender({ priceMonitorIds: ['p2'], categoryMonitorIds: [], enabled: true })
    await flushMicrotasks()

    expect(harness.state().priceRunsByMonitorId.p2).toBeDefined()
    expect(harness.state().priceRunsByMonitorId.p1).toBeUndefined()

    // Now let the stale p1 promise resolve — it must be ignored.
    await act(async () => {
      resolveStale?.({ job_id: 'p1', check_runs: [{ run_id: 'stale-run' }] })
      await Promise.resolve()
      await Promise.resolve()
    })

    expect(harness.state().priceRunsByMonitorId.p1).toBeUndefined()
    expect(harness.state().priceRunsByMonitorId.p2).toBeDefined()

    await harness.unmount()
  })

  it('polls both price and category ids independently and reflects category scans by monitor id', async () => {
    vi.useFakeTimers()
    vi.spyOn(ApiClient, 'getMonitorHistory').mockResolvedValue({ job_id: 'p1', check_runs: [] } as never)
    vi.spyOn(ApiClient, 'getCategoryScans').mockResolvedValue([
      { scan_id: 's1', monitor_id: 'c1', status: 'running', created_at: 'now' },
    ] as never)

    const harness = await mountHook({ priceMonitorIds: ['p1'], categoryMonitorIds: ['c1'], enabled: true })
    await flushMicrotasks()

    expect(harness.state().categoryScansByMonitorId.c1).toHaveLength(1)
    expect(harness.state().categoryScansByMonitorId.c1[0].scan_id).toBe('s1')

    await harness.unmount()
  })

  it('does not poll when disabled', async () => {
    vi.useFakeTimers()
    const historySpy = vi.spyOn(ApiClient, 'getMonitorHistory').mockResolvedValue({ job_id: 'p1', check_runs: [] } as never)

    const harness = await mountHook({ priceMonitorIds: ['p1'], categoryMonitorIds: [], enabled: false })
    await flushMicrotasks()

    expect(historySpy).not.toHaveBeenCalled()

    await harness.unmount()
  })
})
