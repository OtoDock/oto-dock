import { useState, useRef, useCallback, useLayoutEffect, useMemo } from 'react'
import { type SearchContextValue, SearchContext } from './useSearch'

interface RegistryEntry {
  count: number
  order: number  // explicit sort key — msgIdx * 1000 + blockIdx
}

export function SearchProvider({ query, children }: { query: string; children: React.ReactNode }) {
  const [currentMatch, setCurrentMatch] = useState(0)
  const [totalMatches, setTotalMatches] = useState(0)
  // The registry belongs to the query it was filled for: a new query finds
  // it empty. The callbacks check that, after the layout effect below has
  // written the query; the children register in passive effects, so a
  // fresh registration is never wiped.
  const registryRef = useRef<{ forQuery: string; map: Map<string, RegistryEntry> }>({ forQuery: query, map: new Map() })
  const queryRef = useRef(query)
  useLayoutEffect(() => { queryRef.current = query })
  const registry = useCallback(() => {
    const r = registryRef.current
    if (r.forQuery !== queryRef.current) {
      r.forQuery = queryRef.current
      r.map.clear()
    }
    return r.map
  }, [])
  const flushTimerRef = useRef<number>(0)

  // Get entries sorted by explicit order, then by id for stability within same block
  const getSortedEntries = useCallback(() => {
    return [...registry().entries()].sort((a, b) => {
      const orderDiff = a[1].order - b[1].order
      if (orderDiff !== 0) return orderDiff
      return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0
    })
  }, [registry])

  const flushRegistry = useCallback(() => {
    let sum = 0
    for (const [, entry] of registry()) sum += entry.count
    setTotalMatches(sum)
  }, [registry])

  const scheduleFlush = useCallback(() => {
    cancelAnimationFrame(flushTimerRef.current)
    flushTimerRef.current = requestAnimationFrame(flushRegistry)
  }, [flushRegistry])

  const registerMatches = useCallback((id: string, count: number, order: number) => {
    const map = registry()
    const prev = map.get(id)
    if (prev && prev.count === count && prev.order === order) return  // no change
    map.set(id, { count, order })
    scheduleFlush()
  }, [registry, scheduleFlush])

  const unregisterMatches = useCallback((id: string) => {
    const map = registry()
    if (!map.has(id)) return
    map.delete(id)
    scheduleFlush()
  }, [registry, scheduleFlush])

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

  // A new query starts at the first match (in render, on the change).
  const [seenQuery, setSeenQuery] = useState(query)
  if (seenQuery !== query) {
    setSeenQuery(query)
    if (currentMatch !== 0) setCurrentMatch(0)
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
