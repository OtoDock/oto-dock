import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useLongPress } from '../../hooks/useLongPress'

/**
 * Full-text tooltip for truncated labels (sidebar chat/task titles).
 *
 * Shows ONLY when the label is actually clipped (`scrollWidth > clientWidth`
 * measured at trigger time). Desktop: opens after a short hover delay.
 * Mobile: opens on long-press (500ms, movement-cancelled — the platform's
 * FileTile/FileTree constants) with the follow-up click swallowed so the row
 * doesn't also navigate.
 *
 * The bubble PORTALS to <body>: the mobile drawer slides with a CSS
 * translate, which makes any transformed ancestor the containing block for
 * `position: fixed` — an inline fixed bubble (the RemoteBadge idiom this
 * derives from) would mis-position inside it, and the desktop sidebar's
 * overflow-hidden would clip it. Positioned above the label, clamped to the
 * viewport with a margin, flipping below when there is no room above.
 * Closes on leave, scroll (capture — the history list is an inner scroller
 * whose scroll never bubbles), any outside touch, and Escape-free (no esc
 * stack entry: it is transient hover UI, not a modal).
 */
const HOVER_DELAY_MS = 300
const EDGE_MARGIN_PX = 8

export default function TitleTooltip({ text, className = '', children }: {
  /** The FULL plain-text label the bubble shows (never JSX). */
  text: string
  /** Classes for the measuring span — must include the truncation styling
      (e.g. "truncate" / "block truncate") so clip detection works. */
  className?: string
  children: ReactNode
}) {
  const labelRef = useRef<HTMLSpanElement>(null)
  const hoverTimer = useRef<number | null>(null)
  const [pos, setPos] = useState<{ x: number; y: number; below: boolean } | null>(null)
  const open = pos !== null

  const close = useCallback(() => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = null
    }
    setPos(null)
  }, [])

  const show = useCallback(() => {
    const el = labelRef.current
    if (!el || !text) return
    // Only when actually clipped — an untruncated label needs no tooltip.
    if (el.scrollWidth <= el.clientWidth) return
    const r = el.getBoundingClientRect()
    const below = r.top < 48 // no room above → flip under the label
    setPos({
      x: r.left + r.width / 2,
      y: below ? r.bottom + 6 : r.top - 6,
      below,
    })
  }, [text])

  const onMouseEnter = useCallback(() => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = window.setTimeout(() => {
      hoverTimer.current = null
      show()
    }, HOVER_DELAY_MS)
  }, [show])

  const longPress = useLongPress(show)

  useEffect(() => {
    if (!open) return
    // Capture phase: the sidebar list scrolls in its own overflow container,
    // whose scroll events never bubble to window.
    const onScroll = () => close()
    const onDocTouch = (e: TouchEvent) => {
      if (labelRef.current && !labelRef.current.contains(e.target as Node)) close()
    }
    window.addEventListener('scroll', onScroll, true)
    document.addEventListener('touchstart', onDocTouch)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      document.removeEventListener('touchstart', onDocTouch)
    }
  }, [open, close])

  useEffect(() => () => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current)
  }, [])

  return (
    <>
      <span
        ref={labelRef}
        className={className}
        // Static suppression (state-toggled -webkit- props race on older
        // Safari): titles are non-selectable labels, selecting them has no
        // value, and the callout would fight the long-press tooltip.
        style={{ WebkitUserSelect: 'none', userSelect: 'none', WebkitTouchCallout: 'none' } as React.CSSProperties}
        onMouseEnter={onMouseEnter}
        onMouseLeave={close}
        onTouchStart={longPress.onTouchStart}
        onTouchMove={longPress.onTouchMove}
        onTouchEnd={longPress.onTouchEnd}
        onClickCapture={longPress.onClickCapture}
        onContextMenu={longPress.onContextMenu}
      >
        {children}
      </span>
      {open && createPortal(
        <div
          role="tooltip"
          className="fixed z-[70] max-w-[min(320px,calc(100vw-16px))] px-2.5 py-1.5 rounded-lg
                     bg-p-text text-p-bg text-xs leading-snug shadow-lg pointer-events-none
                     break-words"
          ref={(el) => {
            // Clamp AFTER first paint (width known): keep the bubble fully
            // on-screen — the sidebar hugs the left edge, so unclamped
            // centering would push it off-viewport.
            if (!el || !pos) return
            const w = el.offsetWidth
            let left = pos.x - w / 2
            left = Math.max(EDGE_MARGIN_PX, Math.min(left, window.innerWidth - w - EDGE_MARGIN_PX))
            el.style.left = `${left}px`
            if (pos.below) {
              el.style.top = `${Math.min(pos.y, window.innerHeight - el.offsetHeight - EDGE_MARGIN_PX)}px`
            } else {
              el.style.top = `${Math.max(EDGE_MARGIN_PX, pos.y - el.offsetHeight)}px`
            }
          }}
        >
          {text}
        </div>,
        document.body,
      )}
    </>
  )
}
