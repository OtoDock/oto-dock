import { useEffect, useRef, useState } from 'react'

import { POPOVER } from '../ui/classes'
import { pushEscHandler } from '../../lib/escStack'
import { useSearch } from '../../contexts/useSearch'
import type { OlderPage } from '../../hooks/useChatMessages'

interface Props {
  value: string
  onChange: (v: string) => void
  onClose: () => void
  /** Older messages of the chat are not loaded, and the list is shown. */
  canLoadOlder?: boolean
  loadingOlder?: boolean
  onLoadOlder?: () => Promise<OlderPage>
  /** A search result's anchor is still on its way: no first scroll. */
  anchorPending?: boolean
  /** The bubble the anchor landed on: the bar takes its first match. */
  anchorLanded?: { bubbleId: string } | null
}

/** How many older pages one "Search them" press may load. */
export const SEEK_PAGES = 20
// A page another loader holds is asked again this often, then the press
// gives up.
const STALE_RETRY_MS = 300
const STALE_RETRIES = 3

function scrollToMatch(index: number): void {
  document.querySelector(`[data-search-index="${index}"]`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// Two frames: the matches of what just rendered register in effects and
// are counted in a frame.
function afterCount(fn: () => void): () => void {
  let second = 0
  const first = requestAnimationFrame(() => { second = requestAnimationFrame(fn) })
  return () => { cancelAnimationFrame(first); cancelAnimationFrame(second) }
}

interface Seek { text: string; from: number; pages: number; stale: number; settling: boolean; waiting: boolean }

export default function FindBar({
  value, onChange, onClose, canLoadOlder = false, loadingOlder = false, onLoadOlder,
  anchorPending = false, anchorLanded = null,
}: Props) {
  const { query, totalMatches, currentMatch, setCurrentMatch, nextMatch, prevMatch } = useSearch()
  const inputRef = useRef<HTMLInputElement>(null)

  // A search result opening at its message: the bar starts at no match, so
  // its first scroll does not pull the view to the oldest loaded match, and
  // takes the anchored bubble's first match once the anchor lands.
  // Only an anchor this bar waited for: a landing from before it opened
  // (an earlier result, then Ctrl+F) moves nothing.
  const expectAnchor = useRef(false)
  useEffect(() => {
    if (!anchorPending) return
    expectAnchor.current = true
    if (query) setCurrentMatch(-1)
  }, [anchorPending, query, setCurrentMatch])
  useEffect(() => {
    if (!anchorLanded || !expectAnchor.current) return
    expectAnchor.current = false
    return afterCount(() => {
      const box = Array.from(document.querySelectorAll<HTMLElement>('[data-msg-id]'))
        .find((e) => e.dataset.msgId === anchorLanded.bubbleId)
      const inside = Array.from(box?.querySelectorAll<HTMLElement>('[data-search-index]') ?? [])
        .map((e) => Number(e.dataset.searchIndex)).filter((n) => Number.isFinite(n))
      if (inside.length) setCurrentMatch(Math.min(...inside))
    })
  }, [anchorLanded, setCurrentMatch])

  // "Search them": older pages load until a new match shows, the chat's
  // start, or SEEK_PAGES; then the bar moves to the newest of the new matches
  // and scrolls there itself (an index that stays the same scrolls nothing
  // through the list's own effect).
  const [seek, setSeek] = useState<Seek | null>(null)
  const [jump, setJump] = useState<{ index: number } | null>(null)
  const asking = useRef(false)
  // Decided in render: a new match found, or nothing left to look at.
  if (seek && seek.text !== value) setSeek(null)
  else if (seek && !seek.settling && !seek.waiting) {
    if (totalMatches > seek.from) {
      setSeek(null)
      setJump({ index: totalMatches - seek.from - 1 })
    } else if (!canLoadOlder || !onLoadOlder || seek.pages >= SEEK_PAGES || seek.stale > STALE_RETRIES) {
      setSeek(null)
    }
  }
  useEffect(() => {
    if (!jump) return
    setCurrentMatch(jump.index)
    return afterCount(() => scrollToMatch(jump.index))
  }, [jump, setCurrentMatch])
  // After a page lands the count settles first; a page another loader held
  // is asked again a moment later.
  const settling = !!seek?.settling
  const waiting = !!seek?.waiting
  useEffect(() => {
    if (!settling) return
    return afterCount(() => setSeek((s) => s && { ...s, settling: false }))
  }, [settling])
  useEffect(() => {
    if (!waiting) return
    const t = setTimeout(() => setSeek((s) => s && { ...s, waiting: false }), STALE_RETRY_MS)
    return () => clearTimeout(t)
  }, [waiting])
  useEffect(() => {
    if (!seek || seek.settling || seek.waiting || asking.current || !onLoadOlder) return
    // The page landing now counts; this runs again when it ends.
    if (loadingOlder) return
    asking.current = true
    void onLoadOlder().then((page) => {
      asking.current = false
      if (page === 'nothing') setSeek(null)
      else if (page === 'landed') setSeek((s) => s && { ...s, pages: s.pages + 1, stale: 0, settling: true })
      else setSeek((s) => s && { ...s, stale: s.stale + 1, waiting: true })   // another loader holds it
    })
  }, [seek, loadingOlder, onLoadOlder])

  // Auto-focus on mount
  useEffect(() => {
    inputRef.current?.focus()
    inputRef.current?.select()
  }, [])

  // Escape closes — registered on the precedence stack so we never collide
  // with other panels that also bind Escape.
  useEffect(() => pushEscHandler(onClose), [onClose])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (e.shiftKey) prevMatch()
      else nextMatch()
    }
  }

  const displayIndex = totalMatches > 0 ? currentMatch + 1 : 0

  return (
    <div className={`absolute top-12 right-3 left-3 sm:left-auto sm:right-4 z-20 flex flex-wrap items-center gap-1.5 px-3 py-1.5
                    ${POPOVER} max-w-[calc(100vw-1.5rem)]`}>
      {/* Search input */}
      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Find in chat..."
        className="w-44 min-w-0 shrink text-xs px-2 py-1 rounded-control border border-strong bg-surface-card
                   text-primary placeholder:text-tertiary
                   focus:outline-hidden focus:ring-1 focus:ring-focus-ring/40 focus:border-action"
      />

      {/* Match counter */}
      <span className="text-2xs text-secondary whitespace-nowrap min-w-[4.5rem] text-center">
        {value.trim() ? `${displayIndex} of ${totalMatches}` : ''}
      </span>

      {/* Navigation buttons */}
      <button
        onClick={prevMatch}
        disabled={totalMatches === 0}
        className="p-1 rounded-sm hover:bg-surface-muted text-secondary hover:text-primary
                   disabled:opacity-30 disabled:cursor-default transition-colors"
        title="Previous match: Shift+Enter"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
        </svg>
      </button>
      <button
        onClick={nextMatch}
        disabled={totalMatches === 0}
        className="p-1 rounded-sm hover:bg-surface-muted text-secondary hover:text-primary
                   disabled:opacity-30 disabled:cursor-default transition-colors"
        title="Next match: Enter"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Close button */}
      <button
        onClick={onClose}
        className="p-1 rounded-sm hover:bg-surface-muted text-tertiary hover:text-primary transition-colors"
        title="Close: Escape"
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>

      {/* The find bar sees only the loaded messages: say so, and offer to
          look further back, so a miss is never silent. */}
      {value.trim() && canLoadOlder && (
        <div data-testid="find-older" className="basis-full flex items-center gap-2 pt-1 text-2xs text-secondary">
          {seek ? (
            <span>Loading older messages…</span>
          ) : (
            <>
              <span>Older messages are not loaded.</span>
              <button
                onClick={() => setSeek({ text: value, from: totalMatches, pages: 0, stale: 0, settling: false, waiting: false })}
                className="px-1.5 py-0.5 rounded-sm text-action hover:bg-surface-muted transition-colors"
              >
                Search them
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}
