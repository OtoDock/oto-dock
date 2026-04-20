import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { pushEscHandler } from '@/lib/escStack'
import ArtifactView from './ArtifactView'
import type { ArtifactWindow } from '@/hooks/useArtifactWindows'

/**
 * The floating (OPEN) artifact windows for an interactive CLI session.
 * Each display/file-tools artifact (gallery/chart,
 * video, audio, url, file, Collabora preview) opens as a draggable, minimizable
 * glass panel over the terminal. Window data comes from `useArtifactWindows`;
 * this component owns only the view (positions + z-order + drag). Minimized
 * windows dock as icon-buttons in the page's top-left panel stack — that is the
 * separate `ArtifactDock`, rendered by the page (not here).
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
  /** display_ui backchannel sender (PTY chats deliver via terminal injection). */
  onArtifactInteraction?: (token: string, title: string, payload: unknown) => Promise<{ status: string; reason?: string }>
}

interface Pos { x: number; y: number }

// px widths (clamped to the container on small screens). Documents (Collabora)
// and ui artifacts need a larger window than a gallery / link card.
const WIN_W_DEFAULT = 384 // matches w-96
const WIN_W_DOC = 640     // matches w-[40rem]
const isDocWin = (w: ArtifactWindow) => w.block.type === 'document_preview'
// Iframe-hosting windows: the window is fixed-size with overflow-hidden and
// the IFRAME is the single scroller — an overflow-auto body around an
// auto-height iframe stacks two scrollbars.
const isFrameWin = (w: ArtifactWindow) => isDocWin(w) || w.block.type === 'ui'
const winWidthPx = (w: ArtifactWindow) => (isFrameWin(w) ? WIN_W_DOC : WIN_W_DEFAULT)

export default function ArtifactWindows({ windows, minimized, onClose, onMinimize, agent, onArtifactInteraction }: Props) {
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const [positions, setPositions] = useState<Record<number, Pos>>({})
  const [order, setOrder] = useState<number[]>([]) // z-order, last = topmost
  const dragRef = useRef<{ id: number; dx: number; dy: number } | null>(null)
  const windowsRef = useRef(windows)
  windowsRef.current = windows

  const open = windows.filter((w) => !minimized.has(w.id))

  // Assign an initial cascade position (top-right) to any newly-opened window,
  // and keep the z-order list in sync with the live window set.
  useLayoutEffect(() => {
    const host = overlayRef.current
    const cw = host?.clientWidth ?? 800
    setPositions((prev) => {
      let changed = false
      const next = { ...prev }
      let cascade = 0
      for (const w of windows) {
        if (next[w.id]) continue
        const winW = Math.min(winWidthPx(w), cw - 16)
        const offset = (cascade % 5) * 28
        next[w.id] = { x: Math.max(8, cw - winW - 24 - offset), y: 16 + offset }
        cascade++
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
  }, [windows])

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
  openRef.current = open
  const orderRef = useRef(order)
  orderRef.current = order
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
    const winW = Math.min(dw ? winWidthPx(dw) : WIN_W_DEFAULT, cw - 16)
    const x = Math.min(Math.max(0, e.clientX - d.dx), Math.max(0, cw - winW))
    const y = Math.min(Math.max(0, e.clientY - d.dy), Math.max(0, ch - 40))
    setPositions((prev) => ({ ...prev, [d.id]: { x, y } }))
  }, [])

  const onHeaderPointerUp = useCallback((e: React.PointerEvent) => {
    dragRef.current = null
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId) } catch { /* not captured */ }
  }, [])

  if (windows.length === 0) return null

  return (
    <div ref={overlayRef} className="pointer-events-none absolute inset-0 z-30 overflow-hidden">
      {/* Render ALL windows, hiding minimized ones with `hidden` instead of
          unmounting — so a playing video/audio and a loaded Collabora iframe
          keep running in the background while minimized (the dock shows their
          icons). display:none does not pause media playback. */}
      {windows.map((w) => {
        const pos = positions[w.id] || { x: 24, y: 16 }
        const z = Math.max(0, order.indexOf(w.id))
        const frame = isFrameWin(w)
        const hidden = minimized.has(w.id)
        // Iframe windows (Collabora, ui artifacts) are larger + fixed-height so
        // the embedded iframe fills them; other artifacts size to content
        // (max-height capped).
        const sizeCls = frame
          ? 'w-[min(40rem,calc(100%-1rem))] h-[min(80%,44rem)]'
          : 'w-[min(24rem,calc(100%-1rem))] max-h-[min(70%,40rem)]'
        return (
          <div
            key={w.id}
            className={`oto-pop-in pointer-events-auto absolute flex flex-col rounded-xl border border-p-border-light bg-white/95 shadow-2xl backdrop-blur-xs dark:bg-gray-900/95 ${sizeCls} ${hidden ? 'hidden' : ''}`}
            style={{ left: pos.x, top: pos.y, zIndex: 10 + z }}
            onPointerDown={() => bringToFront(w.id)}
          >
            <div
              className="flex cursor-move touch-none select-none items-center gap-2 rounded-t-xl border-b border-p-border-light bg-black/5 px-3 py-1.5 dark:bg-white/5"
              onPointerDown={(e) => onHeaderPointerDown(e, w.id)}
              onPointerMove={onHeaderPointerMove}
              onPointerUp={onHeaderPointerUp}
            >
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-p-text">{w.title}</span>
              <button
                onClick={() => onMinimize(w.id)}
                title="Minimize"
                className="flex h-6 w-6 items-center justify-center rounded-md text-p-text-light hover:bg-black/10 dark:hover:bg-white/10"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" d="M5 12h14" />
                </svg>
              </button>
              <button
                onClick={() => onClose(w.id)}
                title="Close"
                className="flex h-6 w-6 items-center justify-center rounded-md text-p-text-light hover:bg-black/10 dark:hover:bg-white/10"
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
