/* eslint-disable @typescript-eslint/no-explicit-any */
import { create } from 'zustand'
import { toast } from 'sonner'
import { ApiClient } from '../api/client'
import {
  captureEntrySnapshot,
  restoreEntrySnapshot,
} from '../routing/entrySnapshots'
import { pushNotification } from './notificationStore'

// --- Tipos ---

// Resultado discriminado das actions de busca (CR-01): o caller precisa distinguir
// conclusão real de cancelamento/erro para só então pós-processar e refazer o histórico.
// D-08: 'success' agora significa "job aceito" (202), não "job concluído" — o backend
// é o único produtor do terminal, e o caller usa jobId para navegar para a URL durável
// (deepSearchPath) imediatamente, sem esperar o worker terminar.
export type SearchOutcome =
  | { status: 'success'; jobId: string }
  | { status: 'aborted' }
  | { status: 'error' }

interface SearchSlice {
  query: string
  sort: string
  // CEP confirmado para a sessão (sem default — só o que o usuário digitar no modal de frete).
  // Memory-only (sem persist) — reseta no reload.
  zipcode: string
  selectedBrands: string[]
  results: any | null
  loading: boolean
  abortController: AbortController | null
  // Job de histórico cuja pré-carga (getHistoryDetail) está em voo. Distingue
  // "reabrir histórico em voo" de "busca normal em voo" na guarda anti-duplo-fetch (WR-03).
  loadingPreloadId: string | null
}

interface CrossSlice {
  targetSku: string
  zipcode: string
  results: any | null
  // NOTE: selectedItems é Set<string> e NÃO é serializável (JSON.stringify retorna {}).
  // Compatível apenas com store em memória. D-05 proíbe middleware persist por este motivo.
  selectedItems: Set<string>
  selectionMode: boolean
  loading: boolean
  abortController: AbortController | null
  loadingPreloadId: string | null
}

interface SearchStoreState {
  search: SearchSlice
  cross: CrossSlice
  // Actions
  setSearch: (patch: Partial<SearchSlice>) => void
  setCross: (patch: Partial<CrossSlice>) => void
  captureNavigableSnapshot: (entryKey: string, kind: 'search' | 'cross') => void
  restoreNavigableSnapshot: (entryKey: string, kind: 'search' | 'cross') => boolean
  resetSearchSlice: () => void
  resetCrossSlice: () => void
  startSearch: (payload: any) => Promise<SearchOutcome>
  startCrossSearch: (payload: any) => Promise<SearchOutcome>
}

// Aplica _display_order aos resultados da busca por SKU, preservando a ordenação do backend.
// Module-scoped e exportada para que a action do store (fonte única — CR-01) e a pré-carga
// de histórico em App.tsx usem exatamente a mesma lógica (evita duplicação — IN-02).
export const withDisplayOrder = (data: any) => {
  if (!data || !Array.isArray(data.results)) return data
  return {
    ...data,
    results: data.results.map((item: any, index: number) => ({
      ...item,
      _display_order: item._display_order ?? index,
    })),
  }
}

const createDefaultSearchSlice = (): SearchSlice => ({
  query: '',
  sort: 'relevance',
  zipcode: '',
  selectedBrands: [],
  results: null,
  loading: false,
  abortController: null,
  loadingPreloadId: null,
})

const createDefaultCrossSlice = (): CrossSlice => ({
  targetSku: '',
  zipcode: '',
  results: null,
  selectedItems: new Set(),
  selectionMode: false,
  loading: false,
  abortController: null,
  loadingPreloadId: null,
})

// --- Store ---
// Store module-scoped (zustand): sobrevive ao ciclo de vida dos componentes,
// pois vive no escopo do módulo JS, fora da árvore React.
// NÃO usar middleware persist (D-05) — selectedItems (Set<string>) não é serializável
// e os resultados de busca não devem sobreviver a reload.

export const useSearchStore = create<SearchStoreState>()((set, get) => ({
  search: createDefaultSearchSlice(),
  cross: createDefaultCrossSlice(),

  setSearch: (patch) =>
    set((s) => ({ search: { ...s.search, ...patch } })),

  setCross: (patch) =>
    set((s) => ({ cross: { ...s.cross, ...patch } })),

  captureNavigableSnapshot: (entryKey, kind) => {
    const state = get()
    if (kind === 'search') {
      captureEntrySnapshot(entryKey, {
        kind,
        query: state.search.query,
        sort: state.search.sort,
        zipcode: state.search.zipcode,
        selectedBrands: state.search.selectedBrands,
        results: state.search.results,
      })
      return
    }

    captureEntrySnapshot(entryKey, {
      kind,
      targetSku: state.cross.targetSku,
      zipcode: state.cross.zipcode,
      results: state.cross.results,
    })
  },

  restoreNavigableSnapshot: (entryKey, kind) => {
    const snapshot = restoreEntrySnapshot(entryKey)
    if (snapshot === undefined || snapshot.kind !== kind) {
      if (kind === 'search') get().resetSearchSlice()
      else get().resetCrossSlice()
      return false
    }

    if (snapshot.kind === 'search') {
      get().search.abortController?.abort()
      set({
        search: {
          query: snapshot.query,
          sort: snapshot.sort,
          zipcode: snapshot.zipcode,
          selectedBrands: snapshot.selectedBrands,
          results: snapshot.results,
          loading: false,
          abortController: null,
          loadingPreloadId: null,
        },
      })
      return true
    }

    get().cross.abortController?.abort()
    set({
      cross: {
        targetSku: snapshot.targetSku,
        zipcode: snapshot.zipcode,
        results: snapshot.results,
        selectedItems: new Set(),
        selectionMode: false,
        loading: false,
        abortController: null,
        loadingPreloadId: null,
      },
    })
    return true
  },

  resetSearchSlice: () => {
    get().search.abortController?.abort()
    set({ search: createDefaultSearchSlice() })
  },

  resetCrossSlice: () => {
    get().cross.abortController?.abort()
    set({ cross: createDefaultCrossSlice() })
  },

  startSearch: async (payload) => {
    // Cancela request anterior se existir (AbortController vive na action, não nos componentes)
    get().search.abortController?.abort()
    const controller = new AbortController()

    set((s) => ({
      search: {
        ...s.search,
        loading: true,
        results: null,
        abortController: controller,
        loadingPreloadId: null,
      },
    }))

    try {
      const accepted = await ApiClient.search(payload, controller.signal)
      // Identity guard (WR-01/WR-02): um request mais novo já assumiu o controller.
      // Este resolveu tarde demais — não clobberar o estado do request vigente.
      if (get().search.abortController !== controller) return { status: 'aborted' }
      // D-08: 202 é aceitação, não conclusão — results permanece null e loading
      // permanece true (o job segue em andamento na URL durável). Nenhum toast de
      // conclusão aqui: o backend é o único produtor da notificação terminal.
      return { status: 'success', jobId: accepted.job_id }
    } catch (err: any) {
      if (err.name === 'AbortError') return { status: 'aborted' }  // cancelamento intencional — não notifica
      // Identity guard também no erro: só reporta/limpa se ainda for o request vigente.
      if (get().search.abortController !== controller) return { status: 'aborted' }
      set((s) => ({ search: { ...s.search, loading: false, abortController: null } }))
      toast.error('Erro na busca: ' + err.message)
      pushNotification({
        kind: 'error',
        title: 'Busca comparativa falhou',
        message: err.message || 'Não foi possível concluir a busca.',
        source: 'Busca Comparativa',
      })
      return { status: 'error' }
    }
  },

  startCrossSearch: async (payload) => {
    // Cancela request anterior se existir
    get().cross.abortController?.abort()
    const controller = new AbortController()

    set((s) => ({
      cross: {
        ...s.cross,
        loading: true,
        results: null,
        // Zera seleção ao iniciar nova busca (espelha reset que handleSearch da SKU faz hoje)
        selectedItems: new Set(),
        selectionMode: false,
        abortController: controller,
        loadingPreloadId: null,
      },
    }))

    try {
      const accepted = await ApiClient.crossMarketplaceSearch(payload, controller.signal)
      if (get().cross.abortController !== controller) return { status: 'aborted' }
      // D-08: 202 é aceitação, não conclusão — results permanece null e loading
      // permanece true (o job segue em andamento na URL durável). Nenhum toast de
      // conclusão aqui: o backend é o único produtor da notificação terminal.
      return { status: 'success', jobId: accepted.job_id }
    } catch (err: any) {
      if (err.name === 'AbortError') return { status: 'aborted' }  // cancelamento intencional — não notifica
      if (get().cross.abortController !== controller) return { status: 'aborted' }
      set((s) => ({ cross: { ...s.cross, loading: false, abortController: null } }))
      toast.error('Erro na busca: ' + err.message)
      pushNotification({
        kind: 'error',
        title: 'Busca por SKU falhou',
        message: err.message || 'Não foi possível concluir a busca.',
        source: 'Busca por SKU',
      })
      return { status: 'error' }
    }
  },
}))
