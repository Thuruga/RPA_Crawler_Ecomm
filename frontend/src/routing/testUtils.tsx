// @vitest-environment jsdom

import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { afterEach } from 'vitest'

type MountedRoute = {
  container: HTMLDivElement
  root: Root
}

export type RenderRouteResult = {
  container: HTMLDivElement
  rerender: (ui: ReactNode) => Promise<void>
  unmount: () => Promise<void>
}

const mountedRoutes = new Set<MountedRoute>()

;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

async function renderInRouter(root: Root, ui: ReactNode): Promise<void> {
  await act(async () => {
    root.render(<BrowserRouter>{ui}</BrowserRouter>)
  })
}

export async function renderRoute(ui: ReactNode, initialPath = '/monitor'): Promise<RenderRouteResult> {
  window.history.replaceState(null, '', initialPath)

  const container = document.createElement('div')
  container.dataset.testRoot = 'route'
  document.body.append(container)

  const mounted = { container, root: createRoot(container) }
  mountedRoutes.add(mounted)
  await renderInRouter(mounted.root, ui)

  return {
    container,
    rerender: (nextUi) => renderInRouter(mounted.root, nextUi),
    unmount: async () => {
      if (!mountedRoutes.delete(mounted)) return
      await act(async () => mounted.root.unmount())
      mounted.container.remove()
    },
  }
}

export async function navigateBrowser(path: string, options: { replace?: boolean } = {}): Promise<void> {
  await act(async () => {
    if (options.replace) window.history.replaceState(null, '', path)
    else window.history.pushState(null, '', path)
    window.dispatchEvent(new PopStateEvent('popstate', { state: window.history.state }))
  })
}

export async function cleanupRoutes(): Promise<void> {
  const mounted = [...mountedRoutes]
  mountedRoutes.clear()

  for (const { root, container } of mounted) {
    await act(async () => root.unmount())
    container.remove()
  }

  document.body.replaceChildren()
  window.history.replaceState(null, '', '/')
}

afterEach(cleanupRoutes)
