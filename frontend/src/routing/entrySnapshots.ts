export const SNAPSHOT_CACHE_LIMIT = 50

export interface SearchEntrySnapshot {
  kind: 'search'
  query: string
  sort: string
  zipcode: string
  selectedBrands: string[]
  results: unknown | null
}

export interface CrossEntrySnapshot {
  kind: 'cross'
  targetSku: string
  zipcode: string
  results: unknown | null
}

export type EntrySnapshot = SearchEntrySnapshot | CrossEntrySnapshot

// Intencionalmente module-scoped: os snapshots pertencem apenas à aba atual e
// desaparecem num reload. A key é a location.key opaca, nunca pathname/RouteId.
const entrySnapshots = new Map<string, EntrySnapshot>()

const cloneSnapshot = <T extends EntrySnapshot>(snapshot: T): T =>
  structuredClone(snapshot)

export const captureEntrySnapshot = (
  entryKey: string,
  snapshot: EntrySnapshot,
): void => {
  entrySnapshots.set(entryKey, cloneSnapshot(snapshot))

  // FIFO simples e explícito impede retenção sem limite durante navegação longa.
  if (entrySnapshots.size > SNAPSHOT_CACHE_LIMIT) {
    const oldestKey = entrySnapshots.keys().next().value
    if (oldestKey !== undefined) entrySnapshots.delete(oldestKey)
  }
}

export const restoreEntrySnapshot = (
  entryKey: string,
): EntrySnapshot | undefined => {
  const snapshot = entrySnapshots.get(entryKey)
  return snapshot === undefined ? undefined : cloneSnapshot(snapshot)
}

export const dropEntrySnapshot = (entryKey: string): void => {
  entrySnapshots.delete(entryKey)
}

export const clearEntrySnapshots = (): void => {
  entrySnapshots.clear()
}
