import { useEffect, useMemo, useRef } from 'react'
import type { FocusEvent as ReactFocusEvent, RefObject } from 'react'

/** Rows the composer grows to while it holds the focus, and shrinks to when
 * the focus leaves it (its text scrolls inside). */
export const EXPANDED_ROWS = 10
export const COLLAPSED_ROWS = 4
export const HEIGHT_TRANSITION = 'height 250ms ease-out'
/** A press counts for the focus change that follows it this long: on touch
 * the focus moves after `pointerup`, from the compatibility mousedown. */
const PRESS_FRESH_MS = 1000

/** Height of `rows` lines of the box, from its computed line height (24 px
 * lines on a touch screen, 20 px on a desktop) and padding. */
export function rowsPx(ta: HTMLTextAreaElement, rows: number): number {
  const cs = getComputedStyle(ta)
  const line = parseFloat(cs.lineHeight)
  const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
  return rows * (Number.isFinite(line) ? line : 20) + (Number.isFinite(pad) ? pad : 16)
}

function reducedMotion(): boolean {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

const isFrame = (n: unknown): boolean => n instanceof HTMLIFrameElement

type Press = { inside: boolean; onBox: boolean; down: boolean; at: number }

/**
 * The composer's height: content height up to EXPANDED_ROWS while focused,
 * COLLAPSED_ROWS once the focus leaves it, animated (px to px) between the
 * two. What counts as leaving, and when the change runs:
 *
 * - Focus moving to a control of the composer's own bar (the region marked
 *   `data-composer-bar`: Send, Dictate, the pickers, the chips) or into an
 *   iframe (an artifact, an app, Collabora) keeps it open, until a later
 *   press or focus elsewhere in the page.
 * - A press elsewhere shrinks it on the task after its `pointerup`, so the
 *   click that release produces lands before the history moves under it; a
 *   `pointercancel` (a scroll, a drag) shrinks nothing.
 * - A press on the collapsed box grows it the same way, after the caret has
 *   landed; on touch the focus arrives after the release and it grows at once.
 * - The window losing focus changes nothing.
 *
 * Every other height write (typing, dictation, a draft restored, a send) is
 * instant: `fit` clears the transition first, so the first paint and typing
 * never animate.
 */
export function useComposerHeight(
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  rootRef: RefObject<HTMLElement | null>,
  opts: { animate: boolean; holdOpen: boolean },
) {
  const expandedRef = useRef(false)
  const optsRef = useRef(opts)
  optsRef.current = opts
  const pressRef = useRef<Press | null>(null)
  const pendingRef = useRef<'shrink' | 'grow' | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const api = useMemo(() => {
    const region = (): HTMLElement | null => {
      const root = rootRef.current
      return (root?.closest('[data-composer-bar]') as HTMLElement | null) ?? root
    }
    const inRegion = (n: unknown): boolean => {
      const r = region()
      return !!r && n instanceof Node && r.contains(n)
    }
    const focused = () => !!textareaRef.current && document.activeElement === textareaRef.current
    const freshPress = () => {
      const p = pressRef.current
      return p && Date.now() - p.at < PRESS_FRESH_MS ? p : null
    }
    const later = (fn: () => void) => {
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = setTimeout(() => { timerRef.current = null; fn() }, 0)
    }

    const fit = () => {
      const ta = textareaRef.current
      if (!ta) return
      ta.style.transition = ''
      ta.style.height = 'auto'
      const cap = rowsPx(ta, expandedRef.current ? EXPANDED_ROWS : COLLAPSED_ROWS)
      ta.style.height = Math.min(ta.scrollHeight, cap) + 'px'
    }

    const setExpanded = (next: boolean) => {
      const ta = textareaRef.current
      if (expandedRef.current === next || !ta) { expandedRef.current = next; return }
      expandedRef.current = next
      const current = parseFloat(ta.style.height) || ta.offsetHeight
      // A collapsed box never exceeds its text, so scrollHeight is the whole
      // text without an `auto` reset that would cancel the animation.
      const target = next
        ? Math.min(ta.scrollHeight, rowsPx(ta, EXPANDED_ROWS))
        : Math.min(current, rowsPx(ta, COLLAPSED_ROWS))
      if (Math.abs(target - current) < 0.5) return
      ta.style.transition = optsRef.current.animate && !reducedMotion() ? HEIGHT_TRANSITION : ''
      ta.style.height = target + 'px'
    }
    const grow = () => setExpanded(true)
    const shrink = () => {
      if (focused() || optsRef.current.holdOpen) return
      setExpanded(false)
    }

    return {
      fit,
      grow,
      inRegion,
      onFocus: () => {
        const p = pressRef.current
        if (p?.down && p.onBox) { pendingRef.current = 'grow'; return }
        grow()
      },
      onBlur: (e: ReactFocusEvent<HTMLTextAreaElement>) => {
        if (pendingRef.current === 'grow') pendingRef.current = null
        const to = e.relatedTarget
        if (to && (inRegion(to) || isFrame(to))) return
        // A press on a control of the bar keeps it open; a press on the box
        // itself does not (the iOS keyboard's Done right after a tap).
        const p = freshPress()
        if (p?.inside && !p.onBox) return
        if (p?.down) { pendingRef.current = 'shrink'; return }
        later(() => {
          const a = document.activeElement
          if (a === textareaRef.current || isFrame(a) || inRegion(a)) return
          shrink()
        })
      },
      /** A keystroke or an input in the box ends a grow still waiting on a
       * release the page never saw. */
      settle: () => {
        if (pendingRef.current === 'grow') { pendingRef.current = null; grow() }
      },
      onDown: (e: PointerEvent) => {
        const inside = inRegion(e.target)
        pressRef.current = { inside, onBox: e.target === textareaRef.current, down: true, at: Date.now() }
        if (expandedRef.current && !focused() && !inside && !isFrame(e.target)) pendingRef.current = 'shrink'
      },
      onUp: () => {
        if (pressRef.current) pressRef.current.down = false
        const pending = pendingRef.current
        pendingRef.current = null
        if (pending === 'shrink') later(shrink)
        if (pending === 'grow') later(grow)
      },
      onCancel: () => {
        if (pressRef.current) pressRef.current.down = false
        const pending = pendingRef.current
        pendingRef.current = null
        if (pending === 'grow') grow()
      },
      onFocusIn: (e: FocusEvent) => {
        const t = e.target
        if (!expandedRef.current || t === textareaRef.current || pressRef.current?.down) return
        if (isFrame(t) || inRegion(t)) return
        shrink()
      },
      onWindowBlur: () => {
        if (pressRef.current) pressRef.current.down = false
        if (pendingRef.current === 'grow') grow()
        pendingRef.current = null
      },
    }
  }, [textareaRef, rootRef])

  useEffect(() => {
    const { onDown, onUp, onCancel, onFocusIn, onWindowBlur } = api
    document.addEventListener('pointerdown', onDown, true)
    window.addEventListener('pointerup', onUp, true)
    window.addEventListener('pointercancel', onCancel, true)
    document.addEventListener('dragend', onCancel, true)
    document.addEventListener('focusin', onFocusIn, true)
    window.addEventListener('blur', onWindowBlur)
    return () => {
      document.removeEventListener('pointerdown', onDown, true)
      window.removeEventListener('pointerup', onUp, true)
      window.removeEventListener('pointercancel', onCancel, true)
      document.removeEventListener('dragend', onCancel, true)
      document.removeEventListener('focusin', onFocusIn, true)
      window.removeEventListener('blur', onWindowBlur)
      if (timerRef.current) clearTimeout(timerRef.current)
      timerRef.current = null
      pendingRef.current = null
    }
  }, [api])

  return api
}
