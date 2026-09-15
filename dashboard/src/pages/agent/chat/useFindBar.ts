/**
 * useFindBar — AgentChat's find-bar state: the open flag, the raw input and
 * its 200 ms debounced query, the `?q=` deep link (captured here, deferred
 * until chat_history loads via `pendingFindQuery`, consumed by the stream's
 * onChatHistoryLoaded), the Ctrl/Cmd+F intercept and `closeFindBar`. Called
 * where the page declared the find-bar state, so the hook order is unchanged.
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import type { useSearchParams } from 'react-router-dom'

export function useFindBar({ searchParams, setSearchParams }: {
  searchParams: URLSearchParams
  setSearchParams: ReturnType<typeof useSearchParams>[1]
}) {
  // Find bar state
  const [findBarOpen, setFindBarOpen] = useState(false)
  const [findInput, setFindInput] = useState('')   // raw input (debounced before passing to context)
  const [findQuery, setFindQuery] = useState('')    // debounced query for SearchProvider
  const pendingFindQuery = useRef<string | null>(null)  // deferred until chat_history loads

  // --- Find bar: URL param integration + Ctrl+F + debounce ---

  // Capture ?q= param from URL but defer opening until chat_history loads.
  // Opening immediately causes the find bar to render before messages are available.
  useEffect(() => {
    const q = searchParams.get('q')
    if (q) {
      pendingFindQuery.current = q
      // Remove ?q from URL without re-navigation
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams])

  // Debounce find input → findQuery (200ms)
  useEffect(() => {
    const timer = setTimeout(() => setFindQuery(findInput), 200)
    return () => clearTimeout(timer)
  }, [findInput])

  // Ctrl+F / Cmd+F intercept
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault()
        setFindBarOpen(true)
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [])

  const closeFindBar = useCallback(() => {
    setFindBarOpen(false)
    setFindInput('')
    setFindQuery('')
  }, [])

  return {
    findBarOpen, setFindBarOpen, findInput, setFindInput, findQuery, setFindQuery,
    pendingFindQuery, closeFindBar,
  }
}
