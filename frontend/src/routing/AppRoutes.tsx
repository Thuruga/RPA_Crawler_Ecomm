import type { ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router'

import { ROUTES, type RouteId } from './paths'

export type RouteElements = Record<Exclude<RouteId, 'not-found'>, ReactNode>

type AppRoutesProps = {
  elements: RouteElements
  notFoundElement: ReactNode
}

export function AppRoutes({ elements, notFoundElement }: AppRoutesProps) {
  return (
    <Routes>
      <Route path={ROUTES.root} element={<Navigate to={ROUTES.monitor} replace />} />
      <Route path={ROUTES.monitor} element={elements.monitor} />
      <Route path={ROUTES.search} element={elements.search} />
      <Route path={ROUTES.searchDeep} element={elements.search} />
      <Route path={ROUTES.cross} element={elements.cross} />
      <Route path={ROUTES.crossDeep} element={elements.cross} />
      <Route path={ROUTES.category} element={elements.category} />
      <Route path={ROUTES.banners} element={elements.banners} />
      <Route path={ROUTES.settings} element={elements.settings} />
      <Route path="*" element={notFoundElement} />
    </Routes>
  )
}