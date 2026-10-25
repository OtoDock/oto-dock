import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { pushEscHandler } from '../../lib/escStack'
import { useBubbleAnchor } from './bubblePosition'
import { INFO_TIP_BUTTON, POPOVER } from './classes'
import { usePortalRoot } from './portalRoot'

/**
 * The info button beside a setting's label: a small round "i" that shows
 * the setting's longer description in a bubble, so the page shows the
 * labels and keeps the explanations one hover or tap away.
 *
 * Desktop: it opens after the Tooltip's hover delay, and on keyboard focus.
 * Touch: a tap opens it (a long press is undiscoverable on an info button).
 * A click, a tap, Enter or Space pins it open, whatever opened it; the
 * next one closes a pinned bubble, so a screen reader that focuses and then
 * activates the button never opens and closes it at once. A bubble pinned
 * from closed or from a hover also puts its text in a polite live region,
 * since a reader does not re-read a description that appears on the button
 * it is already on; one the focus opened was read with the focus already.
 * On a coarse pointer the button's hit area grows to 44 px (`INFO_TIP_BUTTON`),
 * so a near miss beside it never lands on a row's toggle.
 *
 * It closes on a pointer down outside it; on Escape (the escape stack, so
 * inside a dialog the bubble closes and the dialog stays, and the focus
 * goes back to the button unless the hover opened it); on a scroll outside
 * it (the scroll that brings a focused button into view moves the bubble
 * along instead); when the pointer leaves a bubble the hover opened (the
 * bubble itself counts, so its text can be read and selected); on blur
 * when the hover or the focus opened it; and, pinned, when the focus moves
 * to another control. One bubble shows at a time.
 *
 * The button is a sibling of the row's `<label>`, never inside it: a tap
 * on it must not toggle the checkbox, and a label's text would read the
 * button's name into the control's. Its 32 px box takes no extra line
 * height (negative margins), so it sits inline after the label's last
 * word. The bubble portals into the portal root (the body, or the dialog
 * it is in) and is placed like the Tooltip (`bubblePosition.ts`). A link
 * never goes in the bubble: Tab leaves the button for the next control.
 */
const HOVER_DELAY_MS = 300
const LEAVE_GRACE_MS = 120
// A focus scrolls its button into view a frame later.
const SCROLL_GRACE_MS = 200
const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'

type Mode = 'hover' | 'focus' | 'pinned'

// The open bubble's close, so opening another one closes it first.
let closeOpenTip: (() => void) | null = null

export default function InfoTip({ label, children }: {
  /** The setting's label, for the button's name: "About <label>". */
  label: string
  /** The longer description the bubble shows. */
  children: ReactNode
}) {
  const tipId = useId()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const bubbleRef = useRef<HTMLDivElement>(null)
  const hoverTimer = useRef<number | null>(null)
  const leaveTimer = useRef<number | null>(null)
  // A press is under way: the focus it brings is not a keyboard's.
  const pressRef = useRef(false)
  // The focus Escape gives back must not reopen the bubble.
  const refocusRef = useRef(false)
  const openedAtRef = useRef(0)
  const [mode, setMode] = useState<Mode | null>(null)
  // The pinned text goes to the live region: not when the focus opened it.
  const [announce, setAnnounce] = useState(false)
  const { anchor, showAt, hide, place } = useBubbleAnchor()
  const portalRoot = usePortalRoot()
  const open = mode !== null && anchor !== null

  const clearTimers = useCallback(() => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current)
    if (leaveTimer.current !== null) window.clearTimeout(leaveTimer.current)
    hoverTimer.current = null
    leaveTimer.current = null
  }, [])

  const close = useCallback(() => {
    clearTimers()
    setMode(null)
    setAnnounce(false)
    hide()
  }, [clearTimers, hide])

  const openAs = useCallback((next: Mode) => {
    const el = buttonRef.current
    if (!el) return
    clearTimers()
    if (closeOpenTip && closeOpenTip !== close) closeOpenTip()
    closeOpenTip = close
    openedAtRef.current = Date.now()
    setMode(next)
    setAnnounce(next === 'pinned')
    showAt(el)
  }, [clearTimers, close, showAt])

  const leaveSoon = useCallback(() => {
    if (leaveTimer.current !== null) window.clearTimeout(leaveTimer.current)
    leaveTimer.current = window.setTimeout(() => {
      leaveTimer.current = null
      close()
    }, LEAVE_GRACE_MS)
  }, [close])

  const stayOpen = useCallback(() => {
    if (leaveTimer.current !== null) window.clearTimeout(leaveTimer.current)
    leaveTimer.current = null
  }, [])

  const onPointerEnter = (e: React.PointerEvent) => {
    if (e.pointerType === 'touch' || e.pointerType === 'pen') return
    if (mode !== null) {
      stayOpen()
      return
    }
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = window.setTimeout(() => {
      hoverTimer.current = null
      openAs('hover')
    }, HOVER_DELAY_MS)
  }

  const onPointerLeave = (e: React.PointerEvent) => {
    if (e.pointerType === 'touch' || e.pointerType === 'pen') return
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current)
      hoverTimer.current = null
    }
    if (mode === 'hover') leaveSoon()
  }

  const onFocus = () => {
    const pressed = pressRef.current
    pressRef.current = false
    if (refocusRef.current) {
      refocusRef.current = false
      return
    }
    if (pressed) return
    if (mode === 'hover') {
      stayOpen()
      setMode('focus')
    } else if (mode === null) {
      openAs('focus')
    }
  }

  const onBlur = (e: React.FocusEvent) => {
    const to = e.relatedTarget as Element | null
    if (to && bubbleRef.current?.contains(to)) return
    if (mode === 'hover' || mode === 'focus') close()
    // A press on the bubble's text may focus the dialog it sits in: that is
    // not the focus moving on.
    else if (mode === 'pinned' && to?.matches(FOCUSABLE) && to.tagName !== 'DIALOG') close()
  }

  const onClick = () => {
    pressRef.current = false
    if (mode === 'pinned') close()
    else if (mode !== null) {
      stayOpen()
      setAnnounce(mode === 'hover')
      setMode('pinned')
    } else openAs('pinned')
  }

  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => {
      const button = buttonRef.current
      if (e.target instanceof Node && bubbleRef.current?.contains(e.target)) return
      if (button && Date.now() - openedAtRef.current <= SCROLL_GRACE_MS) showAt(button)
      else close()
    }
    // Bubbling, so a dialog reads its backdrop press while the bubble's
    // escape entry still stands: that press closes the bubble alone.
    const onDocPointerDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (buttonRef.current?.contains(t) || bubbleRef.current?.contains(t)) return
      close()
    }
    const onEscape = () => {
      const button = buttonRef.current
      const active = document.activeElement
      // A press on the bubble's text may have moved the focus to the body or
      // to the dialog that holds the button: it was still here.
      const focusWasHere = active === button || !!bubbleRef.current?.contains(active)
        || (mode !== 'hover' && (active === document.body
          || (active?.tagName === 'DIALOG' && !!button && active.contains(button))))
      close()
      if (button && focusWasHere && active !== button) {
        refocusRef.current = true
        button.focus()
        refocusRef.current = false
      }
    }
    window.addEventListener('scroll', onScroll, true)
    document.addEventListener('pointerdown', onDocPointerDown)
    const popEsc = pushEscHandler(onEscape, () => buttonRef.current)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      document.removeEventListener('pointerdown', onDocPointerDown)
      popEsc()
    }
  }, [open, mode, close, showAt])

  useEffect(() => () => {
    clearTimers()
    if (closeOpenTip === close) closeOpenTip = null
  }, [clearTimers, close])

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        aria-label={`About ${label}`}
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        className={INFO_TIP_BUTTON}
        onPointerDown={() => { pressRef.current = true }}
        onPointerUp={() => { window.setTimeout(() => { pressRef.current = false }, 0) }}
        onPointerCancel={() => { pressRef.current = false }}
        onPointerEnter={onPointerEnter}
        onPointerLeave={onPointerLeave}
        onFocus={onFocus}
        onBlur={onBlur}
        onClick={onClick}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 11v5" />
          <path d="M12 8h.01" />
        </svg>
      </button>
      <span role="status" className="sr-only">{open && announce ? children : null}</span>
      {open && createPortal(
        <div
          id={tipId}
          ref={(el) => {
            bubbleRef.current = el
            place(el)
          }}
          role="tooltip"
          className={`fixed z-[70] max-h-[calc(100dvh-16px)] max-w-[min(320px,calc(100vw-16px))] overflow-y-auto whitespace-normal break-words p-3 text-left text-sm font-normal leading-normal ${POPOVER}`}
          onPointerEnter={(e) => { if (e.pointerType === 'mouse') stayOpen() }}
          onPointerLeave={(e) => { if (e.pointerType === 'mouse' && mode === 'hover') leaveSoon() }}
        >
          {children}
        </div>,
        portalRoot,
      )}
    </>
  )
}
