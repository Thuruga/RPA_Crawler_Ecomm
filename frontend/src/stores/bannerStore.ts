import { create } from 'zustand'
import { toast } from 'sonner'
import { ApiClient } from '../api/client'
import { pushNotification } from './notificationStore'
import { startAssistedBannerCapture, type AssistedCaptureRequest } from '../features/banners/assistedCapture'

export type BannerRunStatus = 'RUNNING' | 'REVIEW' | 'COMPLETED' | 'PARTIAL' | 'CANCELLED' | 'FAILED'
export type BannerBrandStatus = 'PENDING' | 'RUNNING' | 'WAITING_BROWSER' | 'COMPLETED' | 'BLOCKED' | 'NO_BANNERS' | 'FAILED' | 'CANCELLED'

export interface BannerCandidate {
  banner_id: string
  brand_key: string
  brand_name: string
  slide_order: number
  friendly_filename: string
  source_url: string
  rendered_url: string
  click_url?: string | null
  alt_text?: string | null
  dom_kind: string
  rendered_width?: number | null
  rendered_height?: number | null
  natural_width?: number | null
  natural_height?: number | null
  asset: { sha256: string; extension: string; content_type: string; byte_count: number }
}

export interface BannerVideoSlide {
  brand_key: string
  slide_order: number
  source_url?: string | null
  click_url?: string | null
  alt_text?: string | null
}

export interface BannerBrandProgress {
  brand_key: string
  brand_name: string
  status: BannerBrandStatus
  banner_count: number
  video_count: number
  error?: string | null
  screenshot_asset?: unknown
}

export interface BannerRun {
  run_id: string
  selected_brands: string[]
  status: BannerRunStatus
  brand_progress: Record<string, BannerBrandProgress>
  banners: BannerCandidate[]
  video_slides?: BannerVideoSlide[]
  created_at: string
  approved_at?: string | null
  error?: string | null
}

export interface BannerHistoryItem {
  run_id: string
  created_at: string
  approved_at: string
  banner_count: number
  brand_count: number
  status: 'COMPLETED'
}

interface BannerStoreState {
  selectedBrands: string[]
  selectionInitialized: boolean
  starting: boolean
  activeJobId: string | null
  run: BannerRun | null
  selectedBannerIds: string[]
  seenBannerIds: string[]
  history: BannerHistoryItem[]
  historyLoading: boolean
  assistedRequest: AssistedCaptureRequest | null
  setSelectedBrands: (brands: string[]) => void
  initializeBrands: (brands: string[]) => void
  start: () => Promise<void>
  retryAssisted: () => Promise<void>
  stop: () => Promise<void>
  refresh: (jobId?: string) => Promise<void>
  toggleBanner: (bannerId: string) => void
  selectAllBanners: () => void
  clearBanners: () => void
  approve: () => Promise<void>
  loadHistory: () => Promise<void>
  reopenHistory: (jobId: string) => Promise<void>
  deleteHistory: (jobId: string) => Promise<void>
}

let pollGeneration = 0
const TERMINAL = new Set<BannerRunStatus>(['REVIEW', 'COMPLETED', 'PARTIAL', 'CANCELLED', 'FAILED'])
const notifiedBannerTerminals = new Set<string>()
const POLL_INTERVAL_MS = 750
const POLL_RETRY_INTERVAL_MS = 2_000

function selectionForRun(state: BannerStoreState, run: BannerRun) {
  const incoming = run.banners.map(item => item.banner_id)
  const fresh = incoming.filter(id => !state.seenBannerIds.includes(id))
  return {
    selectedBannerIds: [...new Set([...state.selectedBannerIds, ...fresh])],
    seenBannerIds: [...new Set([...state.seenBannerIds, ...incoming])],
  }
}

export const useBannerStore = create<BannerStoreState>()((set, get) => ({
  selectedBrands: [],
  selectionInitialized: false,
  starting: false,
  activeJobId: null,
  run: null,
  selectedBannerIds: [],
  seenBannerIds: [],
  history: [],
  historyLoading: false,
  assistedRequest: null,

  setSelectedBrands: (brands) => set({ selectedBrands: brands }),
  initializeBrands: (brands) => {
    if (!brands.length) return
    set(state => state.selectionInitialized
      ? state
      : { selectedBrands: brands, selectionInitialized: true })
  },

  start: async () => {
    if (get().starting) return
    const brands = get().selectedBrands
    if (!brands.length) return
    const generation = ++pollGeneration
    set({ starting: true })
    try {
      const response = await ApiClient.startBannerJob(brands)
      const run = response.run as BannerRun
      set({
        activeJobId: response.job_id,
        starting: false,
        run,
        selectedBannerIds: [],
        seenBannerIds: [],
        assistedRequest: response.assisted_capture || null,
      })
      if (response.assisted_capture) {
        try {
          await startAssistedBannerCapture(response.assisted_capture)
          toast.info('Zara e Lacoste serão capturadas pelo navegador. Mantenha as abas abertas.')
        } catch (error) {
          toast.error(`${(error as Error).message}. Recarregue a extensão e use “Tentar novamente”.`)
        }
      }
      if (generation === pollGeneration) void get().refresh(response.job_id)
    } catch (error) {
      set({ starting: false })
      toast.error(`Erro ao iniciar extração: ${(error as Error).message}`)
    }
  },

  retryAssisted: async () => {
    const request = get().assistedRequest
    if (!request) return
    try {
      await startAssistedBannerCapture(request)
      toast.info('Captura pelo navegador reiniciada.')
    } catch (error) {
      toast.error(`${(error as Error).message}. Verifique se a extensão está instalada e atualizada.`)
    }
  },

  refresh: async (jobId) => {
    const target = jobId || get().activeJobId
    if (!target) return
    const generation = pollGeneration
    try {
      const run = await ApiClient.getBannerJob(target) as BannerRun
      if (get().activeJobId !== target || generation !== pollGeneration) return
      const selection = selectionForRun(get(), run)
      set({ run, ...selection })
      if (run.status === 'RUNNING') {
        window.setTimeout(() => {
          if (get().activeJobId === target && generation === pollGeneration) void get().refresh(target)
        }, POLL_INTERVAL_MS)
      } else if (TERMINAL.has(run.status)) {
        const terminalKey = `${target}:${run.status}`
        const shouldNotify = !notifiedBannerTerminals.has(terminalKey)
        if (shouldNotify) notifiedBannerTerminals.add(terminalKey)
        if (run.status === 'REVIEW') {
          toast.success('Extração concluída. Revise os banners antes de aprovar.')
          if (shouldNotify) {
            pushNotification({
              kind: 'process',
              title: 'Extração de banners finalizada',
              message: `${run.banners.length} banner(s) encontrados para revisão.`,
              source: 'Banners',
            })
          }
        }
        if (run.status === 'CANCELLED') {
          toast.info('Extração interrompida.')
          if (shouldNotify) {
            pushNotification({
              kind: 'info',
              title: 'Extração de banners interrompida',
              message: 'A extração foi cancelada antes da conclusão.',
              source: 'Banners',
            })
          }
        }
        if (run.status === 'PARTIAL' || run.status === 'FAILED') {
          toast.error(run.error || 'A extração terminou com falhas.')
          if (shouldNotify) {
            pushNotification({
              kind: 'error',
              title: 'Extração de banners terminou com falhas',
              message: run.error || 'Revise os detalhes da extração.',
              source: 'Banners',
            })
          }
        }
      }
    } catch (error) {
      if (get().activeJobId === target) toast.error(`Erro ao acompanhar extração: ${(error as Error).message}`)
      // A transient fetch/network failure must not leave the UI frozen at the
      // last progress value. The generation guard prevents stale jobs from
      // reviving after a new extraction starts.
      if (get().activeJobId === target && generation === pollGeneration) {
        window.setTimeout(() => {
          if (get().activeJobId === target && generation === pollGeneration) void get().refresh(target)
        }, POLL_RETRY_INTERVAL_MS)
      }
    }
  },

  stop: async () => {
    const jobId = get().activeJobId
    if (!jobId) return
    try {
      await ApiClient.stopBannerJob(jobId)
      toast.info('Parada solicitada. Finalizando a etapa atual…')
      void get().refresh(jobId)
    } catch (error) {
      toast.error(`Não foi possível parar: ${(error as Error).message}`)
    }
  },

  toggleBanner: (bannerId) => set(state => ({
    selectedBannerIds: state.selectedBannerIds.includes(bannerId)
      ? state.selectedBannerIds.filter(id => id !== bannerId)
      : [...state.selectedBannerIds, bannerId],
  })),
  selectAllBanners: () => set(state => ({ selectedBannerIds: state.run?.banners.map(item => item.banner_id) || [] })),
  clearBanners: () => set({ selectedBannerIds: [] }),

  approve: async () => {
    const { activeJobId, selectedBannerIds, run: currentRun } = get()
    const hasOnlyVideos = (currentRun?.banners.length || 0) === 0 && (currentRun?.video_slides?.length || 0) > 0
    if (!activeJobId || (!selectedBannerIds.length && !hasOnlyVideos)) return
    try {
      const run = await ApiClient.approveBannerJob(activeJobId, selectedBannerIds) as BannerRun
      set({ run, selectedBannerIds: run.banners.map(item => item.banner_id), seenBannerIds: run.banners.map(item => item.banner_id) })
      const countLabel = run.banners.length > 0 ? `${run.banners.length} banners` : `${run.video_slides?.length || 0} vídeos`
      toast.success(`${countLabel} aprovados e adicionados ao histórico.`)
      pushNotification({
        kind: 'success',
        title: 'Banners aprovados',
        message: `${countLabel} adicionados ao histórico.`,
        source: 'Banners',
      })
      await get().loadHistory()
    } catch (error) {
      toast.error(`Erro ao aprovar banners: ${(error as Error).message}`)
    }
  },

  loadHistory: async () => {
    set({ historyLoading: true })
    try {
      const history = await ApiClient.getBannerHistory() as BannerHistoryItem[]
      set({ history, historyLoading: false })
    } catch (error) {
      set({ historyLoading: false })
      toast.error(`Erro ao carregar histórico: ${(error as Error).message}`)
    }
  },

  reopenHistory: async (jobId) => {
    const generation = ++pollGeneration
    try {
      const run = await ApiClient.getBannerHistoryDetail(jobId) as BannerRun
      if (generation !== pollGeneration) return
      const ids = run.banners.map(item => item.banner_id)
      set({ activeJobId: jobId, run, selectedBannerIds: ids, seenBannerIds: ids })
    } catch (error) {
      toast.error(`Erro ao reabrir extração: ${(error as Error).message}`)
    }
  },

  deleteHistory: async (jobId) => {
    try {
      await ApiClient.deleteBannerHistory(jobId)
      if (get().activeJobId === jobId) {
        ++pollGeneration
        set({ activeJobId: null, run: null, selectedBannerIds: [], seenBannerIds: [], assistedRequest: null })
      }
      await get().loadHistory()
    } catch (error) {
      toast.error(`Erro ao excluir histórico: ${(error as Error).message}`)
    }
  },
}))
