import { useState, useRef, useCallback } from 'react'

// Shared interactive-CLI state + send/warmup routing for the chat surfaces
// (AgentChat, task chats included). The native TUI runs under a PTY on the backend and
// is mirrored to a themed xterm; this hook owns the per-chat toggle intent, the
// live-session flag, and the few subtle rules for routing a send and handling
// warmup_ready, so the two pages don't diverge.

// Current dashboard light/dark mode — sent on interactive warmup so the backend
// seeds Claude's TUI theme to match (and the xterm background follows it).
export function currentDashboardTheme(): 'dark' | 'light' {
  return typeof document !== 'undefined' && document.documentElement.classList.contains('dark')
    ? 'dark'
    : 'light'
}

// Encode a UTF-8 string to base64 for pty_input frames (keystrokes + control
// bytes round-trip cleanly; the backend base64-decodes to raw PTY bytes).
export function utf8ToB64(s: string): string {
  const bytes = new TextEncoder().encode(s)
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}

// Encode a discrete prompt for the PTY as a BRACKETED PASTE (`ESC[200~ … ESC[201~`)
// followed by a single Enter. The TUI treats the whole block — including embedded
// newlines — as one pasted unit instead of submitting at the first `\n`, so a
// multi-line prompt (or our time-stamped prefix below) lands as a single message.
// Mirrors the backend pty_attachments paste (ws/dashboard.py). Use this for any
// ChatInput-sent prompt; raw keystrokes still use utf8ToB64 directly.
export function ptyPasteB64(text: string): string {
  return utf8ToB64('\x1b[200~' + text + '\x1b[201~\r')
}

// Prepend the browser user's current time + timezone to an interactive prompt,
// mirroring the `-p` path's `[Current time: …]` injection (proxy
// `config.format_current_time` → core/layers/cli/session.py). The `-p` pump adds this on the
// backend from the per-session browser tz; interactive prompts are typed straight
// into the PTY (no pump), so the dashboard — which holds the real browser
// time/zone — stamps them here. Format is matched to the backend (date + 24h +
// 12h AM/PM gloss + IANA name + UTC offset) so the agent gets the same
// unambiguous, schedule-safe time on every layer/target. Applied to every
// ChatInput-sent prompt (NOT raw terminal keystrokes — those aren't platform
// prompts). en-US for the date so it's stable regardless of browser locale.
export function withInteractiveTime(text: string): string {
  const now = new Date()
  let tzName = 'UTC'
  try {
    tzName = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch { /* keep UTC */ }
  const date = new Intl.DateTimeFormat('en-US', {
    weekday: 'long', year: 'numeric', month: 'long', day: '2-digit',
  }).format(now) // e.g. "Wednesday, June 16, 2026"
  const hh = now.getHours()
  const mm = String(now.getMinutes()).padStart(2, '0')
  const h24 = `${String(hh).padStart(2, '0')}:${mm}`
  const ampm = hh >= 12 ? 'PM' : 'AM'
  const h12 = `${(hh % 12) || 12}:${mm} ${ampm}`
  const offMin = -now.getTimezoneOffset() // minutes east of UTC
  const sign = offMin >= 0 ? '+' : '-'
  const abs = Math.abs(offMin)
  const offset = `${sign}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`
  return `[Current time: ${date} ${h24} (${h12}) ${tzName} (UTC${offset})]\n\n${text}`
}

// The slice of the dashboard WS this hook drives (kept minimal so it composes
// with the full useDashboardWs object both pages already hold).
interface InteractiveWs {
  changeExecutionMode: (executionMode: string, chatId?: string) => void
  switchExecutionMode: (executionMode: string, chatId: string, theme?: string) => void
  sendPtyInput: (chatId: string, dataB64: string, composer?: boolean) => void
  sendPtyAttachments: (
    chatId: string,
    text: string,
    images?: Array<{ base64: string; name: string }>,
    files?: Array<{ path: string; name: string }>,
  ) => void
  warmup: (
    agent: string,
    chatId?: string,
    permissionMode?: string,
    model?: string,
    executionPath?: string,
    prompt?: { text: string; images?: Array<{ base64: string; name: string }>; files?: Array<{ path: string; name: string }> },
    executionMode?: string,
    theme?: string,
  ) => void
}

// Params needed to cold-start an interactive session on the first send.
export interface InteractiveWarmupParams {
  agentName: string
  chatId?: string
  mode: string
  model: string
  layer?: string
}

export interface RouteSendCtx {
  chatId: string | null
  sessionId: string | null
  warmingUp: boolean
  warmupParams: InteractiveWarmupParams
  /** Page-specific UI prep before a cold start (add the user bubble + set the
   * warming flag). The stashed prompt is flushed to the PTY on warmup_ready. */
  onColdStart: () => void
  /** Chat-attached photos (base64) + already-uploaded files. When present
   * the live send goes through `sendPtyAttachments` (the backend saves the
   * photos + types the Read-tool paths into the PTY) instead of plain text. */
  images?: Array<{ base64: string; name: string }>
  files?: Array<{ path: string; name: string }>
}

/** 'pty' — written to the live terminal; 'cold' — cold-started an interactive
 * warmup (text stashed for flush on ready); null — not interactive, the caller
 * should run its normal send path. */
export type RouteSendResult = 'pty' | 'cold' | null

/** 'interactive' — a live PTY exists, stashed text flushed; 'declined' — we
 * requested interactive but the backend ran headless, the stashed prompt was
 * handed to onDecline; 'none' — not an interactive warmup, the caller handles
 * the rest of warmup_ready (e.g. server-kick adoption). */
export type WarmupReadyResult = 'interactive' | 'declined' | 'none'

export function useInteractiveChat(ws: InteractiveWs, agentDefaultMode: string = '') {
  // The per-chat execution-mode OVERRIDE (tri-state): '' = unset (follow the
  // agent default), 'interactive' / '-p' = an explicit per-chat choice. Sent as
  // execution_mode on warmup; restored from the chat's stored mode on open.
  const [chatExecMode, setChatExecMode] = useState<'' | 'interactive' | '-p'>('')
  // The EFFECTIVE on/off for the toggle + send routing: the per-chat override
  // wins, else the agent's default execution mode. DERIVED (not state)
  // so it tracks both inputs reactively — a brand-new
  // chat with no override reflects the agent default the moment it loads.
  const interactiveMode = (chatExecMode || agentDefaultMode) === 'interactive'
  // A live PTY-backed session exists (from warmup_ready.interactive) → the page
  // renders the terminal instead of the message list. Cleared on chat switch
  // until the resumed chat's warmup_ready{interactive} arrives.
  const [sessionInteractive, setSessionInteractive] = useState(false)
  // The view-toggle: while a live terminal exists, the
  // user can flip to the DB rich history (and back) WITHOUT killing the session —
  // the page keeps <TerminalView> mounted (hidden) so the PTY stays attached and
  // overlays <ChatMessages> when this is true. Only meaningful when
  // sessionInteractive; reset whenever a (re)warm lands so a fresh terminal
  // always opens on the terminal, not a stale snapshot.
  const [showRichView, setShowRichView] = useState(false)
  // Text typed before the terminal was ready; flushed as the first pty_input on
  // warmup_ready (or replayed as a normal turn if the backend declines).
  const pendingPtyTextRef = useRef<string | null>(null)
  // Photos/files attached on a cold-start send; flushed via
  // sendPtyAttachments on warmup_ready (the backend saves + types the paths).
  const pendingPtyAttachmentsRef = useRef<{
    images?: Array<{ base64: string; name: string }>
    files?: Array<{ path: string; name: string }>
  } | null>(null)
  // A live switch (kill+rewarm) is in flight — locks the toggle until
  // the re-warm's warmup_ready lands, so it can't be double-fired.
  const [switching, setSwitching] = useState(false)

  // Toggle the per-chat intent + persist it. Callers
  // only invoke this when no session is live (the switch is locked
  // otherwise), so there's nothing to kill+rewarm — the chosen mode is spawned
  // on the next send. chatId is null on a brand-new chat (local only; the
  // warmup carries execution_mode on the first send).
  const toggle = useCallback((next: boolean, chatId: string | null) => {
    // Write an EXPLICIT mode both ways: turning OFF must persist '-p' (not ''),
    // so it overrides an interactive AGENT default — '' would resolve back to
    // the agent default and the chat could never be turned off (precedence).
    const next_mode = next ? 'interactive' : '-p'
    setChatExecMode(next_mode)
    ws.changeExecutionMode(next_mode, chatId || undefined)
  }, [ws])

  // A switch requested while a -p turn is streaming, deferred to turn-end so we
  // don't cut a running generation (active-generation).
  const deferredSwitchRef = useRef<boolean | null>(null)

  // Live switch: kill + re-warm the chat in the
  // target mode. The caller confirms first (a live process is being restarted).
  // While a -p turn streams, defer to turn-end; otherwise switch now. The backend
  // reloads history + emits warmup_ready{interactive} → the page swaps the UI.
  const performSwitch = useCallback((next: boolean, chatId: string, streaming: boolean): 'switched' | 'deferred' => {
    if (streaming) {
      deferredSwitchRef.current = next
      return 'deferred'
    }
    const next_mode = next ? 'interactive' : '-p'
    setChatExecMode(next_mode)
    setSwitching(true)
    ws.switchExecutionMode(next_mode, chatId, currentDashboardTheme())
    return 'switched'
  }, [ws])

  // Fire any switch deferred while a turn was streaming (call on turn-end).
  const flushDeferredSwitch = useCallback((chatId: string | null) => {
    if (deferredSwitchRef.current === null || !chatId) return
    const next = deferredSwitchRef.current
    deferredSwitchRef.current = null
    const next_mode = next ? 'interactive' : '-p'
    setChatExecMode(next_mode)
    setSwitching(true)
    ws.switchExecutionMode(next_mode, chatId, currentDashboardTheme())
  }, [ws])

  // Seed the per-chat override for a NEW chat from the agent's STICKY interactive
  // preference (agentPrefsStore.lastInteractive) — the interactive twin of seeding
  // the model/mode dropdowns. Local-only (NO ws.changeExecutionMode): a new chat
  // has no row yet, and the next send's warmup carries chatExecMode as its
  // execution_mode. Only an explicit 'interactive'/'-p' seeds; anything else is a
  // no-op (leave following the agent default).
  const seedExecMode = useCallback((mode: string) => {
    if (mode === 'interactive' || mode === '-p') setChatExecMode(mode)
  }, [])

  // Restore the toggle from a chat's stored execution_mode (chat_history meta).
  // sessionInteractive stays false until a warmup_ready{interactive} arrives, so
  // a dead interactive chat shows its DB history with the toggle reflected ON.
  const restoreFromMeta = useCallback((executionMode: string | undefined) => {
    // Store the chat's explicit override ('' | 'interactive' | '-p'); the
    // effective toggle is derived from it + the agent default.
    const m = executionMode === 'interactive' || executionMode === '-p' ? executionMode : ''
    setChatExecMode(m)
    setSessionInteractive(false)
  }, [])

  // Clear the live-session state on chat switch / new chat, and clear the
  // per-chat override so a brand-new chat falls back to the AGENT default (a
  // switch to an existing chat then restores its stored mode via
  // restoreFromMeta, which the page calls right after).
  const resetSession = useCallback(() => {
    setSessionInteractive(false)
    setShowRichView(false)
    setSwitching(false)
    setChatExecMode('')
    pendingPtyTextRef.current = null
    pendingPtyAttachmentsRef.current = null
    deferredSwitchRef.current = null
  }, [])

  // Route a send. A live terminal → write the line straight to the PTY (no pump
  // turn, no bubble). Toggle on with nothing live yet → cold-start an
  // interactive warmup and stash the text. Otherwise return null so the caller
  // runs its normal (headless `-p`) send path.
  const routeSend = useCallback((text: string, ctx: RouteSendCtx): RouteSendResult => {
    const hasAttachments = !!(ctx.images?.length || ctx.files?.length)
    if (sessionInteractive && ctx.chatId) {
      // Live terminal. Stamp the browser time/zone onto the prompt (parity with
      // the -p path's inject_time) AT DELIVERY. Attachments → the backend saves
      // the photos + types the prompt + Read-tool paths into the PTY; plain text
      // → bracketed paste (the time prefix is multi-line, so a raw `text + \r`
      // would submit at the first newline).
      if (hasAttachments) ws.sendPtyAttachments(ctx.chatId, withInteractiveTime(text), ctx.images, ctx.files)
      else ws.sendPtyInput(ctx.chatId, ptyPasteB64(withInteractiveTime(text)), true)
      return 'pty'
    }
    if (interactiveMode && !ctx.sessionId && !ctx.warmingUp) {
      ctx.onColdStart()
      const p = ctx.warmupParams
      // Codex: deliver the first prompt as the launch arg via warmup — the
      // backend puts it in the `codex` argv and the TUI auto-runs it after MCP
      // warm. This is the deterministic first-turn submit; the PTY type-then-Enter
      // race is unreliable during Codex's warm. Claude keeps the PTY flush below.
      // (Attachments need the Read-tool path pipeline → fall back to the flush.)
      if (p.layer === 'codex-cli' && !hasAttachments) {
        // Stamp the argv prompt (parity with -p inject_time); the TUI auto-runs it.
        ws.warmup(p.agentName, p.chatId, p.mode, p.model, p.layer, { text: withInteractiveTime(text) }, 'interactive', currentDashboardTheme())
        return 'cold'
      }
      // Stash the RAW (un-stamped) prompt: if the backend ends up running HEADLESS
      // (-p fallback below), that path injects time itself — stamping here would
      // double it. The interactive flush in onWarmupReady stamps at delivery.
      pendingPtyTextRef.current = text
      pendingPtyAttachmentsRef.current = hasAttachments ? { images: ctx.images, files: ctx.files } : null
      ws.warmup(p.agentName, p.chatId, p.mode, p.model, p.layer, undefined, 'interactive', currentDashboardTheme())
      return 'cold'
    }
    return null
  }, [ws, interactiveMode, sessionInteractive])

  // Handle the interactive parts of warmup_ready. The caller runs this first,
  // then does its own page-specific work only when the result is 'none'.
  const onWarmupReady = useCallback((
    data: { interactive?: boolean; chat_id?: string },
    hooks: { onDecline: (text: string, chatId: string) => void },
  ): WarmupReadyResult => {
    const isInteractive = !!data.interactive
    setSessionInteractive(isInteractive)
    setShowRichView(false)  // a (re)warm landed → open on the terminal, not a stale rich snapshot
    setSwitching(false)  // a re-warm landed → any in-flight live switch is done
    if (isInteractive) {
      if (pendingPtyTextRef.current != null && data.chat_id) {
        const att = pendingPtyAttachmentsRef.current
        // Interactive landed → stamp browser time/zone at delivery (parity with
        // -p inject_time); bracketed paste because the prefix is multi-line.
        if (att) ws.sendPtyAttachments(data.chat_id, withInteractiveTime(pendingPtyTextRef.current), att.images, att.files)
        else ws.sendPtyInput(data.chat_id, ptyPasteB64(withInteractiveTime(pendingPtyTextRef.current)), true)
      }
      pendingPtyTextRef.current = null
      pendingPtyAttachmentsRef.current = null
      return 'interactive'
    }
    if (pendingPtyTextRef.current && data.chat_id) {
      // Requested interactive but the backend ran HEADLESS (kill-switch off, or
      // a non-Claude / remote target). Don't drop the message — run the stashed
      // prompt as a normal turn (the caller already added the bubble).
      const t = pendingPtyTextRef.current
      pendingPtyTextRef.current = null
      pendingPtyAttachmentsRef.current = null  // headless fallback drops attachments (rare)
      hooks.onDecline(t, data.chat_id)
      return 'declined'
    }
    return 'none'
  }, [ws])

  return {
    interactiveMode,
    // The EXPLICIT per-chat override ('' | 'interactive' | '-p'). Pass it as the
    // warmup's execution_mode so the spawn is self-contained — a new chat (no row
    // to persist `changeExecutionMode` into yet) and a toggle-then-send-fast race
    // both need the mode ON the warmup, not relying on the separate persist.
    chatExecMode,
    sessionInteractive,
    setSessionInteractive,
    showRichView,
    setShowRichView,
    switching,
    pendingPtyTextRef,
    toggle,
    performSwitch,
    flushDeferredSwitch,
    restoreFromMeta,
    seedExecMode,
    resetSession,
    routeSend,
    onWarmupReady,
  }
}
