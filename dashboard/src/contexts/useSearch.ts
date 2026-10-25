// The chat find bar's state as the highlighted blocks read it: the context
// object and `useSearch`. SearchProvider (SearchContext.tsx) owns it.

import { createContext, useContext } from 'react'

export interface SearchContextValue {
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

export const SearchContext = createContext<SearchContextValue>({
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
