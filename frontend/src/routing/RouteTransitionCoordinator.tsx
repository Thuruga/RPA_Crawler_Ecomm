import { createContext, useCallback, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react'
import { useLocation, useNavigate, useNavigationType, type NavigateOptions } from 'react-router'

import { dropEntrySnapshot } from './entrySnapshots'
import { resolveRouteMetadata, type SearchKind } from './paths'
import { useSearchStore } from '../stores/searchStore'

type NavigationIntent = {
  resetBase?: boolean
  targetKind?: SearchKind
}

type RouteTransitionContextValue = {
  prepareNavigation: (targetKind?: SearchKind, resetBase?: boolean) => void
  captureCurrentEntry: (targetKind?: SearchKind, resetBase?: boolean) => void
  navigate: (to: string, options?: NavigateOptions & NavigationIntent) => void
  navigateWithSnapshot: (to: string, options?: NavigateOptions & NavigationIntent) => void
}

const RouteTransitionContext = createContext<RouteTransitionContextValue | null>(null)

export function RouteTransitionCoordinator({ children }: { children: ReactNode }) {
  const location = useLocation()
  const navigationType = useNavigationType()
  const routerNavigate = useNavigate()
  const locationRef = useRef(location)
  const intentRef = useRef<NavigationIntent | null>(null)
  const initializedRef = useRef(false)
  const handledKeyRef = useRef<string | null>(null)

  const prepareNavigation = useCallback((targetKind?: SearchKind, resetBase = false) => {
    const current = locationRef.current
    const currentRoute = resolveRouteMetadata(current.pathname)
    if (currentRoute.id === 'search' || currentRoute.id === 'cross') {
      useSearchStore.getState().captureNavigableSnapshot(current.key, currentRoute.id)
    }
    intentRef.current = { targetKind, resetBase }
  }, [])

  const navigate = useCallback((to: string, options: NavigateOptions & NavigationIntent = { resetBase: false }) => {
    const { targetKind, resetBase = false, ...navigateOptions } = options
    prepareNavigation(targetKind, resetBase)
    routerNavigate(to, navigateOptions)
  }, [prepareNavigation, routerNavigate])

  useEffect(() => {
    locationRef.current = location
    if (!initializedRef.current) {
      initializedRef.current = true
      handledKeyRef.current = location.key
      return
    }
    if (handledKeyRef.current === location.key) return
    handledKeyRef.current = location.key

    const route = resolveRouteMetadata(location.pathname)
    const kind = route.id === 'search' || route.id === 'cross' ? route.id : undefined
    const intent = intentRef.current
    intentRef.current = null

    if (navigationType === 'POP' && kind) {
      useSearchStore.getState().restoreNavigableSnapshot(location.key, kind)
      return
    }

    if (navigationType === 'PUSH' && intent?.resetBase && kind && route.jobId === undefined) {
      if (kind === 'search') useSearchStore.getState().resetSearchSlice()
      else useSearchStore.getState().resetCrossSlice()
      dropEntrySnapshot(location.key)
    }
  }, [location, navigationType])

  const value = useMemo(() => ({
    prepareNavigation,
    captureCurrentEntry: prepareNavigation,
    navigate,
    navigateWithSnapshot: navigate,
  }), [navigate, prepareNavigation])
  return <RouteTransitionContext.Provider value={value}>{children}</RouteTransitionContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useRouteTransitionCoordinator(): RouteTransitionContextValue {
  const value = useContext(RouteTransitionContext)
  if (!value) throw new Error('useRouteTransitionCoordinator must be used inside RouteTransitionCoordinator')
  return value
}
