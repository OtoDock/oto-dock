import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { pushEscHandler } from '@/lib/escStack'
import { DOCK_SIDE, useDockSide } from '@/lib/ui/dockSide'
import { isNarrowViewport } from '@/components/settings/useNarrow'
import { useDocumentPaneStore } from '@/store/documentPaneStore'
import ArtifactView from './ArtifactView'
import { boxOf, chipFor, fold, slide, stop, takeOpening, unfold } from './fold'
import { DURATION, canAnimate, useReducedMotion } from './motion'
import { windowChipKey, type ArtifactWindow } from '@/hooks/useArtifactWindows'

/**
 * The floating (OPEN) artifact windows for an interactive CLI session.
 * Each display/file-tools artifact (gallery/chart,
 * video, audio, url, file, ui page) opens as a draggable, minimizable
 * glass panel over the terminal. Window data comes from `useArtifactWindows`;
 * this component owns only the view (positions + z-order + drag + motion).
 * Minimized windows dock as icon-buttons in the page's panel stack — that is
 * the separate `ArtifactDock`, rendered by the page (not here).
 *
 * A window folds into its chip on a minimize and opens out of its chip when
 * the chip was pressed (`fold.ts`); under reduced motion both are a plain
 * swap. New windows open on the side away from the dock; on a phone a window
 * opens beside the dock's column once the dock has chips.
 *
 * Rendered inside TerminalView's relative container as an inset overlay that is
 * click-through (pointer-events-none) except on the windows themselves, so the
 * live terminal stays fully interactive underneath.
 */
interface Props {
  windows: ArtifactWindow[]
  minimized: Set<number>
  onClose: (id: number) => void
  onMinimize: (id: number) => void
  /** Chat's agent slug — ui windows live-reload on a display_ui rewrite. */
  agent?: string
  /** The chat the windows belong to: a setting's card reads its owner. */
  chatId?: string
  /** display_ui backchannel sender (PTY chats deliver via terminal injection). */
  onArtifactInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }>
}

/** A window's place; `w` is the width a phone gives it beside the dock. */
interface Pos { x: number; y: number; w?: number }

// px widths (clamped to the container on small screens). A ui artifact needs
// a larger window than a gallery / link card. (A document opens in the
// chat's document pane, never here.)
const WIN_W_DEFAULT = 384 // matches w-96
const WIN_W_FRAME = 640   // matches w-[40rem]
// The dock's column on a phone: the stack's inset (left-3), a chip
// (size-control-md) and a gap.
const DOCK_COLUMN = 56
const EDGE = 8
// Iframe-hosting windows: the window is fixed-size with overflow-hidden and
// the IFRAME is the single scroller — an overflow-auto body around an
// auto-height iframe stacks two scrollbars.
const isFrameWin = (w: ArtifactWindow) => w.block.type === 'ui'
const winWidthPx = (w: ArtifactWindow) => (isFrameWin(w) ? WIN_W_FRAME : WIN_W_DEFAULT)
const NONE: ReadonlySet<number> = new Set()

export default function ArtifactWindows({ windows, minimized, onClose, onMinimize, agent, chatId, onArtifactInteraction }: Props) {
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const winEls = useRef(new Map<number, HTMLDivElement>())
  const [positions, setPositions] = useState<Record<number, Pos>>({})
  const [order, setOrder] = useState<number[]>([]) // z-order, last = topmost
  const dragRef = useRef<{ id: number; dx: number; dy: number } | null>(null)
  const windowsRef = useRef(windows)
  useLayoutEffect(() => { windowsRef.current = windows })
  const side = useDockSide()
  const reduced = useReducedMotion()
  const motion = canAnimate(reduced)

  // The overlay's size, kept by a resize observer: a phone's placement is
  // decided in render.
  const [box, setBox] = useState<{ w: number; h: number } | null>(null)

  // The place a phone gives a window opening now: beside the dock's column
  // when the dock has chips (other minimized windows, the minimized pane).
  const phonePlace = useCallback((id: number, cw: number): Pos | null => {
    if (!isNarrowViewport()) return null
    const pane = chatId ? useDocumentPaneStore.getState().byChat[chatId] : undefined
    const chips = windows.some((w) => w.id !== id && minimized.has(w.id)) || (!!pane && pane.open && pane.minimized)
    if (!chips) return null
    const x = side === DOCK_SIDE.RIGHT ? EDGE : DOCK_COLUMN
    return { x, y: 16, w: Math.max(160, cw - DOCK_COLUMN - EDGE) }
  }, [windows, minimized, chatId, side])

  // The windows, the minimized set and the dock's side of the previous
  // render: a window that was open there and is minimized now folds into its
  // chip ("flying": it stays on screen, inert, until it lands); one that was
  // minimized and is open now comes to the front, placed beside the dock on a
  // phone; the dock's move to the other side moves a phone's open windows
  // along. A window that arrives minimized never flies.
  const [seen, setSeen] = useState({ windows, minimized, side })
  const [flying, setFlying] = useState<ReadonlySet<number>>(NONE)
  if (seen.windows !== windows || seen.minimized !== minimized || seen.side !== side) {
    const ids = new Set(windows.map((w) => w.id))
    const wasOpen = new Set(seen.windows.filter((w) => !seen.minimized.has(w.id)).map((w) => w.id))
    const wasThere = new Set(seen.windows.map((w) => w.id))
    const folding = motion ? windows.filter((w) => minimized.has(w.id) && wasOpen.has(w.id)).map((w) => w.id) : []
    const opened = windows.filter((w) => !minimized.has(w.id) && wasThere.has(w.id) && !wasOpen.has(w.id)).map((w) => w.id)
    const next = new Set([...flying].filter((id) => ids.has(id) && minimized.has(id)))
    for (const id of folding) next.add(id)
    if (next.size !== flying.size || [...next].some((id) => !flying.has(id))) setFlying(next)
    if (opened.length) setOrder((prev) => [...prev.filter((x) => !opened.includes(x)), ...opened])
    if (box) {
      const placed: Record<number, Pos> = {}
      for (const id of opened) {
        const p = phonePlace(id, box.w)
        const cur = positions[id]
        if (p && (!cur || cur.x !== p.x || cur.w !== p.w)) placed[id] = p
      }
      if (seen.side !== side && isNarrowViewport()) {
        const x = side === DOCK_SIDE.RIGHT ? EDGE : DOCK_COLUMN
        for (const w of windows) {
          const cur = positions[w.id]
          if (!minimized.has(w.id) && cur?.w !== undefined && cur.x !== x && !placed[w.id]) placed[w.id] = { ...cur, x }
        }
      }
      if (Object.keys(placed).length) setPositions((prev) => ({ ...prev, ...placed }))
    }
    setSeen({ windows, minimized, side })
  }

  const open = windows.filter((w) => !minimized.has(w.id))

  // Assign an initial position to any newly-opened window — cascading from
  // the side away from the dock, beside the dock on a phone — and keep the
  // z-order list in sync with the live window set.
  useLayoutEffect(() => {
    const host = overlayRef.current
    const cw = host?.clientWidth ?? 800
    setPositions((prev) => {
      let changed = false
      const next = { ...prev }
      let cascade = windows.filter((w) => prev[w.id] && !minimized.has(w.id)).length
      for (const w of windows) {
        if (next[w.id]) continue
        const phone = minimized.has(w.id) ? null : phonePlace(w.id, cw)
        if (phone) {
          next[w.id] = phone
        } else {
          const winW = Math.min(winWidthPx(w), cw - 16)
          const offset = (cascade % 5) * 28
          next[w.id] = side === DOCK_SIDE.RIGHT
            ? { x: Math.min(24 + offset, Math.max(8, cw - winW - 8)), y: 16 + offset }
            : { x: Math.max(8, cw - winW - 24 - offset), y: 16 + offset }
          cascade++
        }
        changed = true
      }
      return changed ? next : prev
    })
    setOrder((prev) => {
      const ids = new Set(windows.map((w) => w.id))
      const kept = prev.filter((id) => ids.has(id))
      const added = windows.filter((w) => !kept.includes(w.id)).map((w) => w.id)
      return kept.length === prev.length && added.length === 0 ? prev : [...kept, ...added]
    })
  }, [windows, minimized, side, phonePlace])

  // The motions. A fold starts once per flight, toward its chip (mounted in
  // this same commit, see fold.ts; one frame later at most, else the window
  // just hides). A window that opened from a pressed chip grows out of the
  // chip's rectangle (already at its phone place: the render placed it). A
  // phone window the dock's move shifted slides from where it was.
  const started = useRef(new Set<number>())
  const prevMin = useRef(minimized)
  const prevPos = useRef(positions)
  const focusChip = useRef(new Set<number>())
  const land = useCallback((id: number) => {
    setFlying((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [])
  useLayoutEffect(() => {
    const was = prevMin.current
    const before = prevPos.current
    prevMin.current = minimized
    prevPos.current = positions
    // Folds.
    for (const id of flying) {
      if (started.current.has(id)) continue
      started.current.add(id)
      const el = winEls.current.get(id)
      const done = () => {
        land(id)
        // The chip takes the focus only when nothing else took it while the
        // window folded (a click into the terminal, typing): the focus is
        // still in the window, or on the body once the inert window let it go.
        const a = document.activeElement
        const free = !a || a === document.body || !!el?.contains(a)
        if (focusChip.current.delete(id) && free) chipFor(windowChipKey(id))?.focus()
      }
      const go = () => {
        const chip = chipFor(windowChipKey(id))
        return !!el && !!chip && fold(el, chip, done)
      }
      if (!go()) requestAnimationFrame(() => { if (!go()) done() })
    }
    for (const id of [...started.current]) {
      if (flying.has(id)) continue
      started.current.delete(id)
      // Landed and hidden: drop the fold's held end state.
      const el = winEls.current.get(id)
      if (el && minimized.has(id)) stop(el)
    }
    // A plain minimize (no motion) moves the focus to the chip at once.
    if (!motion) {
      for (const id of focusChip.current) {
        if (minimized.has(id)) chipFor(windowChipKey(id))?.focus()
      }
      focusChip.current.clear()
    }
    for (const w of windows) {
      const el = winEls.current.get(w.id)
      if (!el || minimized.has(w.id)) continue
      if (was.has(w.id)) {
        // Opened from its chip.
        const from = motion ? takeOpening(windowChipKey(w.id)) : null
        if (from) unfold(el, from)
        else stop(el)
        el.querySelector<HTMLElement>('button[title="Minimize"]')?.focus({ preventScroll: true })
        continue
      }
      const old = before[w.id]
      const cur = positions[w.id]
      if (motion && old && cur && old !== cur && old.w !== undefined && cur.w !== undefined && old.x !== cur.x && !dragRef.current) {
        const now = boxOf(el)
        slide(el, { ...now, left: now.left + old.x - cur.x }, DURATION.base)
      }
    }
  }, [flying, minimized, windows, positions, motion, land])

  // The overlay's size; a resize or a rotation keeps every window inside it.
  const hasWindows = windows.length > 0
  useEffect(() => {
    const host = overlayRef.current
    if (!host || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      const cw = host.clientWidth
      const ch = host.clientHeight
      setBox((prev) => (prev && prev.w === cw && prev.h === ch ? prev : { w: cw, h: ch }))
      setPositions((prev) => {
        let changed = false
        const next: Record<number, Pos> = {}
        for (const [key, p] of Object.entries(prev)) {
          const win = windowsRef.current.find((w) => w.id === Number(key))
          const w = p.w !== undefined ? Math.min(p.w, Math.max(160, cw - 2 * EDGE)) : undefined
          const width = w ?? Math.min(win ? winWidthPx(win) : WIN_W_DEFAULT, cw - 16)
          const x = Math.min(p.x, Math.max(0, cw - width))
          const y = Math.min(p.y, Math.max(0, ch - 40))
          next[Number(key)] = x === p.x && y === p.y && w === p.w ? p : { x, y, w }
          if (next[Number(key)] !== p) changed = true
        }
        return changed ? next : prev
      })
    })
    ro.observe(host)
    return () => ro.disconnect()
  }, [hasWindows])

  const bringToFront = useCallback((id: number) => {
    setOrder((prev) => (prev[prev.length - 1] === id ? prev : [...prev.filter((x) => x !== id), id]))
  }, [])

  // Esc closes the top-most open window. Registered ONLY while a window is open
  // (the component is always mounted by TerminalView, so an unconditional
  // handler would sit atop the esc stack and swallow Esc from the terminal /
  // other panels even when no artifact window is showing). Reads the live set
  // via refs so the single registration always targets the current top window.
  const hasOpen = open.length > 0
  const openRef = useRef(open)
  const orderRef = useRef(order)
  useLayoutEffect(() => {
    openRef.current = open
    orderRef.current = order
  })
  useEffect(() => {
    if (!hasOpen) return
    return pushEscHandler(() => {
      const ord = orderRef.current
      const openIds = new Set(openRef.current.map((w) => w.id))
      for (let i = ord.length - 1; i >= 0; i--) {
        if (openIds.has(ord[i])) { onClose(ord[i]); return }
      }
    })
  }, [hasOpen, onClose])

  const onHeaderPointerDown = useCallback((e: React.PointerEvent, id: number) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return
    // Don't start a drag when the press lands on a header button (minimize /
    // close) — capturing the pointer here would swallow the button's click
    // (desktop mouse, where pointer capture redirects pointerup off the button).
    if ((e.target as HTMLElement).closest('button')) return
    bringToFront(id)
    const pos = positions[id] || { x: 24, y: 16 }
    dragRef.current = { id, dx: e.clientX - pos.x, dy: e.clientY - pos.y }
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  }, [positions, bringToFront])

  const onHeaderPointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    const host = overlayRef.current
    const cw = host?.clientWidth ?? 800
    const ch = host?.clientHeight ?? 600
    const dw = windowsRef.current.find((w) => w.id === d.id)
    setPositions((prev) => {
      const cur = prev[d.id]
      const winW = cur?.w ?? Math.min(dw ? winWidthPx(dw) : WIN_W_DEFAULT, cw - 16)
      const x = Math.min(Math.max(0, e.clientX - d.dx), Math.max(0, cw - winW))
      const y = Math.min(Math.max(0, e.clientY - d.dy), Math.max(0, ch - 40))
      return { ...prev, [d.id]: { ...cur, x, y } }
    })
  }, [])

  const onHeaderPointerUp = useCallback((e: React.PointerEvent) => {
    dragRef.current = null
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId) } catch { /* not captured */ }
  }, [])

  if (windows.length === 0) return null

  return (
    <div ref={overlayRef} className="pointer-events-none absolute inset-0 z-30 overflow-hidden">
      {/* Render ALL windows, hiding minimized ones with `hidden` instead of
          unmounting — so a playing video/audio and a loaded ui page keep
          running in the background while minimized (the dock shows their
          icons). display:none does not pause media playback. */}
      {windows.map((w) => {
        const pos = positions[w.id] || { x: 24, y: 16 }
        const z = Math.max(0, order.indexOf(w.id))
        const frame = isFrameWin(w)
        const fly = flying.has(w.id)
        const hidden = minimized.has(w.id) && !fly
        // Iframe windows (ui artifacts) are larger + fixed-height so
        // the embedded iframe fills them; other artifacts size to content
        // (max-height capped). A phone's width beside the dock is inline.
        const sizeCls = frame
          ? 'w-[min(40rem,calc(100%-1rem))] h-[min(80%,44rem)]'
          : 'w-[min(24rem,calc(100%-1rem))] max-h-[min(70%,40rem)]'
        return (
          <div
            key={w.id}
            ref={(el) => {
              if (!el) return
              winEls.current.set(w.id, el)
              return () => { if (winEls.current.get(w.id) === el) winEls.current.delete(w.id) }
            }}
            data-artifact-window={w.id}
            className={`${reduced ? '' : 'oto-pop-in'} pointer-events-auto absolute flex flex-col rounded-dialog bg-surface-overlay text-primary shadow-elevation-4 ${sizeCls} ${hidden ? 'hidden' : ''}`}
            style={{ left: pos.x, top: pos.y, width: pos.w, zIndex: fly ? 11 + windows.length : 10 + z }}
            inert={fly}
            onPointerDown={() => bringToFront(w.id)}
          >
            <div
              className="flex cursor-move touch-none select-none items-center gap-2 rounded-t-dialog border-b border-subtle bg-surface-hover px-3 py-1.5"
              onPointerDown={(e) => onHeaderPointerDown(e, w.id)}
              onPointerMove={onHeaderPointerMove}
              onPointerUp={onHeaderPointerUp}
            >
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-primary">{w.title}</span>
              <button
                onClick={() => { focusChip.current.add(w.id); onMinimize(w.id) }}
                title="Minimize"
                className="flex h-6 w-6 items-center justify-center rounded-control text-secondary hover:bg-surface-hover hover:text-primary"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" d="M5 12h14" />
                </svg>
              </button>
              <button
                onClick={() => onClose(w.id)}
                title="Close"
                className="flex h-6 w-6 items-center justify-center rounded-control text-secondary hover:bg-surface-hover hover:text-primary"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className={`min-h-0 flex-1 ${frame ? 'overflow-hidden' : 'overflow-auto p-3'}`}>
              <ArtifactView block={w.block} agent={agent} embedded={frame} onArtifactInteraction={onArtifactInteraction} />
            </div>
          </div>
        )
      })}
    </div>
  )
}
