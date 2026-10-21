import { create } from 'zustand'
import { toast } from 'sonner'

import { deepSearchPath, ROUTES, type SearchKind } from '../routing/paths'

export type AppNotificationKind = 'price' | 'process' | 'success' | 'error' | 'info'

type SearchNotificationRouteTarget = { kind: SearchKind; jobId: string }
type MonitorNotificationRouteTarget = { kind: 'monitor'; monitorId: string }
type CategoryMonitorNotificationRouteTarget = {
  kind: 'category-monitor'
  monitorId: string
  productUrl: string
}

export type NotificationRouteTarget =
  | SearchNotificationRouteTarget
  | MonitorNotificationRouteTarget
  | CategoryMonitorNotificationRouteTarget

export const isNotificationRouteTarget = (value: unknown): value is NotificationRouteTarget => {
  if (typeof value !== 'object' || value === null) return false
  const target = value as Record<string, unknown>
  const keys = Object.keys(target)
  if (target.kind === 'monitor') {
    return keys.length === 2 && keys.includes('kind') && keys.includes('monitorId') &&
      typeof target.monitorId === 'string' && target.monitorId.trim().length > 0
  }
  if (target.kind === 'category-monitor') {
    return keys.length === 3 && keys.includes('kind') && keys.includes('monitorId') && keys.includes('productUrl') &&
      typeof target.monitorId === 'string' && target.monitorId.trim().length > 0 &&
      typeof target.productUrl === 'string' && target.productUrl.trim().length > 0
  }
  return (target.kind === 'search' || target.kind === 'cross') &&
    keys.length === 2 && keys.includes('kind') && keys.includes('jobId') &&
    typeof target.jobId === 'string' && target.jobId.trim().length > 0
}

export const notificationRoutePath = (target: NotificationRouteTarget) => (
  target.kind === 'monitor'
    ? `${ROUTES.monitor}?monitorId=${encodeURIComponent(target.monitorId)}`
    : target.kind === 'category-monitor'
      ? `${ROUTES.monitor}?categoryMonitorId=${encodeURIComponent(target.monitorId)}&categoryProductUrl=${encodeURIComponent(target.productUrl)}`
    : deepSearchPath(target.kind, target.jobId)
)

export type AppNotification = {
  id: string
  kind: AppNotificationKind
  title: string
  message: string
  createdAt: string
  source?: string
  read: boolean
  tab?: string
  routeTarget?: NotificationRouteTarget
}

export type AppNotificationInput = {
  kind?: AppNotificationKind
  title: string
  message: string
  source?: string
  routeTarget?: NotificationRouteTarget
}

type NotificationState = {
  notifications: AppNotification[]
  /** Timestamp (epoch ms) of the last syncBackend call that introduced new unread items.
   *  Used by the bell icon to trigger a pulse animation. */
  lastNewUnreadAt: number
  addNotification: (input: AppNotificationInput) => AppNotification
  markRead: (id: string) => void
  markAllRead: () => void
  clearNotifications: () => void
  syncBackend: (backendNotifications: unknown[]) => void
}

type BackendNotification = {
  id: string
  title: string
  message: string
  tab?: string
  route_target?: unknown
  timestamp: string
  read: boolean
}

const isBackendNotification = (value: unknown): value is BackendNotification => {
  if (typeof value !== 'object' || value === null) return false
  const notification = value as Record<string, unknown>
  return (
    typeof notification.id === 'string' &&
    typeof notification.title === 'string' &&
    typeof notification.message === 'string' &&
    (notification.tab === undefined || typeof notification.tab === 'string') &&
    typeof notification.timestamp === 'string' &&
    typeof notification.read === 'boolean'
  )
}

const NOTIFICATION_KINDS = new Set<AppNotificationKind>(['price', 'process', 'success', 'error', 'info'])

const normalizeAppNotification = (value: unknown): AppNotification | null => {
  if (typeof value !== 'object' || value === null) return null
  const notification = value as Record<string, unknown>
  const isValid = (
    typeof notification.id === 'string' &&
    typeof notification.kind === 'string' &&
    NOTIFICATION_KINDS.has(notification.kind as AppNotificationKind) &&
    typeof notification.title === 'string' &&
    typeof notification.message === 'string' &&
    typeof notification.createdAt === 'string' &&
    (notification.source === undefined || typeof notification.source === 'string') &&
    typeof notification.read === 'boolean' &&
    (notification.tab === undefined || typeof notification.tab === 'string')
  )

  if (!isValid) return null

  return {
    id: notification.id as string,
    kind: notification.kind as AppNotificationKind,
    title: notification.title as string,
    message: notification.message as string,
    createdAt: notification.createdAt as string,
    read: notification.read as boolean,
    ...(typeof notification.source === 'string' ? { source: notification.source } : {}),
    ...(typeof notification.tab === 'string' ? { tab: notification.tab } : {}),
    ...(isNotificationRouteTarget(notification.routeTarget)
      ? { routeTarget: notification.routeTarget }
      : {}),
  }
}

const STORAGE_KEY = 'e-scrapper.notifications'
const MAX_NOTIFICATIONS = 80

const canUseStorage = () => typeof window !== 'undefined' && Boolean(window.localStorage)

const loadNotifications = (): AppNotification[] => {
  if (!canUseStorage()) return []
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .map(normalizeAppNotification)
      .filter((notification): notification is AppNotification => notification !== null)
      .slice(0, MAX_NOTIFICATIONS)
  } catch {
    return []
  }
}

const persistNotifications = (notifications: AppNotification[]) => {
  if (!canUseStorage()) return
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(notifications.slice(0, MAX_NOTIFICATIONS)))
  } catch {
    // Notificações são auxiliares; falha de storage não deve afetar o app.
  }
}

const createNotificationId = () => `${Date.now()}-${Math.random().toString(36).slice(2)}`

export const useNotificationStore = create<NotificationState>()((set) => ({
  notifications: loadNotifications(),
  lastNewUnreadAt: 0,

  addNotification: (input) => {
    const notification: AppNotification = {
      id: createNotificationId(),
      kind: input.kind || 'info',
      title: input.title,
      message: input.message,
      source: input.source,
      createdAt: new Date().toISOString(),
      read: false,
      ...(isNotificationRouteTarget(input.routeTarget) ? { routeTarget: input.routeTarget } : {}),
    }
    set((state) => {
      const notifications = [notification, ...state.notifications].slice(0, MAX_NOTIFICATIONS)
      persistNotifications(notifications)
      return { notifications, lastNewUnreadAt: Date.now() }
    })
    return notification
  },

  markRead: (id) => set((state) => {
    const notifications = state.notifications.map(item => item.id === id ? { ...item, read: true } : item)
    persistNotifications(notifications)
    return { notifications }
  }),

  markAllRead: () => set((state) => {
    const notifications = state.notifications.map(item => ({ ...item, read: true }))
    persistNotifications(notifications)
    return { notifications }
  }),

  clearNotifications: () => {
    persistNotifications([])
    set({ notifications: [] })
  },

  syncBackend: (backendNotifications) => set((state) => {
    let changed = false;
    let newUnreadCount = 0;
    const newLocal = [...state.notifications];

    // O(1) lookup for existing IDs
    const existingIds = new Set(newLocal.map(n => n.id));

    for (const candidate of backendNotifications) {
      if (!isBackendNotification(candidate)) continue;
      const bn = candidate;

      if (!existingIds.has(bn.id)) {
        // New notification from backend — add it and toast if unread
        newLocal.push({
          id: bn.id,
          kind: 'info',
          title: bn.title,
          message: bn.message,
          source: 'Sistema',
          tab: bn.tab,
          routeTarget: isNotificationRouteTarget(bn.route_target) ? bn.route_target : undefined,
          createdAt: bn.timestamp,
          read: bn.read
        });
        existingIds.add(bn.id);
        changed = true;

        if (!bn.read) {
          newUnreadCount++;
          toast(bn.title, {
            description: bn.message,
            duration: 8000,
          });
        }
      } else {
        // Existing notifications can predate route targets. Hydrate a valid
        // backend target so they become navigable after an app upgrade.
        const existing = newLocal.find(n => n.id === bn.id);
        const backendRouteTarget = isNotificationRouteTarget(bn.route_target)
          ? bn.route_target
          : undefined;
        if (existing && !existing.routeTarget && backendRouteTarget) {
          existing.routeTarget = backendRouteTarget;
          changed = true;
        }
        if (existing && !existing.read && bn.read) {
          existing.read = true;
          changed = true;
        }
      }
    }

    if (changed) {
      newLocal.sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
      const sliced = newLocal.slice(0, MAX_NOTIFICATIONS);
      persistNotifications(sliced);
      return {
        notifications: sliced,
        ...(newUnreadCount > 0 ? { lastNewUnreadAt: Date.now() } : {}),
      };
    }
    return state;
  }),
}))

export const pushNotification = (input: AppNotificationInput) => (
  useNotificationStore.getState().addNotification(input)
)
