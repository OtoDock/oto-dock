/**
 * ScrollableTabs — a single-row tab bar that scrolls horizontally instead of
 * wrapping when the tabs don't fit (mobile, narrow desktop, or just many tabs).
 *
 * Affordances, so it never looks like a dead-end:
 *   - native scrollbar hidden (`.scrollbar-hide`), but scroll still works
 *   - edge-fade gradient on whichever side still has hidden tabs
 *   - left/right chevron buttons that appear only when the row overflows and
 *     are DISABLED (not hidden) at each end — so the control set stays stable
 *     and it's obvious which way there's more to see
 *   - the active tab is auto-scrolled into view (deep-linked `?tab=` included)
 *   - vertical mouse-wheel is translated to horizontal scroll on desktop, and
 *     released back to the page once the row hits an edge
 *
 * Shared by the User Settings and admin Setup pages so the two stay identical.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'

interface ScrollableTabsProps<T extends string> {
  tabs: { id: T; label: ReactNode }[]
  active: T
  onChange: (id: T) => void
  /** Extra classes for the outer track. */
  className?: string
}

export function ScrollableTabs<T extends string>({
  tabs,
  active,
  onChange,
  className = '',
}: ScrollableTabsProps<T>) {
  const rowRef = useRef<HTMLDivElement>(null)
  const tabRefs = useRef(new Map<T, HTMLButtonElement>())
  // Mouse drag-to-pan state. Touch/pen are left to native scrolling. A drag
  // past a few px flags `suppressClick` so the trailing click never selects a
  // tab — a real click (no movement) passes through untouched.
  const dragRef = useRef<{ startX: number; startScroll: number; pointerId: number; moved: boolean } | null>(null)
  const suppressClickRef = useRef(false)
  const [overflow, setOverflow] = useState(false)
  const [atStart, setAtStart] = useState(true)
  const [atEnd, setAtEnd] = useState(false)

  const measure = useCallback(() => {
    const el = rowRef.current
    if (!el) return
    const max = el.scrollWidth - el.clientWidth
    setOverflow(max > 1)
    setAtStart(el.scrollLeft <= 1)
    setAtEnd(el.scrollLeft >= max - 1)
  }, [])

  // Measure on mount and whenever the row's size or tab set changes. Layout
  // effect so the arrows render in the right state before the first paint.
  useLayoutEffect(() => {
    measure()
    const el = rowRef.current
    if (!el) return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [measure, tabs.length])

  // Vertical wheel → horizontal scroll (wheel mice; trackpads already pan
  // sideways). Non-passive so we can claim the gesture while there's room, and
  // let it fall through to the page once we reach an edge.
  useEffect(() => {
    const el = rowRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return
      const max = el.scrollWidth - el.clientWidth
      if (max <= 1) return
      const canScroll = (e.deltaY < 0 && el.scrollLeft > 0) || (e.deltaY > 0 && el.scrollLeft < max)
      if (!canScroll) return
      e.preventDefault()
      el.scrollLeft = Math.max(0, Math.min(max, el.scrollLeft + e.deltaY))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // Keep the active tab in view — nudges it on-screen if off, leaves it alone
  // if already visible (so a click doesn't jump the row around).
  useEffect(() => {
    tabRefs.current.get(active)?.scrollIntoView({ inline: 'nearest', block: 'nearest' })
  }, [active])

  const nudge = (dir: -1 | 1) => {
    const el = rowRef.current
    if (!el) return
    el.scrollBy({ left: dir * el.clientWidth * 0.75, behavior: 'smooth' })
  }

  // --- Mouse drag-to-pan (desktop only) ------------------------------------
  // Touch/pen fall through to native momentum scrolling; only a left-button
  // mouse press starts a drag, and only once it moves past the threshold.
  const DRAG_THRESHOLD = 5
  const onPointerDown = (e: React.PointerEvent) => {
    suppressClickRef.current = false
    if (e.pointerType !== 'mouse' || e.button !== 0) return
    const el = rowRef.current
    if (!el || el.scrollWidth - el.clientWidth <= 1) return // nothing to pan
    dragRef.current = { startX: e.clientX, startScroll: el.scrollLeft, pointerId: e.pointerId, moved: false }
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    const el = rowRef.current
    if (!d || !el) return
    const dx = e.clientX - d.startX
    if (!d.moved) {
      if (Math.abs(dx) < DRAG_THRESHOLD) return
      d.moved = true
      try { el.setPointerCapture(d.pointerId) } catch { /* pointer already gone */ }
    }
    el.scrollLeft = d.startScroll - dx
  }
  const endDrag = () => {
    const d = dragRef.current
    if (!d) return
    if (d.moved) {
      suppressClickRef.current = true // swallow the click this drag would fire
      try { rowRef.current?.releasePointerCapture(d.pointerId) } catch { /* noop */ }
    }
    dragRef.current = null
  }
  const onPointerLeave = () => {
    // Only relevant before the threshold is crossed — past it we hold pointer
    // capture and keep getting move/up events wherever the cursor goes.
    if (dragRef.current && !dragRef.current.moved) dragRef.current = null
  }
  const onClickCapture = (e: React.MouseEvent) => {
    if (suppressClickRef.current) {
      e.preventDefault()
      e.stopPropagation()
      suppressClickRef.current = false
    }
  }

  const arrowCls =
    'shrink-0 grid place-items-center w-6 h-7 rounded-md text-p-text-secondary ' +
    'hover:text-p-text hover:bg-white/70 dark:hover:bg-p-surface/70 transition-colors ' +
    'disabled:opacity-30 disabled:pointer-events-none'

  return (
    <div className={`flex items-center gap-1 bg-p-bg rounded-lg p-1 ${className}`}>
      {overflow && (
        <button type="button" aria-label="Scroll tabs left" disabled={atStart} onClick={() => nudge(-1)} className={arrowCls}>
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </button>
      )}

      <div className="relative flex-1 min-w-0">
        {/* Edge fades — only on the side that still has hidden tabs. */}
        {overflow && !atStart && (
          <div className="pointer-events-none absolute left-0 inset-y-0 z-10 w-5 bg-linear-to-r from-p-bg to-transparent" />
        )}
        {overflow && !atEnd && (
          <div className="pointer-events-none absolute right-0 inset-y-0 z-10 w-5 bg-linear-to-l from-p-bg to-transparent" />
        )}

        <div
          ref={rowRef}
          onScroll={measure}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onPointerLeave={onPointerLeave}
          onClickCapture={onClickCapture}
          className={`flex gap-1 overflow-x-auto scrollbar-hide select-none ${
            overflow ? 'cursor-grab active:cursor-grabbing' : ''
          }`}
        >
          {tabs.map((t) => (
            <button
              key={t.id}
              ref={(node) => {
                if (node) tabRefs.current.set(t.id, node)
                else tabRefs.current.delete(t.id)
              }}
              onClick={() => onChange(t.id)}
              aria-current={active === t.id}
              className={`whitespace-nowrap shrink-0 px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
                active === t.id
                  ? 'bg-white dark:bg-p-surface text-p-text shadow-xs'
                  : 'text-p-text-secondary hover:text-p-text'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {overflow && (
        <button type="button" aria-label="Scroll tabs right" disabled={atEnd} onClick={() => nudge(1)} className={arrowCls}>
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </button>
      )}
    </div>
  )
}
