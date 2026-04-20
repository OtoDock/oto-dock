import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { ScopeKey, WorkspaceSection } from './sections'

interface Props {
  sections: WorkspaceSection[]
  activeKey: ScopeKey | ''
  onSelect: (section: WorkspaceSection) => void
}

/**
 * Top-row scope picker. Single horizontally-scrollable row of chips,
 * color-coded by ownership:
 *
 *   My Workspace / My Context           → brand blue
 *   Shared Workspace / Knowledge / Cfg  → accent purple
 *
 * Backend already filters scopes by role, so internal agents see only the
 * agent chips and viewers only see "My" chips.
 *
 * Layout choices:
 *  - Single row (no wrap) — visually consistent with the breadcrumb /
 *    toolbar rows below. Wrapping would change row height and break the
 *    rhythm; scroll is the right primitive when content overflows.
 *  - Fade gradients on the overflow edge(s) signal "more content over
 *    there" since the scrollbar is hidden (it would add vertical noise on
 *    a 1-line container).
 *  - Active chip auto-scrolls into view on mount and on activation, so a
 *    selection made from anywhere (e.g. external nav) lands visible.
 *  - `scroll-snap` keeps a finger-flick landing cleanly on a chip rather
 *    than mid-chip.
 */
export default function ScopeChips({ sections, activeKey, onSelect }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<HTMLButtonElement>(null)
  const [fadeLeft, setFadeLeft] = useState(false)
  const [fadeRight, setFadeRight] = useState(false)

  // Recompute which fade edges are needed on scroll/resize/section change.
  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const update = () => {
      // 2px tolerance to avoid flicker at exact boundaries from sub-pixel layout.
      setFadeLeft(el.scrollLeft > 2)
      setFadeRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 2)
    }
    update()
    el.addEventListener('scroll', update, { passive: true })
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => {
      el.removeEventListener('scroll', update)
      ro.disconnect()
    }
  }, [sections])

  // Scroll the active chip into view whenever it changes — but only if
  // it's not already fully visible. Without this guard, `scrollIntoView`
  // on a snap-x container realigns the chip to its snap point (respecting
  // the container's px-3 padding), which produces a phantom leftward
  // scroll on mount when the first chip is already at the start.
  useEffect(() => {
    const chip = activeRef.current
    const container = scrollRef.current
    if (!chip || !container) return
    const chipLeft = chip.offsetLeft - container.offsetLeft
    const chipRight = chipLeft + chip.offsetWidth
    const viewLeft = container.scrollLeft
    const viewRight = viewLeft + container.clientWidth
    if (chipLeft < viewLeft || chipRight > viewRight) {
      chip.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
        inline: 'nearest',
      })
    }
  }, [activeKey])

  if (sections.length === 0) {
    return (
      <div className="text-xs text-p-text-light px-3 py-2">
        No workspace scopes available.
      </div>
    )
  }
  return (
    <div className="relative">
      <div
        ref={scrollRef}
        // `scroll-pl-3 scroll-pr-3` moves the snap port inside the px-3
        // padding so the first chip's snap-start aligns at scrollLeft=0,
        // not scrollLeft=12. Without this, releasing a swipe at the far
        // left rebounds 12px to the right and exposes the left fade.
        className="flex gap-1.5 px-3 py-2 overflow-x-auto scrollbar-hide snap-x scroll-pl-3 scroll-pr-3"
        style={{ scrollBehavior: 'smooth' }}
      >
        {sections.map((s) => {
          const active = s.key === activeKey
          const purple = (
            s.key === 'agent-workspace'
            || s.key === 'agent-knowledge'
            || s.key === 'agent-config'
          )
          const className = purple
            ? active
              ? 'bg-p-accent-purple text-white border-p-accent-purple'
              : 'bg-p-accent-purple/10 text-p-accent-purple border-p-accent-purple/30 hover:bg-p-accent-purple/20'
            : active
              ? 'bg-brand text-white border-brand'
              : 'bg-brand/10 text-brand border-brand/30 hover:bg-brand/20'
          return (
            <button
              key={s.key}
              ref={active ? activeRef : undefined}
              onClick={() => onSelect(s)}
              className={`shrink-0 snap-start px-3 py-1 rounded-full border text-xs font-medium transition-colors whitespace-nowrap ${className}`}
            >
              {s.label}
            </button>
          )
        })}
      </div>
      {/* Fade gradients — only render the side that has overflow. Match
          the parent's bg-p-bg so the fade blends seamlessly. */}
      {fadeLeft && (
        <div className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 bg-linear-to-r from-p-bg to-transparent" />
      )}
      {fadeRight && (
        <div className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 bg-linear-to-l from-p-bg to-transparent" />
      )}
    </div>
  )
}
