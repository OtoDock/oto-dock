// The motion between a pop-up window over a terminal chat and its chip in
// the dock: a window folds into its chip, a pressed chip opens into its
// window, the chips slide when they reorder or the dock changes side. One
// chat page has one dock, so the registry is module state: the dock
// registers its chips here, the windows and the floating document pane look
// them up.
//
// The windows and the pane find a chip in the same commit that mounted it:
// AgentChat renders ChatSidePanels (the dock) before the chat's slot (the
// terminal's windows, the floating pane), and React attaches a subtree's
// refs before the next sibling's layout effects run.

import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { DURATION, EASE, canAnimate, useReducedMotion } from './motion'

export interface Box { left: number; top: number; width: number; height: number }

const boxOf = (el: Element): Box => {
  const r = el.getBoundingClientRect()
  return { left: r.left, top: r.top, width: r.width, height: r.height }
}
const usable = (b: Box | null): b is Box => !!b && b.width > 0 && b.height > 0

// ---- the dock's chips ------------------------------------------------------

const chips = new Map<string, HTMLElement>()

/** A ref for a chip's button under its key. */
export function chipRef(key: string) {
  return (el: HTMLElement | null) => {
    if (!el) return
    chips.set(key, el)
    return () => { if (chips.get(key) === el) chips.delete(key) }
  }
}

export function chipFor(key: string): HTMLElement | null {
  return chips.get(key) ?? null
}

/** The document pane's chip: the document on show, else the "+k" chip,
 * else the topmost document chip. */
export function paneChip(active: string | null): HTMLElement | null {
  const own = active ? chipFor(`doc:${active}`) : null
  if (own) return own
  const more = chipFor('doc:more')
  if (more) return more
  let best: HTMLElement | null = null
  for (const [key, el] of chips) {
    if (!key.startsWith('doc:')) continue
    if (!best || el.getBoundingClientRect().top < best.getBoundingClientRect().top) best = el
  }
  return best
}

// ---- notes: a pressed chip, the dock before it changes side ---------------

const NOTE_MS = 2000
const openings = new Map<string, { box: Box; at: number }>()
let dockMove: { boxes: Map<string, Box>; at: number } | null = null

/** A chip pressed open: its window (or the pane, key `pane`) opens from here. */
export function noteOpening(key: string, el: Element): void {
  openings.set(key, { box: boxOf(el), at: performance.now() })
}

/** The rectangle of a chip pressed within the last two seconds, once. */
export function takeOpening(key: string): Box | null {
  const note = openings.get(key)
  openings.delete(key)
  return note && performance.now() - note.at < NOTE_MS ? note.box : null
}

/** The chips' rectangles right before the dock moves to the other side. */
export function noteDockMove(boxes: Map<string, Box>): void {
  dockMove = { boxes, at: performance.now() }
}

/** Read by time, never taken: a StrictMode double mount reads it twice. */
export function dockMoveFrom(): Map<string, Box> | null {
  return dockMove && performance.now() - dockMove.at < NOTE_MS ? dockMove.boxes : null
}

/** Tests: forget every chip and note. */
export function resetFoldForTests(): void {
  chips.clear()
  openings.clear()
  dockMove = null
}

// ---- the motions -----------------------------------------------------------

// One motion per element: a new one cancels the one running.
const running = new WeakMap<Element, Animation>()

function run(el: Element, keyframes: Keyframe[], options: KeyframeAnimationOptions): Animation | null {
  stop(el)
  if (typeof el.animate !== 'function') return null
  const a = el.animate(keyframes, options)
  running.set(el, a)
  // A motion that holds its end state keeps its handle until stop() drops
  // it (a later motion would otherwise end on the held state). A cancel
  // rejects `finished`: both branches are consumed.
  const holds = options.fill === 'forwards' || options.fill === 'both'
  a.finished.then(() => { if (!holds && running.get(el) === a) running.delete(el) }, () => {})
  return a
}

/** Cancel the element's motion (a fold's held end state included). */
export function stop(el: Element): void {
  const a = running.get(el)
  if (!a) return
  running.delete(el)
  a.cancel()
}

/** The transform that puts `from` on `to`: one scale (the larger ratio, so
 * the content never squashes) about the centres. */
function onto(from: Box, to: Box): string {
  const s = Math.max(to.width / from.width, to.height / from.height)
  const dx = to.left + to.width / 2 - (from.left + from.width / 2)
  const dy = to.top + to.height / 2 - (from.top + from.height / 2)
  return `translate(${dx}px, ${dy}px) scale(${s})`
}

/** A window folds into its chip and ends there, held, transparent; the chip
 * fades in over the last part. False when either has no size. */
export function fold(el: HTMLElement, chip: HTMLElement, onDone: () => void): boolean {
  stop(el)
  const from = boxOf(el)
  const to = boxOf(chip)
  if (!usable(from) || !usable(to)) return false
  const a = run(el, [
    { transform: 'none', opacity: 1 },
    { opacity: 1, offset: 0.6 },
    { transform: onto(from, to), opacity: 0 },
  ], { duration: DURATION.slow, easing: EASE.standard, fill: 'forwards' })
  if (!a) return false
  a.onfinish = onDone
  run(chip, [{ opacity: 0 }, { opacity: 1 }], {
    duration: DURATION.slow * 0.4, delay: DURATION.slow * 0.6, easing: EASE.standard, fill: 'backwards',
  })
  return true
}

/** A window opens out of the chip's rectangle into its own; a window still
 * folding opens from where it is now. */
export function unfold(el: HTMLElement, chip: Box): boolean {
  const from = running.has(el) ? boxOf(el) : chip
  stop(el)
  const to = boxOf(el)
  if (!usable(from) || !usable(to)) return false
  return !!run(el, [
    { transform: onto(to, from), opacity: 0 },
    { opacity: 1, offset: 0.4 },
    { transform: 'none', opacity: 1 },
  ], { duration: DURATION.slow, easing: EASE.standard })
}

/** An element that moved slides from where it was. */
export function slide(el: HTMLElement, from: Box, duration: number): void {
  const to = boxOf(el)
  const dx = from.left - to.left
  const dy = from.top - to.top
  if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return
  run(el, [{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], { duration, easing: EASE.standard })
}

export { boxOf }

// ---- the floating document pane -------------------------------------------

/**
 * The floating document pane folds into its chip on a minimize and opens
 * from a pressed document chip. Returns whether the box is folding: it stays
 * visible (and inert) until it reaches the chip.
 */
export function usePaneFold(
  ref: RefObject<HTMLElement | null>,
  { folded, shown, active }: { folded: boolean; shown: boolean; active: string | null },
): boolean {
  const motion = canAnimate(useReducedMotion())
  const [prev, setPrev] = useState({ folded, shown })
  const [flying, setFlying] = useState(false)
  if (prev.folded !== folded || prev.shown !== shown) {
    setPrev({ folded, shown })
    if (motion && folded && prev.shown && !shown) setFlying(true)
    else if (flying && !folded) setFlying(false)
  }

  const wasShown = useRef(shown)
  const folding = useRef(false)
  useLayoutEffect(() => {
    const el = ref.current
    const justShown = shown && !wasShown.current
    wasShown.current = shown
    if (!el) return
    if (flying) {
      // Once per fold: a change of the document on show mid-flight keeps it.
      if (folding.current) return
      folding.current = true
      const land = () => setFlying(false)
      const go = (chip: HTMLElement | null) => !!chip && fold(el, chip, land)
      if (!go(paneChip(active))) requestAnimationFrame(() => { if (!go(paneChip(active))) land() })
      return
    }
    const wasFolding = folding.current
    folding.current = false
    if (justShown) {
      const from = takeOpening('pane')
      if (from && motion && unfold(el, from)) return
    }
    // A fold that landed holds its end state: drop it (also a fold cut short
    // by a push that shows the pane again).
    if (wasFolding || justShown) stop(el)
  }, [flying, shown, active, motion, ref])

  return flying
}
