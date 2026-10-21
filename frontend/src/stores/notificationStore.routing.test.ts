// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'e-scrapper.notifications'

const persistedNotification = (routeTarget?: unknown) => ({
  id: 'persisted-1',
  kind: 'info',
  title: 'Busca concluida',
  message: 'Resultado disponivel',
  createdAt: '2026-07-22T12:00:00.000Z',
  read: false,
  ...(routeTarget === undefined ? {} : { routeTarget }),
})

const loadPersistedNotifications = async (records: unknown[]) => {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(records))
  vi.resetModules()
  const storeModule = await import('./notificationStore')
  return {
    notifications: storeModule.useNotificationStore.getState().notifications,
    notificationRoutePath: storeModule.notificationRoutePath,
    useNotificationStore: storeModule.useNotificationStore,
  }
}

describe('notificationStore persisted route targets', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('keeps legacy informational notifications without a route target', async () => {
    const { notifications } = await loadPersistedNotifications([
      persistedNotification(),
    ])

    expect(notifications).toHaveLength(1)
    expect(notifications[0].routeTarget).toBeUndefined()
  })

  it.each([
    { kind: 'search', jobId: 'job/with space' },
    { kind: 'cross', jobId: 'sku-123' },
    { kind: 'monitor', monitorId: 'monitor/with space' },
    { kind: 'category-monitor', monitorId: 'category/1', productUrl: 'https://example.test/product one' },
  ] as const)('keeps a valid $kind target and builds its canonical encoded URL', async (routeTarget) => {
    const { notifications, notificationRoutePath } = await loadPersistedNotifications([
      persistedNotification(routeTarget),
    ])

    expect(notifications[0].routeTarget).toEqual(routeTarget)
    expect(notificationRoutePath(notifications[0].routeTarget!)).toBe(
      routeTarget.kind === 'search'
        ? `/comparativa/${encodeURIComponent(routeTarget.jobId)}`
        : routeTarget.kind === 'cross'
          ? `/sku/${encodeURIComponent(routeTarget.jobId)}`
          : routeTarget.kind === 'monitor'
            ? `/monitor?monitorId=${encodeURIComponent(routeTarget.monitorId)}`
            : `/monitor?categoryMonitorId=${encodeURIComponent(routeTarget.monitorId)}&categoryProductUrl=${encodeURIComponent(routeTarget.productUrl)}`,
    )
  })

  it.each([
    [{ kind: 'search' }],
    [{ jobId: 'job-1' }],
    [{ kind: 'unknown', jobId: 'job-1' }],
    [{ kind: 'cross', jobId: '' }],
    [{ kind: 'cross', jobId: '   ' }],
    [{ kind: 'monitor' }],
    [{ kind: 'monitor', monitorId: '' }],
    [{ kind: 'monitor', monitorId: 'monitor-1', jobId: 'job-1' }],
    [{ kind: 'category-monitor', monitorId: 'category-1' }],
    [{ kind: 'category-monitor', productUrl: 'https://example.test/product' }],
    [{ kind: 'category-monitor', monitorId: 'category-1', productUrl: '' }],
    [{ kind: 'category-monitor', monitorId: 'category-1', productUrl: 'https://example.test/product', jobId: 'job-1' }],
    [{ kind: 'search', jobId: 'job-1', monitorId: 'monitor-1' }],
    ['not-an-object'],
    [null],
  ])('removes an invalid persisted route target before it can be used (%j)', async (routeTarget) => {
    const { notifications } = await loadPersistedNotifications([
      persistedNotification(routeTarget),
    ])

    expect(notifications).toHaveLength(1)
    expect(notifications[0].routeTarget).toBeUndefined()
  })
  it.each([
    ['search', '/comparativa/backend-job'],
    ['cross', '/sku/backend-job'],
    ['monitor', '/monitor?monitorId=backend-monitor'],
    ['category-monitor', '/monitor?categoryMonitorId=backend-category&categoryProductUrl=https%3A%2F%2Fexample.test%2Fproduct'],
  ] as const)('merges one navigable %s completion and remains idempotent by backend id', async (kind, expectedPath) => {
    const { useNotificationStore, notificationRoutePath } = await loadPersistedNotifications([])
    const backendNotification = {
      id: `backend-${kind}`,
      title: 'Busca concluida',
      message: 'Resultado disponivel',
      tab: kind,
      route_target: kind === 'monitor'
        ? { kind, monitorId: 'backend-monitor' }
        : kind === 'category-monitor'
          ? { kind, monitorId: 'backend-category', productUrl: 'https://example.test/product' }
          : { kind, jobId: 'backend-job' },
      timestamp: '2026-07-22T12:00:00.000Z',
      read: false,
    }

    useNotificationStore.getState().syncBackend([backendNotification])
    useNotificationStore.getState().syncBackend([backendNotification])

    const notifications = useNotificationStore.getState().notifications
    expect(notifications).toHaveLength(1)
    expect(notifications[0].routeTarget).toEqual(
      kind === 'monitor'
        ? { kind, monitorId: 'backend-monitor' }
        : kind === 'category-monitor'
          ? { kind, monitorId: 'backend-category', productUrl: 'https://example.test/product' }
          : { kind, jobId: 'backend-job' },
    )
    expect(notificationRoutePath(notifications[0].routeTarget!)).toBe(expectedPath)
  })

  it('hydrates a persisted legacy notification when the backend supplies its monitor target', async () => {
    const { useNotificationStore } = await loadPersistedNotifications([
      persistedNotification(),
    ])

    useNotificationStore.getState().syncBackend([{
      id: 'persisted-1',
      title: 'Preço alterado',
      message: 'Abra o monitor',
      tab: 'monitor',
      route_target: { kind: 'monitor', monitorId: 'monitor-1' },
      timestamp: '2026-07-22T12:00:00.000Z',
      read: false,
    }])

    expect(useNotificationStore.getState().notifications[0].routeTarget).toEqual({
      kind: 'monitor',
      monitorId: 'monitor-1',
    })
  })
})
