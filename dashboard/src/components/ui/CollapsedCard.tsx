import { useEffect, useId, useRef, type ReactNode } from 'react'
import { CARD } from './classes'

// How long an opened card is kept in view while its body fills in.
const REVEAL_WINDOW_MS = 1500
// A person's own scrolling or pressing: a wheel, a finger or the scroll bar,
// a key. The press that opened the card came before the card listens.
const PERSON_INPUTS = ['wheel', 'pointerdown', 'keydown'] as const

type RootProps = { [key: `data-${string}`]: string | undefined }

interface Props {
  open: boolean
  onToggle: () => void
  /** In the button, before the chevron. */
  header: ReactNode
  /** In the button under the header, shown only while the card is closed. */
  preview?: ReactNode
  /** Outside the button, between it and the body: what a person must reach
   *  while the card is closed (a file chip, a link). A click on it never
   *  toggles the card. */
  aside?: ReactNode
  /** The body, rendered only while the card is open. */
  children: ReactNode
  /** The button's padding. */
  buttonClassName?: string
  bodyClassName?: string
  /** For a list with one card open at a time: opening a card below an open
   *  one closes that one and moves this one up, off a phone's screen. When
   *  this card opens (or mounts open), it scrolls into view: nothing moves
   *  when it fits on the screen, a taller card shows from its top. */
  keepInView?: boolean
  /** The frame's data attributes (a test id). */
  rootProps?: RootProps
}

/**
 * The design system's collapsed card: the header and a preview in ONE
 * native button (a click, Enter or Space), a chevron rotating 90°, the body
 * under it named by the button's `aria-controls`, and anything a person must
 * reach outside the button. The focus stays on the button when it opens.
 */
export default function CollapsedCard({
  open, onToggle, header, preview, aside, children,
  buttonClassName = 'px-3 py-2', bodyClassName, keepInView = false, rootProps,
}: Props) {
  const bodyId = useId()
  const frameRef = useRef<HTMLDivElement>(null)

  // One frame late: a page that resets its scroll on arrival (the settings
  // shell, coming back from a conversation) does so in an effect that runs
  // after this one. A body that fills in later (a load) grows the card, so it
  // is kept in view while it grows in its first moments, until the person
  // scrolls, presses or types: then the page is theirs.
  useEffect(() => {
    const frame = frameRef.current
    if (!open || !keepInView || !frame) return
    const reveal = () => frame.scrollIntoView?.({ block: 'nearest' })
    const step = requestAnimationFrame(reveal)
    const until = performance.now() + REVEAL_WINDOW_MS
    const growth = typeof ResizeObserver === 'undefined'
      ? null
      : new ResizeObserver(() => { if (performance.now() < until) reveal() })
    growth?.observe(frame)
    const yieldToPerson = () => {
      cancelAnimationFrame(step)
      growth?.disconnect()
    }
    for (const kind of PERSON_INPUTS) window.addEventListener(kind, yieldToPerson, { passive: true, once: true })
    const stop = setTimeout(() => growth?.disconnect(), REVEAL_WINDOW_MS)
    return () => {
      cancelAnimationFrame(step)
      clearTimeout(stop)
      growth?.disconnect()
      for (const kind of PERSON_INPUTS) window.removeEventListener(kind, yieldToPerson)
    }
  }, [open, keepInView])

  return (
    <div ref={frameRef} {...rootProps} className={CARD}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={bodyId}
        className={`w-full text-left rounded-control ${buttonClassName} hover:bg-surface-hover transition-colors
                   focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus-ring/60`}
      >
        <span className="flex items-center gap-2 min-w-0">
          {header}
          <svg
            className={`ml-auto w-4 h-4 shrink-0 text-tertiary transition-transform ${open ? 'rotate-90' : ''}`}
            fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
          </svg>
        </span>
        {!open && preview}
      </button>
      {aside}
      <div id={bodyId} hidden={!open} className={bodyClassName}>
        {open ? children : null}
      </div>
    </div>
  )
}
