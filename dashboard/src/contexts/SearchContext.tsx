import { createContext, useContext, useState, useRef, useCallback, useMemo } from 'react'

interface RegistryEntry {
  count: number
  order: number  // explicit sort key — msgIdx * 1000 + blockIdx
}

interface SearchContextValue {
  query: string
  currentMatch: number          // 0-based index of active match
  totalMatches: number
  setCurrentMatch: (n: number) => void
  nextMatch: () => void
  prevMatch: () => void
  // Match registry — each SearchHighlight block registers its count + order
  registerMatches: (id: string, count: number, order: number) => void
  unregisterMatches: (id: string) => void
  // Given a block id, return its global offset (sum of counts of all blocks before it, sorted by order)
  getGlobalOffset: (id: string) => number
}

const SearchContext = createContext<SearchContextValue>({
  query: '',
  currentMatch: 0,
  totalMatches: 0,
  setCurrentMatch: () => {},
  nextMatch: () => {},
  prevMatch: () => {},
  registerMatches: () => {},
  unregisterMatches: () => {},
  getGlobalOffset: () => 0,
})

export function useSearch() {
  return useContext(SearchContext)
}

export function SearchProvider({ query, children }: { query: string; children: React.ReactNode }) {
  const [currentMatch, setCurrentMatch] = useState(0)
  const [totalMatches, setTotalMatches] = useState(0)
  const registryRef = useRef<Map<string, RegistryEntry>>(new Map())
  const flushTimerRef = useRef<number>(0)

  // Get entries sorted by explicit order, then by id for stability within same block
  const getSortedEntries = useCallback(() => {
    return [...registryRef.current.entries()].sort((a, b) => {
      const orderDiff = a[1].order - b[1].order
      if (orderDiff !== 0) return orderDiff
      return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0
    })
  }, [])

  const flushRegistry = useCallback(() => {
    let sum = 0
    for (const [, entry] of registryRef.current) sum += entry.count
    setTotalMatches(sum)
  }, [])

  const scheduleFlush = useCallback(() => {
    cancelAnimationFrame(flushTimerRef.current)
    flushTimerRef.current = requestAnimationFrame(flushRegistry)
  }, [flushRegistry])

  const registerMatches = useCallback((id: string, count: number, order: number) => {
    const prev = registryRef.current.get(id)
    if (prev && prev.count === count && prev.order === order) return  // no change
    registryRef.current.set(id, { count, order })
    scheduleFlush()
  }, [scheduleFlush])

  const unregisterMatches = useCallback((id: string) => {
    if (!registryRef.current.has(id)) return
    registryRef.current.delete(id)
    scheduleFlush()
  }, [scheduleFlush])

  const getGlobalOffset = useCallback((id: string) => {
    let offset = 0
    for (const [key, entry] of getSortedEntries()) {
      if (key === id) break
      offset += entry.count
    }
    return offset
  }, [getSortedEntries])

  const nextMatch = useCallback(() => {
    setCurrentMatch((prev) => {
      const total = totalMatches
      if (total === 0) return 0
      return (prev + 1) % total
    })
  }, [totalMatches])

  const prevMatch = useCallback(() => {
    setCurrentMatch((prev) => {
      const total = totalMatches
      if (total === 0) return 0
      return (prev - 1 + total) % total
    })
  }, [totalMatches])

  // Reset currentMatch when query changes
  const prevQueryRef = useRef(query)
  if (prevQueryRef.current !== query) {
    prevQueryRef.current = query
    if (currentMatch !== 0) setCurrentMatch(0)
    registryRef.current.clear()
  }

  const value = useMemo<SearchContextValue>(() => ({
    query,
    currentMatch,
    totalMatches,
    setCurrentMatch,
    nextMatch,
    prevMatch,
    registerMatches,
    unregisterMatches,
    getGlobalOffset,
  }), [query, currentMatch, totalMatches, nextMatch, prevMatch, registerMatches, unregisterMatches, getGlobalOffset])

  return (
    <SearchContext.Provider value={value}>
      {children}
    </SearchContext.Provider>
  )
}
