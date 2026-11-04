import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { arrangeChips, windowChipKey, type ArtifactWindow } from '@/hooks/useArtifactWindows'
import { DOCK_SIDE, otherSide, setDockSide, type DockSide } from '@/lib/ui/dockSide'
import type { MessageBlock } from '../types'
import { GLASS_FILL, GLASS_HOVER } from '../../ui/glass'
import { boxOf, chipRef, dockMoveFrom, noteDockMove, noteOpening, slide, type Box } from './fold'
import { DURATION, EASE, canAnimate, useReducedMotion } from './motion'

/**
 * Minimized interactive-CLI artifact windows (and the minimized document
 * pane's chips), docked as icon-buttons in one of the page's two panel stacks
 * — the same idiom as PlanPanel / TodoPanel / WorkflowPanel (and
 * TaskMetadata), appearing BELOW them. The chip is the button: its type glyph
 * on an accent border, a press restores the floating window (it opens out of
 * the chip, fold.ts); the red × at the outer top corner dismisses it, on
 * every device. A chip drags to reorder the dock (a mouse at once, a finger
 * after a hold); released past the middle of the chat it moves the dock to
 * the other side, remembered per device (lib/ui/dockSide). Rendered by the
 * page (ChatSidePanels), driven by the lifted `useArtifactWindows` state.
 */
interface Props {
  windows: ArtifactWindow[]
  minimized: Set<number>
  onRestore: (id: number) => void
  onClose: (id: number) => void
  /** The minimized document pane: one chip per open document (the first
   * four, then a chip for the rest); a chip restores the pane on its file,
   * its X closes that document. */
  documents?: {
    chips: { fileId: string; title: string }[]
    more: number
    onRestore: (fileId?: string) => void
    onClose: (fileId: string) => void
  } | null
  /** The side of the chat the dock sits on. */
  side?: DockSide
  /** The chips' order a drag left (keys of `windowChipKey` and `doc:<fileId>`);
   * chips it does not name follow in their arrival order. */
  order?: string[]
  onReorder?: (keys: string[]) => void
  /** A drag lifts the dock's stack above the windows while it runs. */
  onLift?: (lifted: boolean) => void
}

interface Chip {
  key: string
  type: MessageBlock['type']
  title: string
  label?: string
  restore: () => void
  close?: () => void
  /** The fold registry's key of what the chip opens. */
  opens: string
}

// A finger drags after a hold, so a tap still opens and a swipe is not a drag.
const HOLD_MS = 350
const HOLD_SLOP = 10
const MOUSE_SLOP = 4
// The click a drag's release sends is swallowed for this long.
const SWALLOW_MS = 400

// The cards' glyphs, by the block's type: a card with its buttons, a live
// list, a setting's control.
const CARD_GLYPHS: Partial<Record<MessageBlock['type'], ReactNode>> = {
  card: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path strokeLinecap="round" d="M7 9h10" />
      <rect x="7" y="13" width="4.5" height="3" rx="1.5" />
      <rect x="13" y="13" width="4.5" height="3" rx="1.5" />
    </>
  ),
  list: (
    <>
      <path strokeLinecap="round" d="M9 7h11M9 12h11M9 17h11" />
      <circle cx="5" cy="7" r="1" />
      <circle cx="5" cy="12" r="1" />
      <circle cx="5" cy="17" r="1" />
    </>
  ),
  setting: (
    <>
      <path strokeLinecap="round" d="M4 8h9M17 8h3M4 16h3M11 16h9" />
      <circle cx="15" cy="8" r="2" />
      <circle cx="9" cy="16" r="2" />
    </>
  ),
}

function TypeIcon({ type }: { type: MessageBlock['type'] }) {
  const cls = 'w-5 h-5 text-secondary'
  const card = CARD_GLYPHS[type]
  if (card) {
    return <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>{card}</svg>
  }
  switch (type) {
    case 'images':
    case 'image_generating':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <rect x="3" y="4" width="18" height="16" rx="2" />
          <circle cx="8.5" cy="9.5" r="1.5" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M21 16l-5-5L5 20" />
        </svg>
      )
    case 'video':
    case 'media_processing':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <rect x="3" y="5" width="18" height="14" rx="2" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M10 9l5 3-5 3V9z" />
        </svg>
      )
    case 'audio':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M11 5L6 9H3v6h3l5 4V5z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M16 9a3 3 0 010 6M18.5 7a6 6 0 010 10" />
        </svg>
      )
    case 'url':
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5a4 4 0 015.66 0l.34.34a4 4 0 010 5.66l-2.5 2.5a4 4 0 01-5.66 0M10.5 13.5a4 4 0 01-5.66 0l-.34-.34a4 4 0 010-5.66l2.5-2.5a4 4 0 015.66 0" />
        </svg>
      )
    default: // file, document_preview
      return (
        <svg className={cls} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M14 3v5h5M9 13h6M9 17h6" />
        </svg>
      )
  }
}

interface Drag {
  key: string
  pointerId: number
  touch: boolean
  x0: number
  y0: number
  /** The pointer's offset inside the chip at the press. */
  gx: number
  gy: number
  x: number
  y: number
  active: boolean
  timer: ReturnType<typeof setTimeout> | null
  button: HTMLElement
  /** The chips' slots when the drag started, in the dock's order. */
  slots: Box[]
  /** Each chip's slot index at the start. */
  home: Map<string, number>
  /** The order the drag shows. */
  order: string[]
  past: boolean
}

export default function ArtifactDock({ windows, minimized, onRestore, onClose, documents, side = DOCK_SIDE.LEFT, order, onReorder, onLift }: Props) {
  const motion = canAnimate(useReducedMotion())
  const right = side === DOCK_SIDE.RIGHT
  const rootRef = useRef<HTMLDivElement | null>(null)
  const wraps = useRef(new Map<string, HTMLDivElement>())
  const drag = useRef<Drag | null>(null)
  const swallowUntil = useRef(0)
  const stopTouchEnd = useRef(false)
  const settle = useRef<{ key: string; from: Box | undefined } | null>(null)
  const [lifted, setLifted] = useState<string | null>(null)
  const [past, setPast] = useState(false)
  const onLiftRef = useRef(onLift)
  useLayoutEffect(() => { onLiftRef.current = onLift })

  const chips: Chip[] = []
  for (const d of documents?.chips ?? []) {
    chips.push({
      key: `doc:${d.fileId}`, type: 'document_preview', title: d.title, opens: 'pane',
      restore: () => documents!.onRestore(d.fileId), close: () => documents!.onClose(d.fileId),
    })
  }
  if (documents?.more) {
    chips.push({
      key: 'doc:more', type: 'document_preview', title: `${documents.more} more documents`,
      label: `+${documents.more}`, opens: 'pane', restore: () => documents.onRestore(),
    })
  }
  for (const w of windows) {
    if (!minimized.has(w.id)) continue
    const key = windowChipKey(w.id)
    chips.push({ key, type: w.block.type, title: w.title, opens: key, restore: () => onRestore(w.id), close: () => onClose(w.id) })
  }
  const shown = arrangeChips(chips, order)
  const keys = shown.map((c) => c.key)
  const hasChips = shown.length > 0
  // A dragged chip that left the dock (its window opened, its document
  // closed, the pane re-shown by a push) ends its drag: the lift goes here,
  // the drag's own state in the effect below.
  if (lifted !== null && !keys.includes(lifted)) {
    setLifted(null)
    setPast(false)
  }

  // The captured button left the DOM, so no pointer event of it comes again
  // (its lost capture fires at the document, where React does not listen).
  useLayoutEffect(() => {
    const d = drag.current
    if (!d || keys.includes(d.key)) return
    if (d.timer) clearTimeout(d.timer)
    drag.current = null
    stopTouchEnd.current = false
    for (const wrap of wraps.current.values()) {
      wrap.style.transition = ''
      wrap.style.transform = ''
    }
    if (d.active) onLiftRef.current?.(false)
  })

  // A dock that unmounts mid-drag (the workspace overlay opens, another tab
  // moves the dock) drops the stack's lift with it.
  useEffect(() => () => {
    const d = drag.current
    if (d?.timer) clearTimeout(d.timer)
    if (d?.active) onLiftRef.current?.(false)
  }, [])

  // After a drop the order is the DOM's again: the preview's shifts go, and
  // the dropped chip slides from where it was let go into its slot.
  useLayoutEffect(() => {
    const done = settle.current
    if (!done) return
    settle.current = null
    for (const wrap of wraps.current.values()) {
      wrap.style.transition = ''
      wrap.style.transform = ''
    }
    const wrap = wraps.current.get(done.key)
    if (wrap && done.from && motion) slide(wrap, done.from, DURATION.fast)
  })

  // The dock that mounts on the other side slides each chip from where it was.
  useLayoutEffect(() => {
    const from = dockMoveFrom()
    if (!from || !motion) return
    for (const [key, box] of from) {
      const wrap = wraps.current.get(key)
      if (wrap) slide(wrap, box, DURATION.base)
    }
  }, [motion])

  // The chat page's drawer swipe listens on an ancestor before React sees the
  // touch: the touch that ends a chip drag stops here, so it never opens the
  // history drawer.
  useEffect(() => {
    const root = rootRef.current
    if (!root) return
    const onTouchEnd = (e: TouchEvent) => {
      if (!stopTouchEnd.current) return
      stopTouchEnd.current = false
      e.stopPropagation()
    }
    root.addEventListener('touchend', onTouchEnd)
    return () => root.removeEventListener('touchend', onTouchEnd)
  }, [hasChips])

  const boxesNow = (): Map<string, Box> => {
    const boxes = new Map<string, Box>()
    for (const [key, el] of wraps.current) boxes.set(key, boxOf(el))
    return boxes
  }

  // The middle of the chat column (the stacks' containing block).
  const pastMiddle = (x: number): boolean => {
    const column = (rootRef.current?.offsetParent as HTMLElement | null)?.offsetParent as HTMLElement | null
    if (!column) return false
    const r = column.getBoundingClientRect()
    if (!r.width) return false
    const mid = r.left + r.width / 2
    return right ? x < mid : x > mid
  }

  // The drag's preview: each chip shifted to its place in the drag's order,
  // the dragged one under the pointer. The DOM order never changes mid-drag:
  // moving the captured button would release the pointer.
  const place = (d: Drag) => {
    d.order.forEach((k, i) => {
      const wrap = wraps.current.get(k)
      const home = d.home.get(k)
      if (!wrap || home === undefined) return
      const from = d.slots[home]
      if (k === d.key) {
        wrap.style.transition = ''
        wrap.style.transform = `translate(${d.x - d.gx - from.left}px, ${d.y - d.gy - from.top}px)`
      } else {
        wrap.style.transition = motion ? `transform ${DURATION.fast}ms ${EASE.standard}` : ''
        wrap.style.transform = `translateY(${d.slots[i].top - from.top}px)`
      }
    })
  }

  const start = (d: Drag) => {
    d.active = true
    d.slots = keys.map((k) => {
      const el = wraps.current.get(k)
      return el ? boxOf(el) : { left: 0, top: 0, width: 0, height: 0 }
    })
    d.home = new Map(keys.map((k, i) => [k, i]))
    d.order = keys
    if (d.touch) {
      stopTouchEnd.current = true
      try { d.button.setPointerCapture(d.pointerId) } catch { /* the pointer is already up */ }
    }
    setLifted(d.key)
    onLift?.(true)
  }

  const end = (commit: boolean, at: number) => {
    const d = drag.current
    drag.current = null
    if (!d) return
    if (d.timer) clearTimeout(d.timer)
    if (!d.active) return
    swallowUntil.current = at + SWALLOW_MS
    const boxes = boxesNow()
    settle.current = { key: d.key, from: boxes.get(d.key) }
    if (commit && d.past) {
      noteDockMove(boxes)
      onReorder?.(d.order)
      setDockSide(otherSide(side))
    } else if (commit) {
      onReorder?.(d.order)
    }
    setLifted(null)
    setPast(false)
    onLift?.(false)
  }

  const onPointerDown = (e: React.PointerEvent<HTMLButtonElement>, key: string) => {
    swallowUntil.current = 0
    // Only the primary button drags (a finger and a pen press with button 0).
    if (e.button !== 0) return
    if (!onReorder) return
    const r = e.currentTarget.getBoundingClientRect()
    const d: Drag = {
      key, pointerId: e.pointerId, touch: e.pointerType === 'touch',
      x0: e.clientX, y0: e.clientY, gx: e.clientX - r.left, gy: e.clientY - r.top, x: e.clientX, y: e.clientY,
      active: false, timer: null, button: e.currentTarget, slots: [], home: new Map(), order: [], past: false,
    }
    drag.current = d
    if (d.touch) {
      d.timer = setTimeout(() => { if (drag.current === d && !d.active) start(d) }, HOLD_MS)
    } else {
      try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* no capture: the moves still come while over the chip */ }
    }
  }

  const onPointerMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    const d = drag.current
    if (!d || d.pointerId !== e.pointerId) return
    d.x = e.clientX
    d.y = e.clientY
    if (!d.active) {
      const moved = Math.hypot(e.clientX - d.x0, e.clientY - d.y0)
      if (d.touch) {
        // Travel before the hold: not a drag, the gesture is the page's.
        if (moved > HOLD_SLOP) { if (d.timer) clearTimeout(d.timer); drag.current = null }
        return
      }
      if (moved <= MOUSE_SLOP) return
      start(d)
    }
    const nowPast = pastMiddle(e.clientX)
    if (nowPast !== d.past) { d.past = nowPast; setPast(nowPast) }
    // The slot nearest the chip's centre.
    const cy = e.clientY - d.gy + (d.slots[0]?.height ?? 0) / 2
    let target = 0
    let best = Infinity
    d.slots.forEach((s, i) => {
      const dist = Math.abs(s.top + s.height / 2 - cy)
      if (dist < best) { best = dist; target = i }
    })
    const at = d.order.indexOf(d.key)
    if (at >= 0 && target !== at) {
      const next = d.order.filter((k) => k !== d.key)
      next.splice(target, 0, d.key)
      d.order = next
    }
    place(d)
  }

  if (shown.length === 0) return null

  return (
    <div
      ref={rootRef}
      className={`flex flex-col gap-2 [@media(pointer:coarse)]:gap-4 ${right ? 'items-end' : 'items-start'}`}
    >
      {shown.map((c) => {
        const up = lifted === c.key
        return (
          <div
            key={c.key}
            ref={(el) => {
              if (!el) return
              wraps.current.set(c.key, el)
              return () => { if (wraps.current.get(c.key) === el) wraps.current.delete(c.key) }
            }}
            className={`relative ${up ? 'z-10' : ''}`}
          >
            <button
              ref={chipRef(c.key)}
              onClick={(e) => {
                if (e.timeStamp < swallowUntil.current) { swallowUntil.current = 0; e.preventDefault(); return }
                noteOpening(c.opens, e.currentTarget)
                c.restore()
              }}
              onPointerDown={(e) => onPointerDown(e, c.key)}
              onPointerMove={onPointerMove}
              onPointerUp={(e) => end(true, e.timeStamp)}
              onPointerCancel={(e) => end(false, e.timeStamp)}
              onLostPointerCapture={(e) => { if (drag.current?.active) end(false, e.timeStamp) }}
              onContextMenu={(e) => { if (drag.current) e.preventDefault() }}
              title={`Open: ${c.title}`}
              className={`relative flex size-control-md touch-none select-none items-center justify-center rounded-xl border border-action/40 [-webkit-touch-callout:none] ${GLASS_FILL} ${GLASS_HOVER} transition-colors duration-fast ${up ? 'cursor-grabbing shadow-elevation-3' : ''} ${up && past ? 'ring-2 ring-action' : ''}`}
            >
              {c.label ? <span className="text-xs font-semibold text-secondary">{c.label}</span> : <TypeIcon type={c.type} />}
            </button>
            {/* dismiss — red, at the outer top corner, on every device. A
                coarse pointer gets a larger hit area toward that corner only,
                never over the chip's own button. */}
            {c.close && (
              <button
                onClick={c.close}
                title="Close"
                className={`absolute -top-1 ${right ? '-right-1' : '-left-1'} flex h-4 w-4 items-center justify-center rounded-full border border-status-danger-solid bg-status-danger-fill text-on-solid shadow-elevation-1 hover:bg-status-danger-fill-hover [@media(pointer:coarse)]:before:absolute [@media(pointer:coarse)]:before:-top-3 [@media(pointer:coarse)]:before:bottom-0 [@media(pointer:coarse)]:before:content-[''] ${right
                  ? '[@media(pointer:coarse)]:before:-right-3 [@media(pointer:coarse)]:before:left-0'
                  : '[@media(pointer:coarse)]:before:-left-3 [@media(pointer:coarse)]:before:right-0'}`}
              >
                <svg className="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}
