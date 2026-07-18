import { useCallback, useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebglAddon } from '@xterm/addon-webgl'
import { WebLinksAddon } from '@xterm/addon-web-links'
import { Unicode11Addon } from '@xterm/addon-unicode11'
import { ClipboardAddon } from '@xterm/addon-clipboard'
import { openTerminalLink } from '../lib/openExternal'
import { createPtyBrandFilter } from '../lib/ptyBrandColors'
import { applyCtrlHold } from '../lib/terminalCtrlHold'

/**
 * Drives the dashboard side of an interactive CLI (PTY) session:
 * a themed xterm.js mirroring the native TUI. PTY
 * output (base64 `pty_output` frames) is written to the terminal; keystrokes
 * (xterm `onData`) + resizes go back as `pty_input`/`pty_resize`; the session's
 * permission queue arrives as `pty_permission` and is surfaced via the returned
 * `pendingPermission` (rendered with the existing PermissionDialog).
 *
 * Kept OUT of the useChatStream monolith — it subscribes to raw WS frames via
 * the ws bundle's generic `subscribe()`. It depends only on the ws bundle's
 * STABLE methods (not the bundle object, whose identity changes on every
 * streaming toggle) so a turn starting/ending never tears down the terminal.
 */

// The interactive surface of the useDashboardWs bundle this hook needs.
export interface InteractiveWs {
  subscribe: (frameType: string, fn: (msg: any) => void) => () => void
  sendPtyAttach: (chatId: string) => void
  sendPtyInput: (chatId: string, dataB64: string, composer?: boolean) => void
  sendPtyResize: (chatId: string, rows: number, cols: number) => void
  sendPermission: (requestId: string, approved: boolean) => void
}

export interface PtyPermission {
  kind: string // 'permission' | 'plan_review' | 'question'
  requestId: string
  toolName: string
  toolInput: any
  meetingAgent?: string
}

// xterm themes matched to the dashboard light/dark mode (bg ≈ --p-bg, accents
// from the brand ramp: #146bb5 blue, #4CAF50 success green, #da3536 accent red,
// #0d9488 teal, #673a97 purple). The chosen palette MUST match the
// Claude TUI theme the backend seeds (from the same dashboard mode at warmup) —
// otherwise Claude's text color clashes with the xterm background. Picked at
// mount; it stays for the session to stay coherent with Claude's baked theme.
// The TUI's truecolor diff rows are branded separately (lib/ptyBrandColors).
const DARK_THEME = {
  background: '#121620',
  foreground: '#dde3eb',
  cursor: '#1a7fce',
  cursorAccent: '#121620',
  selectionBackground: 'rgba(26,127,206,0.35)',
  black: '#1e1e1e', red: '#e25555', green: '#66bb6a', yellow: '#d7ba7d',
  blue: '#569cd6', magenta: '#b794d8', cyan: '#3fbfae', white: '#dde3eb',
  brightBlack: '#5a6370', brightRed: '#f28b82', brightGreen: '#89d185',
  brightYellow: '#f4b206', brightBlue: '#7cb7ff', brightMagenta: '#d0b3f0',
  brightCyan: '#6fd4c4', brightWhite: '#ffffff',
}
const LIGHT_THEME = {
  background: '#faf9f9',
  foreground: '#333333',
  cursor: '#146bb5',
  cursorAccent: '#faf9f9',
  selectionBackground: 'rgba(20,107,181,0.20)',
  black: '#2e2e2e', red: '#c22a2b', green: '#2e7d32', yellow: '#b8860b',
  blue: '#146bb5', magenta: '#673a97', cyan: '#0d7c72', white: '#5c6370',
  brightBlack: '#6b7280', brightRed: '#da3536', brightGreen: '#43a047',
  brightYellow: '#caa14a', brightBlue: '#1a7fce', brightMagenta: '#8b53c1',
  brightCyan: '#0d9488', brightWhite: '#1a1a1a',
}

function pickMode(): 'light' | 'dark' {
  const dark =
    typeof document !== 'undefined' &&
    document.documentElement.classList.contains('dark')
  return dark ? 'dark' : 'light'
}

const FONT_FAMILY =
  "'JetBrains Mono', 'Cascadia Code', 'SF Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace"

// Wheel-scroll speed multiplier for the terminal scrollback (xterm default is 1,
// which reads sluggish next to normal page scrolling). Applies to both terminal
// surfaces (chat + task run) via this one hook; the touch path divides its
// synthetic wheel deltas by this so finger-tracking stays 1:1.
const WHEEL_SCROLL_SENSITIVITY = 1.5

function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

function strToB64(s: string): string {
  const bytes = new TextEncoder().encode(s)
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}

export function useInteractiveTerminal(ws: InteractiveWs, chatId: string, onExit?: () => void) {
  // Destructure the STABLE methods so the effect's deps don't churn with the
  // bundle's identity (which changes on streaming toggles).
  const { subscribe, sendPtyAttach, sendPtyInput, sendPtyResize, sendPermission } = ws

  // Latest onExit in a ref so the terminal-init effect (which must NOT re-run
  // when this changes) can fire it on a real pty_exit without listing it as a dep.
  const onExitRef = useRef(onExit)
  onExitRef.current = onExit
  // While the satellite WS is mid-reconnect the terminal is frozen and
  // keystrokes can't reach it — pause input (the onData closure reads this ref) +
  // surface a banner. Cleared on "reconnected" or a fresh attach.
  const reconnectingRef = useRef(false)
  // Dual-control: once this viewer is evicted (a take-over) or the session
  // exits, STOP sending keystrokes — the onData closure reads this ref (it can't
  // see the `exited` state, the init effect never re-runs). The proxy also gates
  // dashboard input server-side while a local terminal controls; this is the UX
  // half (don't emit dead frames + match "reload to take over").
  const exitedRef = useRef(false)

  const containerRef = useRef<HTMLDivElement | null>(null)
  const termRef = useRef<Terminal | null>(null)
  const [pendingPermission, setPendingPermission] = useState<PtyPermission | null>(null)
  const [exited, setExited] = useState(false)
  const [exitReason, setExitReason] = useState('')
  const [reconnecting, setReconnecting] = useState(false)

  // Click-to-focus: xterm only sends keystrokes while its hidden textarea has
  // focus, so the wrapper forwards a click anywhere in the padded area to it
  // (the xterm is the live, direct desktop surface).
  const focus = useCallback(() => {
    try { termRef.current?.focus() } catch { /* not mounted */ }
  }, [])

  const respondPermission = useCallback(
    (requestId: string, approved: boolean) => {
      sendPermission(requestId, approved)
      setPendingPermission((p) => (p && p.requestId === requestId ? null : p))
    },
    [sendPermission],
  )

  useEffect(() => {
    const el = containerRef.current
    if (!chatId || !el) return
    setExited(false)
    setExitReason('')
    setPendingPermission(null)
    setReconnecting(false)
    reconnectingRef.current = false
    exitedRef.current = false  // a fresh (re)attach re-claims control

    const mode = pickMode()
    const term = new Terminal({
      fontFamily: FONT_FAMILY,
      fontSize: 13,
      theme: mode === 'dark' ? DARK_THEME : LIGHT_THEME,
      cursorBlink: true,
      allowProposedApi: true, // Unicode11Addon
      scrollback: 5000,
      convertEol: false,
      scrollSensitivity: WHEEL_SCROLL_SENSITIVITY,
    })
    termRef.current = term
    const fit = new FitAddon()
    term.loadAddon(fit)
    try {
      const uni = new Unicode11Addon()
      term.loadAddon(uni)
      term.unicode.activeVersion = '11'
    } catch { /* unicode addon optional */ }
    // Detected URLs open on Ctrl/Cmd+click (a plain click is TUI input — the
    // CLIs run with mouse tracking on); in the Android app a plain tap opens a
    // Custom Tab (the WebView swallows the addon's default window.open).
    try { term.loadAddon(new WebLinksAddon(openTerminalLink)) } catch { /* optional */ }
    // OSC 52 clipboard: Claude Code 2.1.x copies the TUI's own (mouse-tracking)
    // selection by emitting OSC 52, which xterm ignores without this addon — so
    // Ctrl-C "selected N via osc 52" silently failed unless you held Shift (which
    // forces xterm's native selection). The addon writes OSC 52 payloads to the
    // system clipboard (navigator.clipboard), so Ctrl-C copies without Shift.
    try { term.loadAddon(new ClipboardAddon()) } catch { /* optional */ }

    term.open(el)
    // WebGL renderer after open; fall back to the DOM renderer on context loss.
    try {
      const webgl = new WebglAddon()
      webgl.onContextLoss(() => { try { webgl.dispose() } catch { /* noop */ } })
      term.loadAddon(webgl)
    } catch { /* WebGL unavailable — canvas/DOM fallback */ }

    // Fit + size-sync chokepoint. Guarded (a 0-sized container mid-layout must
    // not fit — xterm would compute garbage dims) and deduped (only a CHANGED
    // rows×cols reaches the PTY as SIGWINCH). Every refit path below uses this:
    // mount (+ the rAF retry while the container is still 0-sized), the
    // ResizeObserver, and the font-load refit. The font one fixes a rare trio
    // of open-glitches (2026-07-11): fitting with FALLBACK font metrics before
    // the mono webfont loads computes too many rows — the TUI's input line
    // clips below the composer and the buffer "fits" the viewport so nothing
    // scrolls — and nothing re-fired fit on a desktop (container size never
    // changes) until a reload.
    let lastSentSize = ''
    const syncSize = (): boolean => {
      if (!el.isConnected || el.clientWidth < 2 || el.clientHeight < 2) return false
      try { fit.fit() } catch { return false }
      const key = `${term.rows}x${term.cols}`
      if (key !== lastSentSize) {
        lastSentSize = key
        try { sendPtyResize(chatId, term.rows, term.cols) } catch { /* not connected */ }
      }
      return true
    }
    syncSize()
    term.focus()
    // Container not laid out yet (flex/route transition): retry on frames,
    // bounded — the ResizeObserver takes over the moment the box gets a size.
    let fitRetryRaf = 0
    let fitRetries = 0
    const retryFit = () => {
      if (syncSize() || ++fitRetries > 60) return
      fitRetryRaf = requestAnimationFrame(retryFit)
    }
    if (lastSentSize === '') fitRetryRaf = requestAnimationFrame(retryFit)

    // Copy/paste. In a terminal Ctrl-C is SIGINT, so: Ctrl/Cmd-C copies when
    // there's a selection (else falls through as interrupt), Ctrl/Cmd-V pastes.
    // Shift variants always copy/paste. Right-click already works natively.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true
      const mod = e.ctrlKey || e.metaKey
      if (!mod) return true
      const k = e.key.toLowerCase()
      if (k === 'c') {
        const sel = term.getSelection()
        if (sel) {
          navigator.clipboard?.writeText(sel).catch(() => { /* insecure ctx / denied */ })
          term.clearSelection()
          return false // copied — suppress SIGINT
        }
        return true // no selection → Ctrl-C = interrupt (correct terminal behaviour)
      }
      if (k === 'v') {
        // Secure context: paste via the async clipboard API and preventDefault
        // the browser's native paste — returning false skips xterm's handling
        // (including ITS preventDefault), so without this BOTH paths fired and
        // every paste landed twice; the doubled TUI text then compounded on
        // each copy→paste round-trip. Insecure context (no readText): return
        // false WITHOUT preventDefault so the native paste stays the single
        // path (xterm's ^V handling stays suppressed either way).
        if (navigator.clipboard?.readText) {
          e.preventDefault()
          navigator.clipboard.readText()
            .then((t) => { if (t) term.paste(t) })
            .catch(() => { /* permission denied — right-click paste still works */ })
        }
        return false
      }
      return true
    })

    const onData = term.onData((d) => {
      // Drop keystrokes while the satellite is reconnecting (the PTY is
      // frozen on the far side; stale input into a TUI is worse than dropping).
      if (reconnectingRef.current) return
      // Dual-control: drop keystrokes after this viewer was evicted / the
      // session exited — this terminal is no longer the controller.
      if (exitedRef.current) return
      // Control bar's one-shot Ctrl: the next typed key becomes its control byte.
      sendPtyInput(chatId, strToB64(applyCtrlHold(d)))
    })

    // After a fresh attach / device-switch the backend replays the scrollback as
    // a burst of pty_output; Codex's full-redraw scrollback can leave the xterm
    // viewport at the TOP. Pin to the bottom for a short window after attach so
    // the latest output shows (Claude lands at the bottom already → no-op).
    let pinBottomUntil = 0
    // Stale-paint guard: heavy TUI redraw bursts can desync the WebGL
    // renderer's glyph/vertex state from its cell MODEL (the addon's atlas-page
    // races, xterm#4480 family) — rows render blank until the model changes on
    // those cells (a mouse selection, or the TUI rewriting them). term.refresh()
    // cannot repair this class: renderRows diffs against the model and skips
    // "unchanged" cells, which is exactly why the earlier refresh-only guard
    // still let the composer box vanish. clearTextureAtlas() clears atlas +
    // model → a true full re-render (no-op on the DOM fallback; the refresh
    // covers plain missed dirty-marks there).
    let repaintTimer: ReturnType<typeof setTimeout> | null = null
    let repaintDeferredSince = 0 // 0 = none pending
    const fullRepaint = () => {
      repaintDeferredSince = 0
      try { term.clearTextureAtlas() } catch { /* noop */ }
      try { term.refresh(0, term.rows - 1) } catch { /* noop */ }
    }
    const scheduleRepaint = () => {
      // Repaint once each output burst settles — but sustained generation
      // re-arms the settle timer on every frame and would defer forever
      // (the "vanished mid-turn" case), so bound the deferral.
      const now = Date.now()
      if (!repaintDeferredSince) repaintDeferredSince = now
      if (repaintTimer) clearTimeout(repaintTimer)
      if (now - repaintDeferredSince > 2000) {
        repaintTimer = null
        fullRepaint()
        return
      }
      repaintTimer = setTimeout(fullRepaint, 350)
    }
    const onVisibility = () => {
      if (document.visibilityState !== 'visible') return
      // Re-fit BEFORE repainting: writes while the tab was hidden ran against
      // throttled rAF, and stale render dimensions leave the viewport's
      // scroll area wrong (the "can't scroll until I type" flavor — a
      // keypress healed it only because its echo forced a fresh
      // write+render). syncSize() recomputes the geometry; the repaint then
      // redraws against it.
      syncSize()
      fullRepaint()
    }
    document.addEventListener('visibilitychange', onVisibility)
    // Idle stale-paint backstop (2026-07-17): every guard above fires on an
    // EVENT — output settling (scheduleRepaint), tab visibility, font load,
    // resize. A terminal that desyncs while VISIBLE and IDLE has no event
    // coming, so the stale rows sat there until a selection/resize/keypress
    // forced a re-render — the recurring "bottom separator vanished" report
    // (opening DevTools healed it because the viewport resize re-rendered).
    // While output is quiet, run the proven heal on a slow cadence: cheap
    // (glyph re-rasterization of one viewport) and visually seamless. Skipped
    // while hidden (tab in background / rich-view display:none) — those paths
    // repaint on their own transition handlers.
    let lastOutputAt = 0
    const idleRepaintTimer = setInterval(() => {
      if (document.visibilityState !== 'visible') return
      if (!el.isConnected || el.clientWidth < 2 || el.clientHeight < 2) return
      if (Date.now() - lastOutputAt < 5_000) return  // streaming — burst-settle guard owns it
      fullRepaint()
    }, 15_000)
    // Brand-tint the Claude TUI's truecolor diff rows (see lib/ptyBrandColors).
    // Both the xterm theme and the filter can be re-pointed by the session's
    // baked TUI theme (pty_status "attached" below).
    let appliedMode = mode
    let brand = createPtyBrandFilter(mode)
    const unsubOut = subscribe('pty_output', (m: any) => {
      if (m.chat_id && m.chat_id !== chatId) return
      try {
        // Refresh-on-reconnect: a reset replay clears the mis-aligned
        // mirror so the scrollback re-renders cleanly from scratch (then the live
        // gap output appends onto it).
        if (m.reset) { term.reset(); term.scrollToBottom(); brand.reset() }
        // "Following the tail" = at the bottom BEFORE this write. Codex's
        // --no-alt-screen redraws (from typing, the control-bar keys, ChatInput,
        // or a resize) can reposition the viewport to the TOP; if the user was
        // following, snap back to the bottom after the write so it stays pinned.
        // If they'd scrolled up to read, leave them there. pinBottomUntil covers
        // the attach-replay / resize bursts where the pre-write position is
        // ambiguous (empty buffer / mid-relayout).
        const buf = term.buffer.active
        const following = buf.viewportY >= buf.baseY
        term.write(brand.push(b64ToBytes(m.data || '')))
        if (following || Date.now() < pinBottomUntil) term.scrollToBottom()
        lastOutputAt = Date.now()
        scheduleRepaint()
      } catch { /* malformed frame */ }
    })
    // Pre-expiry re-warm: the platform replaced the CLI process to re-arm its
    // auth token; the session lives on under the same id. Poll re-attach until
    // the fresh process answers (registration takes a few seconds) — the
    // 'attached' status frame stops the poll.
    let rewarmTimer: ReturnType<typeof setInterval> | null = null
    const stopRewarmPoll = () => {
      if (rewarmTimer) { clearInterval(rewarmTimer); rewarmTimer = null }
    }

    const unsubExit = subscribe('pty_exit', (m: any) => {
      if (m.chat_id && m.chat_id !== chatId) return
      const reason = m.reason || ''
      if (reason === 'rewarmed') {
        try { term.reset() } catch { /* noop */ }
        try { term.write('\x1b[2m[refreshing session…]\x1b[0m\r\n') } catch { /* noop */ }
        stopRewarmPoll()
        let tries = 0
        rewarmTimer = setInterval(() => {
          if (++tries > 20) {
            // The respawn never came back — fall through to the normal ended
            // state (the next message re-warms the chat lazily).
            stopRewarmPoll()
            setExited(true)
            exitedRef.current = true
            setExitReason('')
            return
          }
          try { sendPtyAttach(chatId) } catch { /* not connected */ }
        }, 1000)
        return
      }
      setExited(true)
      exitedRef.current = true
      setExitReason(reason)
      // A TAKE-OVER (the session is NOT dead — it is alive elsewhere): another
      // dashboard tab/device ("superseded") OR a local `otodock` terminal
      // ("superseded_otodock", dual-control) claimed it. Anything else = the
      // CLI process actually ended.
      const isTakeover = reason === 'superseded' || reason === 'superseded_otodock'
      const note =
        reason === 'superseded'
          ? '\r\n\x1b[2m[opened on another device — reload to take over]\x1b[0m\r\n'
          : reason === 'superseded_otodock'
            ? '\r\n\x1b[2m[opened in a local terminal — reload to take over]\x1b[0m\r\n'
            : '\r\n\x1b[2m[session ended]\x1b[0m\r\n'
      try { term.write(note) } catch { /* noop */ }
      // A REAL exit (the CLI process died: a task finished, or the user quit the
      // TUI with Ctrl+C/Ctrl+D) — tell the page so it can leave the dead terminal
      // for the DB rich view and let the next send re-warm + RESUME the session.
      // A take-over is skipped — the session is alive
      // on another device/terminal, not dead.
      if (!isTakeover) onExitRef.current?.()
    })
    const unsubPerm = subscribe('pty_permission', (m: any) => {
      if (m.chat_id && m.chat_id !== chatId) return
      setPendingPermission({
        kind: m.kind || 'permission',
        requestId: m.request_id,
        toolName: m.tool_name || '',
        toolInput: m.tool_input,
        meetingAgent: m.meeting_agent,
      })
    })

    const unsubStatus = subscribe('pty_status', (m: any) => {
      // The REMOTE PTY transport is reconnecting/reconnected (a
      // satellite WS blip). The proxy held the session in grace, so the terminal
      // is alive — just frozen. Show a banner + pause input until it returns.
      if (m.chat_id && m.chat_id !== chatId) return
      // The session's baked TUI theme (sent on attach) wins over the dashboard
      // mode: a dark-seeded TUI in a light xterm paints its text for a dark
      // background — white-on-white (otodock-opened and re-warmed sessions
      // seed dark regardless of the viewer's dashboard mode).
      const t = m.tui_theme
      if ((t === 'light' || t === 'dark') && t !== appliedMode) {
        appliedMode = t
        try { term.options.theme = t === 'dark' ? DARK_THEME : LIGHT_THEME } catch { /* noop */ }
        brand = createPtyBrandFilter(t)
      }
      const st = m.state || ''
      if (st === 'attached') {
        // Attach acknowledged — if this was a re-warm re-attach, the fresh
        // process answered: stop polling and pin the replay to the bottom.
        stopRewarmPoll()
        pinBottomUntil = Date.now() + 1500
      }
      if (st === 'reconnecting') {
        reconnectingRef.current = true
        setReconnecting(true)
        try { term.write('\r\n\x1b[2m[reconnecting to the remote machine…]\x1b[0m\r\n') } catch { /* noop */ }
      } else if (st === 'reconnected') {
        reconnectingRef.current = false
        setReconnecting(false)
        // The proxy sends a reset+scrollback replay (a `pty_output` with reset)
        // right before this for a clean re-render; just re-sync the size so the
        // satellite renders post-reconnect output at the current width (covers a
        // resize dropped during the gap).
        try { sendPtyResize(chatId, term.rows, term.cols) } catch { /* noop */ }
      }
    })

    // Subscribed — signal the backend to attach the PTY viewer + replay the
    // scrollback NOW (the handshake that avoids the subscribe-vs-replay race).
    sendPtyAttach(chatId)
    pinBottomUntil = Date.now() + 1500  // keep the post-attach replay burst pinned to the bottom

    let raf = 0
    let wasZeroSize = false
    const ro = new ResizeObserver(() => {
      // Debounce to a frame so a drag-resize doesn't spam SIGWINCH.
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        // scrollToBottom after a fit: a resize (e.g. mobile keyboard open/close)
        // re-lays-out the buffer and Codex's redraw can jump the viewport to the
        // top — keep the latest output + input line visible. Codex's redraw
        // arrives ASYNC (pty_output) after the SIGWINCH, so also open a short
        // pin window so that redraw gets pinned too (see the pty_output handler).
        const zeroNow = !el.isConnected || el.clientWidth < 2 || el.clientHeight < 2
        if (syncSize()) {
          try { term.scrollToBottom() } catch { /* noop */ }
          pinBottomUntil = Date.now() + 1000
          // Re-shown from display:none (the rich-view toggle): writes kept
          // landing while hidden and a SAME-SIZE return re-renders nothing on
          // its own (fit dedupes) — repaint so the first visible frame isn't
          // stale.
          if (wasZeroSize) fullRepaint()
        }
        wasZeroSize = zeroNow
      })
    })
    ro.observe(el)

    // Font-load refit: the initial fit may have measured the FALLBACK font
    // (cold cache) — when the real mono font lands the cell size changes, so
    // re-fit + repaint or the terminal keeps the fallback geometry until some
    // container resize. `fonts.ready` covers the initial load; `loadingdone`
    // catches any late lazy face. Both idempotent through syncSize's dedupe.
    const onFontsLoaded = () => {
      if (syncSize()) {
        try { term.scrollToBottom() } catch { /* noop */ }
        fullRepaint()
      }
    }
    try { document.fonts?.ready.then(onFontsLoaded).catch(() => { /* noop */ }) } catch { /* older WebView */ }
    try { document.fonts?.addEventListener('loadingdone', onFontsLoaded) } catch { /* noop */ }

    // Blank-first-paint insurance: the attach replay can race the WebGL
    // renderer's init (atlas not ready → nothing drawn) and with no FURTHER
    // output nothing triggers the burst-settle repaint — the terminal shows
    // only background until a reload. One deferred full repaint after the
    // replay window covers it; a no-op when the first paint was fine.
    const postAttachRepaint = setTimeout(() => { syncSize(); fullRepaint() }, 1600)

    // Touch swipe → scroll the terminal scrollback.
    // xterm has no built-in touch scrolling (xterm.js #1007 / #5377). The robust
    // fix (per the copilot-cli reference + xterm PR #289 "wheel in app mode"):
    // translate the swipe into **synthetic wheel events dispatched at the
    // `.xterm-viewport`** — xterm's own wheel handler scrolls the buffer even in
    // application/mouse-tracking mode, which is exactly why DESKTOP wheel works;
    // reusing that path makes touch behave the same.
    //
    // Two important details that broke the earlier attempts:
    //  - The touch listeners go on **`term.element`** (the `.xterm` root) in the
    //    CAPTURE phase — NOT on `.xterm-viewport`, which sits BEHIND `.xterm-screen`
    //    and never receives the touches.
    //  - `touch-action: none` on `.xterm` stops the WebView from claiming the
    //    gesture first; we preventDefault + stopPropagation so the TUI doesn't
    //    also see it as a mouse drag. Taps (no move) never reach onTouchMove, so
    //    focus + the TUI's own tap handling still work.
    const xtermEl = term.element as HTMLElement | null
    const viewportEl = el.querySelector('.xterm-viewport') as HTMLElement | null
    const touchTarget: HTMLElement = xtermEl ?? el
    if (xtermEl) xtermEl.style.touchAction = 'none'
    let touchY = 0, touchActive = false
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) { touchActive = false; return }
      touchY = e.touches[0].clientY; touchActive = true
    }
    const onTouchMove = (e: TouchEvent) => {
      if (!touchActive || e.touches.length !== 1) return
      const t = e.touches[0]
      const dy = t.clientY - touchY
      touchY = t.clientY
      if (dy === 0) return
      // Swipe down (dy>0) → reveal earlier output → wheel UP (negative deltaY).
      // Divided by the wheel sensitivity (xterm applies it to synthetic wheel
      // events too) so touch scrolling stays locked to the finger.
      const target = viewportEl ?? touchTarget
      try {
        target.dispatchEvent(new WheelEvent('wheel', {
          deltaY: -dy / WHEEL_SCROLL_SENSITIVITY, deltaMode: 0, bubbles: true, cancelable: true,
          clientX: t.clientX, clientY: t.clientY,
        }))
      } catch {
        try { term.scrollLines(dy > 0 ? -1 : 1) } catch { /* noop */ }
      }
      e.preventDefault()
      e.stopPropagation()
    }
    const onTouchEnd = () => { touchActive = false }
    touchTarget.addEventListener('touchstart', onTouchStart, { capture: true, passive: true })
    touchTarget.addEventListener('touchmove', onTouchMove, { capture: true, passive: false })
    touchTarget.addEventListener('touchend', onTouchEnd, { capture: true, passive: true })

    // Mobile keyboard: focusing the xterm textarea opens the on-screen keyboard,
    // which scrolls the page up to keep the (cursor-positioned) textarea visible;
    // when the keyboard closes the page is NOT scrolled back, leaving the
    // terminal stuck up. Reset the document/window scroll once focus leaves an
    // input (keyboard actually closing — not just hopping to the ChatInput).
    const resetScroll = () => {
      const a = document.activeElement
      const typing = !!a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || (a as HTMLElement).isContentEditable)
      if (typing) return
      try {
        window.scrollTo(0, 0)
        if (document.scrollingElement) document.scrollingElement.scrollTop = 0
        term.scrollToBottom()  // keyboard closed → keep the terminal at the latest output
      } catch { /* noop */ }
    }
    const onBlur = () => setTimeout(resetScroll, 150)
    term.textarea?.addEventListener('blur', onBlur)
    const vv = window.visualViewport
    const onVvResize = () => {
      // Keyboard opened OR closed — from the xterm OR the dashboard ChatInput
      // below. Either way the terminal re-lays-out and Codex's (async) redraw can
      // jump the viewport to the top. Pin the xterm to the bottom across the
      // redraw, regardless of which input is focused (the resetScroll page-scroll
      // reset stays guarded against stealing focus while typing in ChatInput).
      pinBottomUntil = Date.now() + 1000
      try { term.scrollToBottom() } catch { /* noop */ }
      if (vv && vv.height >= window.innerHeight - 80) resetScroll()
    }
    vv?.addEventListener('resize', onVvResize)

    return () => {
      cancelAnimationFrame(raf)
      cancelAnimationFrame(fitRetryRaf)
      clearTimeout(postAttachRepaint)
      clearInterval(idleRepaintTimer)
      try { document.fonts?.removeEventListener('loadingdone', onFontsLoaded) } catch { /* noop */ }
      stopRewarmPoll()
      if (repaintTimer) clearTimeout(repaintTimer)
      document.removeEventListener('visibilitychange', onVisibility)
      onData.dispose()
      unsubOut(); unsubExit(); unsubPerm(); unsubStatus()
      ro.disconnect()
      touchTarget.removeEventListener('touchstart', onTouchStart, { capture: true } as any)
      touchTarget.removeEventListener('touchmove', onTouchMove, { capture: true } as any)
      touchTarget.removeEventListener('touchend', onTouchEnd, { capture: true } as any)
      term.textarea?.removeEventListener('blur', onBlur)
      vv?.removeEventListener('resize', onVvResize)
      // Release the GL context explicitly BEFORE disposing: term.dispose()
      // drops the WebGL addon but never frees its context, so every chat
      // switch leaked a zombie GL context until GC — and browsers cap live
      // contexts per page, evicting the OLDEST under pressure (the natural
      // context-loss source in the many-terminals workflow). getContext on
      // the addon's canvas returns its live context; non-GL canvases return
      // null and are skipped.
      try {
        for (const c of Array.from(el.querySelectorAll('canvas'))) {
          const gl = (c as HTMLCanvasElement).getContext('webgl2')
            || (c as HTMLCanvasElement).getContext('webgl')
          gl?.getExtension('WEBGL_lose_context')?.loseContext()
        }
      } catch { /* noop */ }
      try { term.dispose() } catch { /* already disposed */ }
      termRef.current = null
    }
  }, [chatId, subscribe, sendPtyAttach, sendPtyInput, sendPtyResize])

  return { containerRef, pendingPermission, respondPermission, exited, exitReason, reconnecting, focus }
}
