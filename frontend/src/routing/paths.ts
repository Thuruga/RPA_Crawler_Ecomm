import { matchPath } from 'react-router'

export type SearchKind = 'search' | 'cross'
export type RouteId = 'monitor' | 'search' | 'cross' | 'category' | 'banners' | 'settings' | 'not-found'

export const ROUTES = {
  root: '/',
  monitor: '/monitor',
  search: '/comparativa',
  searchDeep: '/comparativa/:jobId',
  cross: '/sku',
  crossDeep: '/sku/:jobId',
  category: '/categorias',
  banners: '/banners',
  settings: '/marcas',
} as const

export type BaseRouteDefinition = {
  id: Exclude<RouteId, 'not-found'>
  path: string
  title: string
  sidebarLabel: string
  description: string
}

export const BASE_ROUTES: readonly BaseRouteDefinition[] = [
  {
    id: 'monitor',
    path: ROUTES.monitor,
    title: 'Painel de Monitoramento',
    sidebarLabel: 'Monitores',
    description: 'Acompanhe preços, disponibilidade e variações em uma leitura compacta.',
  },
  {
    id: 'search',
    path: ROUTES.search,
    title: 'Busca Comparativa',
    sidebarLabel: 'Comparativa',
    description: 'Compare preços, disponibilidade e frete entre marcas sem perder densidade de informação.',
  },
  {
    id: 'cross',
    path: ROUTES.cross,
    title: 'Busca por SKU',
    sidebarLabel: 'SKU',
    description: 'Compare ofertas, buy box e frete entre e-commerces e marketplaces concorrentes.',
  },
  {
    id: 'category',
    path: ROUTES.category,
    title: 'Varredura por Categoria',
    sidebarLabel: 'Categorias',
    description: 'Selecione uma categoria por marca e acompanhe a extração em tempo real.',
  },
  {
    id: 'banners',
    path: ROUTES.banners,
    title: 'Banners',
    sidebarLabel: 'Banners',
    description: 'Extraia, revise e aprove os banners das marcas ativas em um fluxo protegido.',
  },
  {
    id: 'settings',
    path: ROUTES.settings,
    title: 'Gerenciar Marcas',
    sidebarLabel: 'Marcas',
    description: 'Cadastre e gerencie as marcas disponíveis nas operações de monitoramento.',
  },
] as const

export type RouteMetadata = {
  id: RouteId
  title: string
  sidebarLabel: string | null
  basePath: string | null
  description?: string
  jobId?: string
}

const NOT_FOUND_METADATA: RouteMetadata = {
  id: 'not-found',
  title: 'Página não encontrada',
  sidebarLabel: null,
  basePath: null,
}

export const baseSearchPath = (kind: SearchKind): string => (
  kind === 'search' ? ROUTES.search : ROUTES.cross
)

export const deepSearchPath = (kind: SearchKind, jobId: string): string => (
  `${baseSearchPath(kind)}/${encodeURIComponent(jobId)}`
)

export function resolveRouteMetadata(pathname: string): RouteMetadata {
  for (const [kind, pattern] of [['search', ROUTES.searchDeep], ['cross', ROUTES.crossDeep]] as const) {
    const match = matchPath({ path: pattern, end: true }, pathname)
    if (match) {
      const base = BASE_ROUTES.find(route => route.id === kind)!
      return { ...base, basePath: base.path, jobId: match.params.jobId }
    }
  }

  const base = BASE_ROUTES.find(route => matchPath({ path: route.path, end: true }, pathname))
  return base ? { ...base, basePath: base.path } : NOT_FOUND_METADATA
}