import { useEffect, useRef, useState } from 'react'

import { ApiClient, type CategoryScanRecord, type PriceCheckRun } from '../api/client'

// Alinhado ao polling da fase (07-VALIDATION.md W0-08); cada passagem futura
// do monitor de preço é um novo ciclo (D-02), não um progresso contínuo do
// mesmo job — por isso um intervalo fixo, sem backoff.
const POLL_INTERVAL_MS = 1500

export type MonitorJobPollingOptions = {
  priceMonitorIds: string[]
  categoryMonitorIds: string[]
  enabled: boolean
}

export type MonitorJobPollingState = {
  priceRunsByMonitorId: Record<string, PriceCheckRun[]>
  categoryScansByMonitorId: Record<string, CategoryScanRecord[]>
  loading: boolean
  error: string | null
}

// Chave estável do conjunto de ids visível, independente da ordem ou da
// identidade do array recebido a cada render — só reinicia o ciclo/timer
// quando o conjunto realmente muda (T-07-50).
const idsSetKey = (ids: string[]): string => [...ids].sort().join('|')

export function useMonitorJobPolling(options: MonitorJobPollingOptions): MonitorJobPollingState {
  const { priceMonitorIds, categoryMonitorIds, enabled } = options

  const [priceRunsByMonitorId, setPriceRunsByMonitorId] = useState<Record<string, PriceCheckRun[]>>({})
  const [categoryScansByMonitorId, setCategoryScansByMonitorId] = useState<Record<string, CategoryScanRecord[]>>({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // "Latest ids" ref: atualizado a cada render para que um ciclo em andamento
  // sempre consulte os ids visíveis mais recentes sem precisar reiniciar o
  // efeito quando só a identidade do array muda (mesmo conteúdo).
  const idsRef = useRef({ priceMonitorIds, categoryMonitorIds })
  useEffect(() => {
    idsRef.current = { priceMonitorIds, categoryMonitorIds }
  })

  const priceKey = idsSetKey(priceMonitorIds)
  const categoryKey = idsSetKey(categoryMonitorIds)

  const generation = useRef(0)

  useEffect(() => {
    if (!enabled) {
      const timer = setTimeout(() => setLoading(false), 0)
      return () => clearTimeout(timer)
    }

    const token = ++generation.current
    let timerId: ReturnType<typeof window.setTimeout> | null = null
    let activeControllers: AbortController[] = []

    const isCurrent = () => generation.current === token

    const runCycle = () => {
      const { priceMonitorIds: currentPriceIds, categoryMonitorIds: currentCategoryIds } = idsRef.current

      const priceControllers = currentPriceIds.map(() => new AbortController())
      const categoryControllers = currentCategoryIds.map(() => new AbortController())
      activeControllers = [...priceControllers, ...categoryControllers]

      setLoading(true)

      const pricePromises = currentPriceIds.map((jobId, index) =>
        ApiClient.getMonitorHistory(jobId, priceControllers[index].signal),
      )
      const categoryPromises = currentCategoryIds.map((monitorId, index) =>
        ApiClient.getCategoryScans(monitorId, categoryControllers[index].signal),
      )

      return Promise.allSettled([...pricePromises, ...categoryPromises]).then(results => {
        // Ciclo obsoleto (ids trocaram ou desmontou durante o voo): descarta
        // sem tocar em estado — o cleanup já abortou os controllers.
        if (!isCurrent()) return

        const priceResults = results.slice(0, pricePromises.length) as PromiseSettledResult<
          Awaited<ReturnType<typeof ApiClient.getMonitorHistory>>
        >[]
        const categoryResults = results.slice(pricePromises.length) as PromiseSettledResult<
          CategoryScanRecord[]
        >[]

        let sawError = false

        setPriceRunsByMonitorId(prev => {
          const next = { ...prev }
          priceResults.forEach((result, index) => {
            const jobId = currentPriceIds[index]
            if (result.status === 'fulfilled') {
              next[jobId] = result.value.check_runs ?? []
            } else {
              sawError = true
            }
          })
          return next
        })

        setCategoryScansByMonitorId(prev => {
          const next = { ...prev }
          categoryResults.forEach((result, index) => {
            const monitorId = currentCategoryIds[index]
            if (result.status === 'fulfilled') {
              next[monitorId] = result.value
            } else {
              sawError = true
            }
          })
          return next
        })

        setLoading(false)
        setError(sawError ? 'monitor-poll-error' : null)

        if (!isCurrent()) return
        // Nada visível: evita agendar um timer indefinidamente ocioso — o
        // efeito reinicia sozinho quando priceKey/categoryKey mudar.
        if (currentPriceIds.length + currentCategoryIds.length === 0) return

        timerId = window.setTimeout(() => {
          timerId = null
          if (!isCurrent()) return
          runCycle()
        }, POLL_INTERVAL_MS)
      })
    }

    runCycle()

    return () => {
      activeControllers.forEach(controller => controller.abort())
      if (timerId !== null) {
        window.clearTimeout(timerId)
        timerId = null
      }
      if (generation.current === token) generation.current += 1
    }
  }, [priceKey, categoryKey, enabled])

  return { priceRunsByMonitorId, categoryScansByMonitorId, loading, error }
}
