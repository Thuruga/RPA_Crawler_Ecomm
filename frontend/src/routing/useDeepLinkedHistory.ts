import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'

import { ApiClient, ApiHttpError, type JobProgress } from '../api/client'
import { useSearchStore, withDisplayOrder } from '../stores/searchStore'
import { deepSearchPath, type SearchKind } from './paths'

type DeepHistoryStatus =
  | 'idle'
  | 'loading'
  | 'retrying'
  | 'ready'
  | 'pending'
  | 'running'
  | 'interrupted'
  | 'failed'
  | 'not-found'
  | 'temporary-error'
  | 'wrong-type'

export type DeepHistoryState = {
  status: DeepHistoryStatus
  progress: JobProgress | null
  error: string | null
  retry: () => void
}

const historyKind = (value: unknown): SearchKind | null =>
  value === 'search' || value === 'cross' ? value : null

const normalizedStatus = (value: unknown): string =>
  typeof value === 'string' ? value.toLowerCase() : ''

// Fallback local caso o snapshot não traga poll_after_ms — espelha o default
// do backend (SearchHistorySnapshot.poll_after_ms = 1500, routes_history.py).
const DEFAULT_POLL_AFTER_MS = 1500

// D-05/D-06: pending/running seguem em voo; interrupted foi encontrado órfão
// no boot e é retomado do zero automaticamente — nenhum dos três é honesto
// como terminal ainda, então o deep link continua consultando o snapshot.
const POLLING_STATUSES = new Set(['pending', 'running', 'interrupted'])

export function useDeepLinkedHistory(kind: SearchKind, jobId?: string): DeepHistoryState {
  const navigate = useNavigate()
  const [attempt, setAttempt] = useState(0)
  const [status, setStatus] = useState<DeepHistoryStatus>(jobId ? 'loading' : 'idle')
  const [progress, setProgress] = useState<JobProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const generation = useRef(0)

  const retry = useCallback(() => {
    setStatus('retrying')
    setAttempt(value => value + 1)
  }, [])

  useEffect(() => {
    if (!jobId) {
      // The branch synchronizes local state with a removed route parameter.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStatus('idle')
      setProgress(null)
      setError(null)
      return
    }

    const token = ++generation.current
    // Cancelamento sequencial (T-07-29): apenas um fetch em voo por vez; o
    // timer do próximo poll só nasce depois do settle do anterior, e ambos
    // ficam nesta única closure para o cleanup poder abortar/limpar de fato
    // o que está ativo neste exato momento, mesmo após vários ciclos.
    let timerId: ReturnType<typeof window.setTimeout> | null = null
    let activeController: AbortController | null = null

    const store = useSearchStore.getState()
    const slice = kind === 'search' ? store.search : store.cross
    slice.abortController?.abort()

    const firstController = new AbortController()
    activeController = firstController

    if (kind === 'search') {
      store.setSearch({
        loading: true,
        results: null,
        loadingPreloadId: jobId,
        abortController: firstController,
      })
    } else {
      store.setCross({
        loading: true,
        results: null,
        selectedItems: new Set(),
        selectionMode: false,
        loadingPreloadId: jobId,
        abortController: firstController,
      })
    }
    setStatus(current => current === 'retrying' ? current : 'loading')
    setProgress(null)
    setError(null)

    // T-07-30: generation token + loadingPreloadId (job_id) recusam qualquer
    // resposta que não pertença mais ao ciclo vigente — mesmo que resolva
    // depois do abort ou depois de trocar de job/rota.
    const isCurrent = () => {
      if (generation.current !== token) return false
      const current = useSearchStore.getState()
      return (kind === 'search' ? current.search : current.cross).loadingPreloadId === jobId
    }

    const poll = (controller: AbortController) => {
      ApiClient.getHistoryDetail(jobId, controller.signal)
        .then(record => {
          if (controller.signal.aborted || !isCurrent()) return

          const actualKind = historyKind(record?.type)
          if (actualKind && actualKind !== kind) {
            if (kind === 'search') useSearchStore.getState().resetSearchSlice()
            else useSearchStore.getState().resetCrossSlice()
            setStatus('wrong-type')
            navigate(deepSearchPath(actualKind, jobId), { replace: true })
            return
          }

          const recordStatus = normalizedStatus(record?.status)

          if (POLLING_STATUSES.has(recordStatus)) {
            if (kind === 'search') {
              useSearchStore.getState().setSearch({ loading: false, results: null, abortController: null })
            } else {
              useSearchStore.getState().setCross({ loading: false, results: null, abortController: null })
            }
            setStatus(recordStatus as DeepHistoryStatus)
            setProgress(record?.progress ?? null)
            setError(null)

            const delay = typeof record?.poll_after_ms === 'number' ? record.poll_after_ms : DEFAULT_POLL_AFTER_MS
            const nextController = new AbortController()
            activeController = nextController
            timerId = window.setTimeout(() => {
              timerId = null
              if (!isCurrent()) return
              poll(nextController)
            }, delay)
            return
          }

          if (recordStatus === 'failed') {
            // Terminal honesto (D-05): erro real do site/dado, distinto de
            // interrupted (retomada automática) — não agenda novo poll.
            if (kind === 'search') useSearchStore.getState().setSearch({ loading: false, abortController: null })
            else useSearchStore.getState().setCross({ loading: false, abortController: null })
            setStatus('failed')
            setProgress(record?.progress ?? null)
            setError(typeof record?.error === 'string' ? record.error : null)
            return
          }

          if (recordStatus !== 'completed') {
            if (kind === 'search') useSearchStore.getState().setSearch({ loading: false, abortController: null })
            else useSearchStore.getState().setCross({ loading: false, abortController: null })
            setStatus('temporary-error')
            return
          }

          if (kind === 'search') {
            useSearchStore.getState().setSearch({
              results: { results: record.results ?? [], query: record.query, brands_searched: record.brands },
              query: record.query || '',
              selectedBrands: Array.isArray(record.brands) ? record.brands : [],
              loading: false,
              loadingPreloadId: null,
              abortController: null,
            })
          } else {
            useSearchStore.getState().setCross({
              results: withDisplayOrder(record.results),
              targetSku: record.target_sku || String(record.query || '').replace('SKU: ', ''),
              selectedItems: new Set(),
              selectionMode: false,
              loading: false,
              loadingPreloadId: null,
              abortController: null,
            })
          }
          setProgress(null)
          setError(null)
          setStatus('ready')
        })
        .catch(fetchError => {
          if (controller.signal.aborted || fetchError?.name === 'AbortError' || !isCurrent()) return
          if (kind === 'search') useSearchStore.getState().setSearch({ loading: false, abortController: null })
          else useSearchStore.getState().setCross({ loading: false, abortController: null })
          setStatus(fetchError instanceof ApiHttpError && fetchError.status === 404 ? 'not-found' : 'temporary-error')
        })
    }

    poll(firstController)

    return () => {
      activeController?.abort()
      if (timerId !== null) {
        window.clearTimeout(timerId)
        timerId = null
      }
      if (generation.current === token) generation.current += 1
    }
  }, [attempt, jobId, kind, navigate])

  return { status, progress, error, retry }
}
