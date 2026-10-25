import { useCallback, useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useLongPress } from '../../hooks/useLongPress'
import { pushEscHandler } from '../../lib/escStack'
import { useBubbleAnchor } from './bubblePosition'
import { TOOLTIP } from './classes'
import { usePortalRoot } from './portalRoot'

/**
 * A text bubble for what a label cannot show: the sidebar's truncated chat titles (`onlyWhenClipped`:
 * only when `scrollWidth > clientWidth` at the moment it would show) and an
 * icon button's name.
 *
 * Desktop: opens after a short hover delay, and while the focus is inside
 * the trigger. Touch: opens on a long press (the platform's 500 ms with
 * movement cancelling), and the click that follows the press is swallowed
 * so a row does not also navigate. It closes on leave, blur, scroll (capture:
 * the history list is an inner scroller), any outside touch, and Escape
 * (WCAG 1.4.13: dismissable without moving the pointer). The trigger's first
 * focusable element is described by the bubble while it shows.
 *
 * The bubble portals into the portal root (the body, or the dialog it is
 * in): the mobile drawer slides with a CSS translate, which makes any
 * transformed ancestor the containing block for `position: fixed`, and the
 * desktop sidebar's overflow would clip it. Placed by `bubblePosition.ts`
 * (the InfoTip's rule too): above the trigger, clamped to the viewport,
 * below it when there is no room above.
 */
const HOVER_DELAY_MS = 300
const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'

export default function Tooltip({ text, className = '', onlyWhenClipped = false, children }: {
  /** The FULL plain-text label the bubble shows (never JSX). */
  text: string
  /** Classes for the trigger span; with `onlyWhenClipped` they must carry
      the truncation ("truncate" / "block truncate") the check measures. */
  className?: string
  onlyWhenClipped?: boolean
  children: ReactNode
}) {
  const tipId = useId()
  const triggerRef = useRef<HTMLSpanElement>(null)
  const hoverTimer = useRef<number | null>(null)
  const { anchor, showAt, hide, place } = useBubbleAnchor()
  const portalRoot = usePortalRoot()
  const open = anchor !== null

  const close = useCallback(() => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = null
    }
    hide()
  }, [hide])

  const show = useCallback(() => {
    const el = triggerRef.current
    if (!el || !text) return
    if (onlyWhenClipped && el.scrollWidth <= el.clientWidth) return
    showAt(el)
  }, [text, onlyWhenClipped, showAt])

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
    const onScroll = () => close()
    const onDocTouch = (e: TouchEvent) => {
      if (triggerRef.current && !triggerRef.current.contains(e.target as Node)) close()
    }
    window.addEventListener('scroll', onScroll, true)
    document.addEventListener('touchstart', onDocTouch)
    const popEsc = pushEscHandler(close, () => triggerRef.current)
    // The trigger's control is what a screen reader reads the bubble for,
    // unless its own label already says the same words.
    const control = triggerRef.current?.querySelector<HTMLElement>(FOCUSABLE) ?? null
    const described = control?.getAttribute('aria-label') === text ? null : control
    described?.setAttribute('aria-describedby', tipId)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      document.removeEventListener('touchstart', onDocTouch)
      popEsc()
      if (described?.getAttribute('aria-describedby') === tipId) described.removeAttribute('aria-describedby')
    }
  }, [open, close, tipId, text])

  useEffect(() => () => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current)
  }, [])

  return (
    <>
      <span
        ref={triggerRef}
        className={className}
        // Static suppression (state-toggled -webkit- props race on older
        // Safari): the label is not for selecting, and the callout would
        // fight the long-press bubble.
        style={{ WebkitUserSelect: 'none', userSelect: 'none', WebkitTouchCallout: 'none' } as React.CSSProperties}
        onMouseEnter={onMouseEnter}
        onMouseLeave={close}
        onFocus={show}
        onBlur={close}
        onTouchStart={longPress.onTouchStart}
        onTouchMove={longPress.onTouchMove}
        onTouchEnd={longPress.onTouchEnd}
        onTouchCancel={longPress.onTouchCancel}
        onPointerDown={longPress.onPointerDown}
        onClickCapture={longPress.onClickCapture}
        onContextMenu={longPress.onContextMenu}
      >
        {children}
      </span>
      {open && createPortal(
        <div
          id={tipId}
          role="tooltip"
          className={`fixed z-[70] max-w-[min(320px,calc(100vw-16px))] pointer-events-none break-words ${TOOLTIP}`}
          ref={place}
        >
          {text}
        </div>,
        portalRoot,
      )}
    </>
  )
}
