/**
 * useDashboardWs — the single dashboard WebSocket connection plus its frame
 * dispatch. Kept whole on purpose: the connect / reconnect / ping / idle
 * lifecycle and the large onmessage switch share a web of refs (callbacksRef
 * reads the latest state, streamingRef, reconnect timers) that only behaves
 * correctly as one closure. The frame TYPES + callback shape are split out
 * into useDashboardWs.types.
 */
import { useRef, useState, useCallback, useEffect, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { ActiveChat, Chat } from '../api/chats'

import { useChatStore, getActiveChatIds } from '@/store/chatStore'
import { toQueuedMessage } from '@/store/types'
import { useInstallStore } from '@/store/installStore'
import { useTransferStore } from '@/store/transferStore'
import { useMachineUpdateStore } from '@/store/machineUpdateStore'
import { emitFileUpdate } from '../lib/fileUpdates'
import { CATALOG_RESYNC, emitAppLive, releaseCatalogSender, resendCatalogSubscriptions, setCatalogSender } from '../lib/appLive'
import { releaseFocusSender, resendFocus, setFocusSender } from '../lib/focus'
import { noteServerBuild } from '../lib/buildId'
import type { WsCallbacks } from './useDashboardWs.types'
import { isTaskChatId } from '../lib/session/kind'
import { CHAT_PHASE } from '../lib/status/chat'
import { OUT, PER_CHAT_FRAMES, WIRE, isWireType, type WireFrame, type WireType } from '../api/wireEvents'
import { callNative } from '../lib/nativeBridge'
import { sessionStillValid } from '../api/auth'

// The frames, their per-chat flag (PER_CHAT_FRAMES is derived from the
// catalogue's table: a frame carrying a chat_id different from the viewed
// chat is dropped before dispatch, an untagged frame passes) and the
// messages this hook sends are the wire catalogue's (api/wireEvents.ts).

// A frame type the catalogue does not know warns ONCE per type per page:
// an older bundle dropped it silently, this one says so and still hands it
// to the bus. A catalogued frame without a case is silent.
const warnedFrameTypes = new Set<string>()

/** The close code of a dashboard socket held by the forced password change
 *  or 2FA enrolment (proxy ``ws/dashboard.py`` ``WS_CLOSE_GATE``); the close
 *  reason names the gate. */
export const WS_CLOSE_GATE = 4403
// The server closes an open socket 4001 when its session no longer holds
// (signed out, the password changed, the person removed); the tab asks
// /auth/me before it reconnects into the same refusal.
export const WS_CLOSE_SESSION = 4001
function warnUnknownFrame(type: unknown): void {
  const key = String(type)
  if (warnedFrameTypes.has(key)) return
  warnedFrameTypes.add(key)
  console.warn(`[ws] frame type not in the catalogue: ${key} (api/wireEvents.ts)`)
}
export function _resetUnknownFrameWarningsForTests(): void { warnedFrameTypes.clear() }

/** A photo on the wire: a fresh one travels as its data URL, one the chat's
 * scope already holds (handed back from a cancelled queued message) travels
 * by its saved path and is used in place. */
export type WireImage = { name: string; base64?: string; path?: string }
const wireImage = (i: WireImage) =>
  i.base64 ? { data: i.base64, name: i.name } : { path: i.path, name: i.name }

/** A message's id in its chat's queue. `crypto.randomUUID` exists only in
 *  a secure context (a dashboard served over plain http on a LAN address
 *  has none), `getRandomValues` everywhere. */
export function mintQueueId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** A queued message handed back to its author: into the composer of the
 *  chat this view shows (the page's callback), or into the stored draft
 *  and pending attachments of the chat it was typed into when the author
 *  is elsewhere. */
function returnToComposer(
  chatId: string | null | undefined,
  frame: { text?: string; images?: Array<{ name: string; path?: string }>;
           files?: Array<{ path: string; name: string }>; chat_id?: string },
  cb: WsCallbacks,
): void {
  if (!chatId || chatId === cb.viewedChatId) {
    cb.onQueueEditReturn?.({ type: WIRE.QUEUE_REMOVED, queue_id: '', index: -1,
                             text: frame.text, images: frame.images, files: frame.files,
                             chat_id: frame.chat_id })
    return
  }
  const st = useChatStore.getState()
  if (frame.text) {
    const draft = st.byChat[chatId]?.draftInput ?? ''
    st.setDraftInput(chatId, draft ? `${draft}\n\n${frame.text}` : frame.text)
  }
  const images = (frame.images ?? []).filter((i) => i.path)
  if (images.length) {
    st.addPendingImages(chatId, images.map((i) => ({ id: `img-${i.path}`, path: i.path, name: i.name })))
  }
  if (frame.files?.length) {
    st.addPendingFiles(chatId, frame.files.map((f) => ({
      id: `file-${f.path}`, name: f.name, size: 0, uploadedPath: f.path })))
  }
}

export function useDashboardWs(callbacks: WsCallbacks) {
  const wsRef = useRef<WebSocket | null>(null)
  // Stable app singleton — captured by the once-created onmessage closure to
  // patch query caches (Active-now titles) without a consumer in the loop.
  const queryClient = useQueryClient()
  // Generic frame subscribers (frameType → set of handlers), used by features
  // that own their own state outside the WsCallbacks object — currently the
  // interactive terminal (pty_output/pty_exit/pty_permission). Dispatched after
  // the main switch, so per-chat gating still applies. Keeps interactive logic
  // out of the useChatStream monolith.
  const frameSubsRef = useRef<Map<string, Set<(msg: any) => void>>>(new Map())
  const [connected, setConnected] = useState(false)
  const [_streaming, _setStreaming] = useState(false)
  // Latest-value ref so async handlers (ws.onclose lives inside a
  // useCallback([]) created once on mount) read the CURRENT streaming state
  // rather than the mount-time value — otherwise the mid-stream reload
  // recovery below never fires.
  const streamingRef = useRef(_streaming)
  streamingRef.current = _streaming
  // One auto-attach per streaming episode of the viewed chat (chat_status
  // handler): set synchronously on send, cleared when the chat leaves
  // streaming — the backend echoes chat_status streaming straight to the
  // socket that resumes, so deriving this from store state would loop.
  const autoAttachRef = useRef<string | null>(null)
  // Wrap setStreaming to also notify Android native (controls background wake lock)
  const streaming = _streaming
  const setStreaming = useCallback((value: boolean) => {
    _setStreaming(value)
    callNative('setStreaming', value)
  }, [])
  const callbacksRef = useRef(callbacks)
  callbacksRef.current = callbacks
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectDelay = useRef(1000)
  const intentionalClose = useRef(false)
  const pingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const healthCheckTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pongReceived = useRef(true)
  const missedPongs = useRef(0)
  // Track current chatId so we can re-resume on reconnect
  const currentChatId = useRef<string | null>(null)
  const reconnectedRef = useRef(false)
  // Minted chat ids whose warmup_started already moved the new-chat draft:
  // a reconnect replays a still-warming chat's frame (resume_chat) and the
  // draft typed on the new-chat page since then must stay where it is.
  const transferredNewChats = useRef<Set<string>>(new Set())
  // title_updated fallback-invalidation bounds (see the handler): once per
  // chat id + a PER-ID cooldown so rename bursts can't refetch-storm the
  // Active-now seed; plus the debounce for the task-list invalidation. The
  // cooldown must be per id, never shared — a global timestamp let a rename
  // burst on chat X swallow the only frame chat Y ever gets.
  const titleInvalidatedIds = useRef<Set<string>>(new Set())
  const lastTitleInvalidateAt = useRef<Map<string, number>>(new Map())
  const taskChatsInvalidateTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Track last reported IANA timezone so we can re-send on visibility change
  // when the user crosses time zones (laptop closed in Athens, opened in NYC).
  const lastSentTz = useRef<string | null>(null)
  // Idle detection — user is treated as inactive after 5 min of no input even
  // when the tab is still visible (e.g. laptop maximized at the office while
  // they're away at coffee). Backend then routes notifications to native push
  // instead of a toast nobody is around to hear.
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const isIdleRef = useRef(false)
  // Last presence tri-state sent to the backend — 'active' (visible + recent
  // input), 'away' (visible but input-idle: user stepped away from an open
  // dashboard), 'idle' (hidden tab). Dedupes redundant sends; a boolean here
  // would swallow the away→hidden transition (both are "not active").
  const lastSentState = useRef<string | null>(null)
  // Throttle for activity-event handler — mousemove fires many times per
  // second; we only need to bump the idle timer at most once per second.
  const lastActivityResetAt = useRef(0)

  // 5-minute idle threshold. Matches typical "away" timeouts (Slack/Discord) and is short
  // enough that an unattended laptop hands off to the user's phone within a coffee break.
  const IDLE_MS = 5 * 60 * 1000

  // Compute (visible AND !idle) and send the corresponding user_active / user_idle to the
  // backend, deduped. `force=true` bypasses the dedup and is used on ws.onopen so the backend
  // doesn't sit on its default `active=true` assumption when the tab opens already hidden or
  // the user has been idle through a reconnect.
  const sendActiveState = useCallback((ws: WebSocket | null, force = false) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    const visible = typeof document !== 'undefined' && document.visibilityState === 'visible'
    const active = visible && !isIdleRef.current
    // 'away' = dashboard visibly open but input-idle: the end-of-turn toast
    // would play to an empty chair, so the backend also routes the alert to
    // native push. A hidden tab stays plain-idle (`away: false`) — the user
    // is often multitasking on the same machine and a buzz would be spam.
    const state = active ? 'active' : visible ? 'away' : 'idle'
    if (!force && state === lastSentState.current) return
    lastSentState.current = state
    try {
      ws.send(JSON.stringify(
        active ? { type: OUT.USER_ACTIVE } : { type: OUT.USER_IDLE, away: visible },
      ))
    } catch { /* ignore */ }
  }, [])

  const armIdleTimer = useCallback(() => {
    if (idleTimer.current) clearTimeout(idleTimer.current)
    idleTimer.current = setTimeout(() => {
      if (!isIdleRef.current) {
        isIdleRef.current = true
        sendActiveState(wsRef.current)
      }
    }, IDLE_MS)
  }, [sendActiveState])

  const sendClientInfo = useCallback((ws: WebSocket) => {
    if (ws.readyState !== WebSocket.OPEN) return
    const isNative = !!(window as any).Capacitor?.isNativePlatform?.()
    let tz: string | undefined
    try {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone
    } catch {
      tz = undefined
    }
    try {
      ws.send(JSON.stringify({
        type: OUT.CLIENT_INFO,
        platform: isNative ? 'android' : 'web',
        time_zone: tz,
        // A watched task chat's turn end arrives as the rows this tab lacks
        // (chat_history_delta) instead of the whole history re-sent.
        history_deltas: true,
      }))
      if (tz) lastSentTz.current = tz
    } catch { /* ignore */ }
  }, [])

  // Schedule next ping (setTimeout chain — more resilient than setInterval to browser throttling)
  const schedulePing = useCallback((ws: WebSocket) => {
    if (pingTimer.current) clearTimeout(pingTimer.current)
    pingTimer.current = setTimeout(() => {
      if (ws.readyState !== WebSocket.OPEN) return

      if (!pongReceived.current) {
        missedPongs.current++
        if (missedPongs.current >= 3) {
          // 3 consecutive missed pongs — connection is dead
          ws.close()
          return
        }
      } else {
        missedPongs.current = 0
      }
      pongReceived.current = false
      try {
        ws.send(JSON.stringify({ type: OUT.PING }))
      } catch {
        ws.close()
        return
      }
      // Schedule next ping
      schedulePing(ws)
    }, 30000)
  }, [])

  const connect = useCallback(() => {
    // Guard against double-connect (OPEN or CONNECTING)
    const existing = wsRef.current
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return

    // A previous disconnect() (page navigation, StrictMode's dev-mode
    // mount→cleanup→mount cycle) left intentionalClose set — an explicit
    // connect() supersedes it. Without this reset, auto-reconnect and the
    // visibility health-check stay permanently disabled after any
    // disconnect→connect cycle.
    intentionalClose.current = false

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/dashboard`)
    // This socket's senders for the per-connection module state (viewer
    // focus, catalog subscriptions): its close releases these and no other.
    const sendFrame = (frame: Record<string, unknown>) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame))
    }

    ws.onopen = () => {
      setConnected(true)
      reconnectDelay.current = 1000
      pongReceived.current = true
      missedPongs.current = 0

      // Reconcile install-bar state with the server: drop non-terminal
      // slices — the connect replay (snapshot_inflight) immediately re-feeds
      // any install genuinely still running, so this only kills ghosts whose
      // done/failed frame was lost on a dropped socket (proxy restart
      // mid-install → "Preparing remote environment…" stuck forever).
      useInstallStore.getState().clearInFlight()

      // Same reconciliation for workspace transfer progress: in-flight items
      // are re-fed by the server's transfer_state connect replay; phase-1
      // (local XHR) items are locally owned and kept.
      useTransferStore.getState().clearInFlight()

      // Tell backend what platform + IANA timezone we're on. Platform controls
      // ephemeral notification routing; time_zone snapshots onto the session
      // so scheduled tasks/notifications resolve in the user's local TZ.
      sendClientInfo(ws)

      // Immediately tell the backend the real visibility/idle state so it
      // doesn't sit on its default active=true assumption when the tab opens
      // already hidden or after we've been idle through a reconnect. Force-send
      // bypasses the dedup since this WS is a fresh connection.
      sendActiveState(ws, true)
      // Viewer focus lives per connection on the server, so a fresh socket
      // starts blank: install the sender and send the current value now.
      setFocusSender(sendFrame)
      resendFocus()
      // Catalog subscriptions live per connection too: re-send them, then
      // every live server feed re-requests its snapshot, since deltas may
      // have been missed while the socket was down (APPS.md).
      setCatalogSender(sendFrame)
      resendCatalogSubscriptions()
      emitAppLive({ type: CATALOG_RESYNC })
      // Arm a fresh idle timer for this connection. Any user input will reset
      // it; after 5 min without input we'll switch to user_idle.
      armIdleTimer()

      // Start ping chain
      schedulePing(ws)

      // On reconnect, re-resume every chat that has live state on the
      // backend (in-flight warmup OR active stream). The backend uses
      // resume_chat to attach this WS as a listener — for warming chats
      // it pulls from the warmup_registry and replays history; for
      // streaming chats it re-attaches to the active pump. The currently-
      // displayed chat may not be in this set (e.g. it just finished),
      // so resume it too if we know it.
      if (reconnectedRef.current) {
        setStreaming(false)
        const ids = new Set<string>(getActiveChatIds())
        if (currentChatId.current) ids.add(currentChatId.current)
        for (const cid of ids) {
          ws.send(JSON.stringify({ type: OUT.RESUME_CHAT, chat_id: cid }))
        }
        // A title_updated missed across the gap leaves the Active-now widget
        // stale forever (it has no poll, unlike the history list) — refetch
        // the seed once per reconnect. One GET per tab; observers dedupe.
        queryClient.invalidateQueries({ queryKey: ['active-chats'] })
      }
      reconnectedRef.current = true
    }

    ws.onclose = (event?: CloseEvent) => {
      if (event?.code === WS_CLOSE_SESSION) {
        void sessionStillValid().then((ok) => {
          if (!ok) window.location.href = '/'
        })
      }
      // A session held by the forced password change or 2FA enrolment: the
      // socket closes 4403 with the gate's name, and the tab goes to that
      // screen (the same targets as apiFetch's X-Auth-Gate rule) instead of
      // reconnecting into the same refusal.
      if (event?.code === WS_CLOSE_GATE) {
        intentionalClose.current = true
        setConnected(false)
        setStreaming(false)
        wsRef.current = null
        releaseFocusSender(sendFrame)
        releaseCatalogSender(sendFrame)
        if (pingTimer.current) {
          clearTimeout(pingTimer.current)
          pingTimer.current = null
        }
        const target = event.reason === 'must_change_password' ? '/change-password' : '/setup-2fa'
        if (window.location.pathname !== target) window.location.href = target
        return
      }
      const wasStreaming = streamingRef.current
      setConnected(false)
      setStreaming(false)
      wsRef.current = null
      releaseFocusSender(sendFrame)
      releaseCatalogSender(sendFrame)
      if (pingTimer.current) {
        clearTimeout(pingTimer.current)
        pingTimer.current = null
      }
      // Notify pages that hold a "preWarmed" guard so they can reset and
      // let their eager useEffect re-fire pre_warmup once the WS is back.
      // Without this, AgentChat's preWarmedRef stays set across a reconnect
      // and the next WS never attaches as install listener — user on a
      // new-chat page loses install progress visibility during mobile
      // network flaps.
      try {
        window.dispatchEvent(new CustomEvent('otodock:ws-disconnect'))
      } catch { /* ignore */ }

      // If the WS died while any chat was streaming, force a page reload
      // for clean state recovery. The plain reconnect path
      // (resume_chat + chat_history) races against in-flight stream
      // events and can leave stale streaming bubbles in the UI; the
      // reload bypasses that by rebuilding all React state from DB.
      // For background-only state (no chat actively streaming on the
      // currently-displayed chat), auto-reconnect is enough and reload
      // would be needlessly disruptive — that path keeps the per-chat
      // resume_chat fan-out from onopen.
      if (wasStreaming && !intentionalClose.current) {
        setTimeout(() => window.location.reload(), 500)
        return
      }

      // Auto-reconnect for the non-streaming case. State recovery
      // happens via the per-chat resume_chat fan-out in onopen + the
      // backend warmup_registry attach + history replay.
      if (!intentionalClose.current) {
        reconnectTimer.current = setTimeout(() => {
          reconnectDelay.current = Math.min(reconnectDelay.current * 2, 30000)
          connect()
        }, reconnectDelay.current)
      }
    }

    ws.onerror = () => {
      // onclose will fire after this
    }

    ws.onmessage = (event) => {
      let raw: any
      try {
        raw = JSON.parse(event.data)
      } catch {
        return  // non-JSON frame — ignore
      }
      try {
        const cb = callbacksRef.current
        // Per-chat stream frames for a chat this view is NOT showing are
        // dropped (cross-chat contamination guard). viewedChatId === null
        // means a brand-new chat with no id yet — every tagged stream frame
        // is foreign until warmup_started mints the id.
        if (cb.viewedChatId !== undefined && raw.chat_id
            && raw.chat_id !== cb.viewedChatId && PER_CHAT_FRAMES.has(raw.type)) {
          return
        }
        const msg = raw as WireFrame
        switch (msg.type) {
          case WIRE.TEXT:
            cb.onText?.(msg.content)
            break
          case WIRE.THINKING:
            cb.onThinking?.(msg)
            break
          case WIRE.TOOL_START:
            cb.onToolStart?.(msg)
            break
          case WIRE.TOOL_INFO:
            cb.onToolInfo?.(msg)
            break
          case WIRE.TOOL_END:
            cb.onToolEnd?.(msg)
            break
          case WIRE.TASK_SPAWN:
            cb.onTaskSpawn?.(msg)
            break
          case WIRE.DELEGATE_SPAWN:
            cb.onDelegateSpawn?.(msg)
            break
          case WIRE.DELEGATE_RESULT:
            cb.onDelegateResult?.(msg)
            break
          case WIRE.BG_AGENT_DONE:
            cb.onBgAgentDone?.(msg)
            break
          case WIRE.BG_COMMAND_SPAWN:
            cb.onBgCommandSpawn?.(msg)
            break
          case WIRE.BG_COMMAND_DONE:
            cb.onBgCommandDone?.(msg)
            break
          case WIRE.BG_AGENTS_COMPLETE:
            cb.onBgAgentsComplete?.(msg)
            break
          case WIRE.BG_COMMANDS_COMPLETE:
            cb.onBgCommandsComplete?.(msg)
            break
          case WIRE.WORKFLOW_START:
            cb.onWorkflowStart?.(msg)
            break
          case WIRE.WORKFLOW_PROGRESS:
            cb.onWorkflowProgress?.(msg)
            break
          case WIRE.WORKFLOW_END:
            cb.onWorkflowEnd?.(msg)
            break
          case WIRE.FG_AGENTS_COMPLETE:
            cb.onFgAgentsComplete?.()
            break
          case WIRE.PERMISSION_PROMPT:
            cb.onPermissionPrompt?.(msg)
            break
          case WIRE.PROMPT_RETIRED:
            cb.onPromptRetired?.(msg)
            break
          case WIRE.LOCATION_REQUEST:
            cb.onLocationRequest?.(msg)
            break
          case WIRE.PLAN_MODE:
            cb.onPlanMode?.(msg)
            break
          case WIRE.PLAN_REVIEW:
            cb.onPlanReview?.(msg)
            break
          case WIRE.SYSTEM:
            cb.onSystem?.(msg)
            break
          case WIRE.METADATA:
            cb.onMetadata?.(msg)
            break
          case WIRE.DONE:
            setStreaming(false)
            {
              // Prefer the chat_id the backend stamps on the event over
              // currentChatId.current, which only reflects the chat the
              // user is currently viewing. If the user navigated to a
              // different chat mid-stream (or started a 2nd new chat in
              // the same WS session), currentChatId points to the wrong
              // chat and the sidebar streaming dot would stick on the
              // original chat forever. Backend emits chat_id on every
              // done frame (proxy/ws/dashboard.py) so this is reliable.
              const doneChatId: string = msg.chat_id || currentChatId.current || ''
              if (doneChatId) {
                useChatStore.getState().setReady(doneChatId)
              }
            }
            cb.onDone?.()
            break
          case WIRE.ERROR:
            // A pump's error carries the chat it ends: the turn is over, so
            // the composer goes back to Send and the next message starts a
            // new turn (a `done` follows once the rows landed). A bare error
            // (one refused message, no chat_id) changes nothing.
            if (msg.chat_id && msg.chat_id === cb.viewedChatId) {
              setStreaming(false)
              useChatStore.getState().setReady(msg.chat_id)
            }
            cb.onError?.(msg.message, msg.reason ? { reason: msg.reason, resets_at: msg.resets_at } : undefined)
            break
          case WIRE.IMAGES:
            cb.onImages?.(msg)
            break
          case WIRE.VIDEO:
            cb.onVideo?.(msg)
            break
          case WIRE.AUDIO:
            cb.onAudio?.(msg)
            break
          case WIRE.MEDIA_PROCESSING:
            cb.onMediaProcessing?.(msg)
            break
          case WIRE.MEDIA_FAILED:
            cb.onMediaFailed?.(msg)
            break
          case WIRE.IMAGE_GENERATING:
            cb.onImageGenerating?.(msg)
            break
          case WIRE.MCP_COST:
            cb.onMcpCost?.(msg)
            break
          case WIRE.IMAGE_GEN_FAILED:
            cb.onImageGenFailed?.()
            break
          case WIRE.LIMIT_WARNING:
            cb.onLimitWarning?.(msg)
            break
          case WIRE.LIMIT_REACHED:
            cb.onLimitReached?.(msg)
            break
          case WIRE.URL:
            cb.onUrl?.(msg)
            break
          case WIRE.FILE:
            cb.onFile?.(msg)
            break
          case WIRE.DOCUMENT_PREVIEW:
            cb.onDocumentPreview?.(msg)
            break
          case WIRE.FILE_UPDATED:
            // Fan out to any open Collabora preview (module bus) + let the host
            // invalidate the workspace file-tree query.
            emitFileUpdate(msg)
            cb.onFileUpdated?.(msg)
            break
          case WIRE.APP_PUSH:
          case WIRE.APP_STATE:
          case WIRE.OPEN_APP:
          case WIRE.APP_DEPLOYED:
          case WIRE.CATALOG:
            // Live-app frames (APPS.md "Live apps") reach their app
            // frames and the overlay through the module bus, not props.
            emitAppLive(msg)
            break
          case WIRE.PRE_WARMUP_READY:
            cb.onPreWarmupReady?.(msg)
            break
          case WIRE.WARMUP_READY:
            if (msg.chat_id) {
              useChatStore.getState().finishWarmup(msg.chat_id, {
                execution_path: msg.execution_path,
                execution_target: msg.execution_target,
                fallback_reason: msg.fallback_reason,
                turn_open: msg.turn_open,
                // Pin-vs-current-target mismatch (owner/admin frames only).
                // null when the fields are absent so a stale mismatch clears
                // — the fresh warmup after a successful move carries none.
                target_mismatch: msg.pinned_target && msg.resolved_target
                  ? {
                      pinnedTarget: msg.pinned_target,
                      pinnedLabel: msg.pinned_label || msg.pinned_target,
                      resolvedTarget: msg.resolved_target,
                      resolvedLabel: msg.resolved_label || msg.resolved_target,
                    }
                  : null,
              })
            }
            cb.onWarmupReady?.(msg)
            break
          case WIRE.WARMUP_STARTED:
            // Order matters: transfer the new-chat draft FIRST so the slice
            // for the freshly-minted chat_id carries it; beginWarmup then
            // merges (does not replace) so the draft survives.
            // Three guards, each load-bearing. `new_chat`: the frame also
            // fires for an existing chat re-warmed by a send, and that frame
            // must never take the new-chat page's draft. Strict null: only
            // the new-chat page (viewedChatId === null) holds a draft that
            // may move — an open chat (a string) or an opt-out consumer
            // (undefined) keeps its hands off. Once per id: a reconnect from
            // the new-chat page replays the still-warming minted chat's
            // frame, new_chat and all, and would move a draft typed since.
            if (msg.chat_id && msg.agent && msg.new_chat === true
                && cb.viewedChatId === null
                && !transferredNewChats.current.has(msg.chat_id)) {
              transferredNewChats.current.add(msg.chat_id)
              useChatStore.getState().transferNewChatToChat(msg.agent, msg.chat_id)
            }
            useChatStore.getState().beginWarmup(msg.chat_id, {
              agent: msg.agent,
              execution_path: msg.execution_path,
              execution_target: msg.execution_target,
            })
            // Adopt the new chat_id as the current focus. The previous
            // `if (!currentChatId.current)` guard was wrong: a 2nd new
            // chat in the same WS session would leave currentChatId
            // pointing at the FIRST chat, so subsequent done/aborted
            // events would mark the wrong chat as ready and the sidebar
            // streaming dot would stick on the new chat. warmup_started
            // is the signal that "this is the chat we're now working on" —
            // EXCEPT when the user already navigated to a different chat
            // while the spawn ran in the background (viewedChatId set and
            // mismatched): repointing then would mis-key done/aborted/
            // live-state handling at a chat the user left.
            {
              const vc = cb.viewedChatId
              if (vc === undefined || vc === null || vc === msg.chat_id) {
                currentChatId.current = msg.chat_id
              }
            }
            cb.onWarmupStarted?.(msg)
            break
          case WIRE.WARMUP_HEARTBEAT:
            useChatStore.getState().touchHeartbeat(msg.chat_id)
            break
          case WIRE.WARMUP_FAILED:
            useChatStore.getState().failWarmup(msg.chat_id, msg.error || 'warmup failed')
            cb.onWarmupFailed?.(msg)
            break
          case WIRE.CHAT_MOVED:
            // move_chat ack (the op acts on the connection's OPEN chat). The
            // consumer re-resumes the chat so the fresh warmup runs on the
            // new target and the "moved" history card arrives. Failures come
            // as standard error frames instead.
            cb.onChatMoved?.(msg)
            break
          case WIRE.ENGINE_SWITCHED:
            // Cross-engine switch: direct ack to the acting socket AND a
            // per-user broadcast to sibling tabs — consumers must be
            // idempotent (the actor receives it twice).
            cb.onEngineSwitched?.(msg)
            break
          case WIRE.SWITCH_ENGINE_DENIED:
            // Dedicated frame (NOT the generic error rail — that only
            // renders into an open stream bubble, invisible on the idle dead
            // chats this op runs on). Rendered inside the switch dialog.
            cb.onSwitchEngineDenied?.(msg)
            break
          case WIRE.LIVENESS:
            // probe_liveness answer — lazy process_alive refresh for the
            // bound chat (fired when the model dropdown opens).
            cb.onLiveness?.(msg)
            break
          // ── Install lifecycle (keyed by machine_id + agent, NOT chat_id) ──
          case WIRE.INSTALL_STARTED:
            useInstallStore.getState().begin(msg)
            break
          case WIRE.INSTALL_MCP_PLAN:
            useInstallStore.getState().setPlan(msg)
            break
          case WIRE.INSTALL_PROGRESS:
            useInstallStore.getState().recordProgress({
              machine_id: msg.machine_id,
              agent: msg.agent,
              mcp: msg.mcp,
              phase: msg.phase,
              pct: msg.pct,
              message: msg.message || '',
            })
            break
          case WIRE.INSTALL_HEARTBEAT:
            useInstallStore.getState().touchHeartbeat(msg)
            break
          case WIRE.MCP_INSTALL_FAILED:
            useInstallStore.getState().recordFailure({
              machine_id: msg.machine_id,
              agent: msg.agent,
              mcp: msg.mcp,
              error: msg.error || 'install failed',
            })
            break
          case WIRE.INSTALL_VERIFYING:
            useInstallStore.getState().verifying(msg)
            break
          case WIRE.INSTALL_DONE:
            useInstallStore.getState().finish(msg)
            break
          case WIRE.INSTALL_FAILED:
            useInstallStore.getState().fail({
              machine_id: msg.machine_id,
              agent: msg.agent,
              error: msg.error || 'install failed',
            })
            break
          // ── Workspace transfer progress (keyed by transfer_id) ──
          case WIRE.TRANSFER_STARTED:
            useTransferStore.getState().applyStarted(msg)
            break
          case WIRE.TRANSFER_MACHINE_STATE:
            useTransferStore.getState().applyMachineState(msg)
            break
          case WIRE.TRANSFER_PROGRESS:
            useTransferStore.getState().applyProgress(msg)
            break
          case WIRE.TRANSFER_DONE:
            useTransferStore.getState().applyDone(msg)
            break
          case WIRE.TRANSFER_STATE:
            useTransferStore.getState().applySnapshot(msg)
            break
          case WIRE.SATELLITE_UPDATING:
            useMachineUpdateStore.getState().beginUpdate(msg)
            cb.onSatelliteUpdating?.(msg)
            break
          case WIRE.SATELLITE_UPDATED:
            useMachineUpdateStore.getState().markUpdated(msg)
            cb.onSatelliteUpdated?.(msg)
            break
          case WIRE.SATELLITE_UPDATE_FAILED:
            useMachineUpdateStore.getState().markFailed(msg)
            cb.onSatelliteUpdateFailed?.(msg)
            break
          case WIRE.SATELLITE_UPDATE_SYNC:
            // Connect-time reconciliation: clears stale 'updating' banners whose
            // update finished while we were briefly disconnected (missed the
            // transient 'satellite_updated'), + surfaces any in-flight update.
            useMachineUpdateStore.getState().reconcile(msg.inflight || [])
            break
          case WIRE.MODE_CHANGED:
            cb.onModeChanged?.(msg.mode)
            break
          case WIRE.MODEL_CHANGED:
            cb.onModelChanged?.(msg.model)
            break
          case WIRE.EXECUTION_MODE_CHANGED:
            cb.onExecutionModeChanged?.(msg)
            break
          case WIRE.CHAT_HISTORY:
            cb.onChatHistory?.(msg)
            break
          case WIRE.CHAT_HISTORY_DELTA:
            cb.onChatHistoryDelta?.(msg)
            break
          case WIRE.QUEUED: {
            // The chat's queue (any chat: the frame is chat-routed). A chip
            // from a 1.7.0 proxy (no queue id) takes the page's own path.
            const qChat = msg.view_chat_id || msg.chat_id
            if (msg.queue_id && qChat) {
              useChatStore.getState().addQueuedMessage(qChat, msg.index, toQueuedMessage(msg))
            }
            if (!qChat || qChat === cb.viewedChatId) cb.onQueued?.(msg)
            break
          }
          case WIRE.STEERED:
            // Mid-turn steer accepted by the engine — the message is part of
            // the RUNNING turn (no queue entry, no new turn starts).
            cb.onSteered?.(msg)
            break
          case WIRE.QUEUE_REMOVED: {
            const rChat = msg.chat_id || currentChatId.current
            if (msg.queue_id && rChat) {
              useChatStore.getState().removeQueuedMessage(rChat, { queueId: msg.queue_id })
            }
            // The author's copy hands the text and the attachments back to
            // the composer of the chat they were typed into. A 1.7.0 proxy
            // sends them on every copy, with no `returned`.
            if ((msg.returned || !msg.queue_id)
                && (msg.text || msg.images?.length || msg.files?.length)) {
              returnToComposer(rChat, msg, cb)
            }
            cb.onQueueRemoved?.(msg)
            break
          }
          case WIRE.QUEUE_SENT: {
            // Key the slice by the FRAME's chat, not the viewed chat: its
            // chips go and its turn runs; the socket's own turn state moves
            // only for the chat this view shows.
            const qsChatId = msg.chat_id || currentChatId.current
            if (qsChatId) {
              const st = useChatStore.getState()
              // A turn that took no queued message (a nudge, an utterance)
              // names none: the chips stay.
              if (msg.queue_ids?.length) st.clearQueuedMessages(qsChatId, msg.queue_ids)
              st.setStreaming(qsChatId)
            }
            if (!msg.chat_id || msg.chat_id === cb.viewedChatId) {
              setStreaming(true)
              cb.onQueueSent?.(msg)
            }
            break
          }
          case WIRE.QUEUE_CLEARED:
            // The chips of the chat the frame names leave (a cancel, a Stop,
            // a failed turn, a refused delivery), whichever chat this view
            // shows; the author's copy hands them back to its composer. An
            // untagged frame acknowledges a clear the page already applied.
            if (msg.chat_id) {
              useChatStore.getState().clearQueuedMessages(msg.chat_id, msg.queue_ids)
              if (msg.returned && (msg.text || msg.images?.length || msg.files?.length)) {
                returnToComposer(msg.chat_id, msg, cb)
              }
            }
            break
          case WIRE.QUEUE_SNAPSHOT:
            // Backend-authoritative reconciliation on resume_chat — replaces
            // any reload-persisted queuedMessages with the chat's queue as
            // the proxy holds it. Strict replace, backend wins.
            if (msg.chat_id) {
              useChatStore.getState().setQueuedMessages(
                msg.chat_id, Array.isArray(msg.messages) ? msg.messages.map(toQueuedMessage) : [])
            }
            break
          case WIRE.SERVER_TURN_START:
            // A server-initiated turn (bg-nudge review / delegate-result
            // synthesis) is now streaming on this chat. No user-send flipped the
            // generating state, so do it here — the timer shows + Send becomes
            // Stop. The existing 'done'/'aborted' handlers clear it.
            setStreaming(true)
            cb.onServerTurnStart?.()  // start the live turn timer (no user-send did)
            {
              const sChatId: string = msg.chat_id || currentChatId.current || ''
              if (sChatId) useChatStore.getState().setStreaming(sChatId)
            }
            break
          case WIRE.CHAT_STATUS:
            // Authoritative per-chat live-dot signal, broadcast to ALL this user's
            // connections — lights/clears the sidebar dot for a chat generating
            // in the BACKGROUND, regardless of which chat this socket is viewing.
            if (msg.chat_id) {
              if (msg.status === CHAT_PHASE.STREAMING) {
                useChatStore.getState().setStreaming(msg.chat_id)
                // Auto-attach: a pump started server-side on the chat this
                // socket is ALREADY viewing (delegate echo, queued task run
                // leaving the park) — the open page never re-attaches on its
                // own (resume fires only on navigation), so the stream stayed
                // invisible until a reload.
                {
                  const viewed = cb.viewedChatId
                  if (viewed && msg.chat_id === viewed
                      && currentChatId.current === viewed
                      && !streamingRef.current
                      && !cb.isViewedChatPtyLive?.()
                      && autoAttachRef.current !== viewed) {
                    autoAttachRef.current = viewed
                    ws.send(JSON.stringify({ type: OUT.RESUME_CHAT, chat_id: viewed }))
                  }
                }
              } else {
                if (autoAttachRef.current === msg.chat_id) autoAttachRef.current = null
                useChatStore.getState().setReady(msg.chat_id)
                // A turn just finished somewhere the user isn't looking →
                // flip the sidebar unread dot immediately (the server row
                // confirms on the next list refetch). A finish on the VIEWED
                // chat while the tab is visible is read on arrival — the
                // page's chat_read triggers clear it. Task chats never flip:
                // tasks carry no unread state anywhere (notifications cover
                // completion) — this is the flag's only true-setter.
                const viewed = cb.viewedChatId
                if (!isTaskChatId(msg.chat_id)
                    && (msg.chat_id !== viewed || document.visibilityState === 'hidden')) {
                  useChatStore.getState().setUnread(msg.chat_id, true)
                }
              }
            }
            break
          case WIRE.CHAT_STATUS_SNAPSHOT: {
            // The authoritative "streaming right now" set, sent on connect and
            // again after the proxy's notify queue dropped a status frame —
            // clears stale streaming dots from missed frames and lights ones
            // this client never saw start (mirror of satellite_update_sync).
            const live = new Set<string>((msg.chat_ids as string[]) || [])
            const store = useChatStore.getState()
            for (const [cid, slice] of Object.entries(store.byChat)) {
              if (slice.status === CHAT_PHASE.STREAMING && !live.has(cid)) store.setReady(cid)
            }
            for (const cid of live) store.setStreaming(cid)
            break
          }
          case WIRE.CHAT_READ:
            // Someone (this user's other tab, or any user of a shared-only
            // chat) opened the chat — drop the unread dot everywhere.
            if (msg.chat_id) useChatStore.getState().setUnread(msg.chat_id, false)
            break
          case WIRE.PLAN_STATUS:
            cb.onPlanStatus?.(msg)
            break
          case WIRE.QUESTION:
            cb.onQuestion?.(msg)
            break
          case WIRE.TOOL_RESULT:
            cb.onToolResult?.(msg)
            break
          case WIRE.TITLE_UPDATED: {
            // Patch the Active-now seed in place: its rows render straight from
            // this cache, and a retitle landing AFTER the seed fetch (auto-title
            // of a still-streaming chat) otherwise shows the stale pre-title
            // until the next natural refetch.
            let matched = false
            queryClient.setQueryData<ActiveChat[]>(['active-chats'], (old) =>
              old?.map((r) => {
                if (r.id !== msg.chat_id) return r
                matched = true
                return { ...r, title: msg.title }
              }))
            if (!matched) {
              // No row to patch — the frame beat the chat's appearance in the
              // widget cache. Refetch the seed, BOUNDED: once per chat id +
              // a per-id 5s cooldown, so a rename burst (task-definition
              // rename fans one frame per live run) can't turn into a
              // refetch storm — and can't swallow another chat's only frame,
              // which a shared timestamp did. Renamed idle chats hit this
              // too — the per-id set is what keeps that cheap. If the burned
              // invalidate's refetch races empty (chat still warming), the
              // hook's bounded reseed retry is the backstop.
              const now = Date.now()
              if (!titleInvalidatedIds.current.has(msg.chat_id) &&
                  now - (lastTitleInvalidateAt.current.get(msg.chat_id) ?? 0) > 5000) {
                titleInvalidatedIds.current.add(msg.chat_id)
                lastTitleInvalidateAt.current.set(msg.chat_id, now)
                queryClient.invalidateQueries({ queryKey: ['active-chats'] })
              }
            }
            // Task rows label from task_name/chat title — keep the task list
            // fresh on OTHER clients. Scoped to task- ids (uuid worker chats
            // ride onTitleUpdated → refetchChats) and debounced.
            if (typeof msg.chat_id === 'string' && isTaskChatId(msg.chat_id)) {
              if (taskChatsInvalidateTimer.current) {
                clearTimeout(taskChatsInvalidateTimer.current)
              }
              taskChatsInvalidateTimer.current = setTimeout(() => {
                taskChatsInvalidateTimer.current = null
                queryClient.invalidateQueries({ queryKey: ['task-chats'] })
              }, 400)
            }
            cb.onTitleUpdated?.(msg)
            break
          }
          case WIRE.CHAT_ROWS:
            cb.onChatRows?.(msg)
            break
          case WIRE.CHAT_META:
            // Orchestrator stamp (project adoption / first delegation): patch
            // the cached chat rows so the sidebar accent + Dock gate flip
            // immediately instead of on the chats poll.
            queryClient.setQueriesData<Chat[]>({ queryKey: ['chats'] }, (old) =>
              old?.map((c) => (c.id === msg.chat_id
                ? {
                    ...c,
                    ...(msg.delegate_role ? { delegate_role: msg.delegate_role } : {}),
                    ...(msg.project_id ? { project_id: msg.project_id } : {}),
                  }
                : c)))
            if (msg.chat_id) {
              queryClient.invalidateQueries({ queryKey: ['chat-project', msg.chat_id] })
            }
            break
          case WIRE.ABORTED:
            setStreaming(false)
            {
              // Same chat_id preference as 'done' above — see comment.
              const abortChatId: string = msg.chat_id || currentChatId.current || ''
              if (abortChatId) {
                useChatStore.getState().setReady(abortChatId)
              }
            }
            cb.onAborted?.(msg)
            break
          case WIRE.LIVE_STATE: {
            // streaming === false is a residual snapshot (turn ended, bg
            // subagents still running) — restore badges without flipping the
            // input/stop state to "live".
            setStreaming(msg.streaming !== false)
            const lsChatId = msg.chat_id || currentChatId.current
            if (lsChatId && msg.streaming !== false) {
              useChatStore.getState().setStreaming(lsChatId)
            }
            cb.onLiveState?.(msg)
            break
          }
          case WIRE.TODO_UPDATE:
            cb.onTodoUpdate?.(msg)
            break
          case WIRE.GOAL_UPDATE:
            cb.onGoalUpdate?.(msg)
            break
          case WIRE.CONTEXT_COMPACT:
            cb.onContextCompact?.(msg)
            break
          case WIRE.NOTIFICATION:
            cb.onNotification?.(msg)
            break
          case WIRE.NOTIFICATION_SILENT:
            cb.onNotificationSilent?.(msg)
            break
          case WIRE.NOTIFICATION_COUNT:
            cb.onNotificationCount?.(msg)
            break
          case WIRE.SHARE_INBOX:
            cb.onShareInbox?.(msg)
            break
          case WIRE.TURN_COMPLETE:
            cb.onTurnComplete?.(msg)
            break
          case WIRE.PONG:
            pongReceived.current = true
            // The build the server serves rides every pong (and the
            // connect-time server_info): a page running an older build
            // reloads once — lib/buildId decides.
            if (typeof msg.build_id === 'string') noteServerBuild(msg.build_id)
            break
          case WIRE.SERVER_INFO:
            noteServerBuild(msg.build_id)
            break
          default:
            if (!isWireType(raw.type)) warnUnknownFrame(raw.type)
        }
        // Generic subscribers (after the switch + per-chat gating). A handler
        // throw is isolated per-subscriber so one bad listener can't drop the
        // frame for others.
        const subs = frameSubsRef.current.get(raw.type)
        if (subs) {
          for (const fn of Array.from(subs)) {
            try { fn(raw) } catch (e2) { console.error('[ws] subscriber failed:', raw?.type, e2) }
          }
        }
      } catch (e) {
        // A handler throw must not be silently swallowed: the frame is lost
        // either way, but losing it INVISIBLY turns real bugs into "the chat
        // froze" mysteries. Per-frame isolation — later frames still process.
        console.error('[ws] frame handler failed:', raw?.type, e)
      }
    }

    wsRef.current = ws
  }, [])

  const send = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  const preWarmup = useCallback(
    (agent: string, model?: string, permissionMode = 'default', executionPath?: string) => {
      send({ type: OUT.PRE_WARMUP, agent, model, permission_mode: permissionMode, execution_path: executionPath })
    },
    [send],
  )

  const warmup = useCallback(
    (agent: string, chatId?: string, permissionMode = 'default', model?: string, executionPath?: string,
     prompt?: { text: string; images?: WireImage[]; files?: Array<{ path: string; name: string }> },
     executionMode?: string, theme?: string) => {
      if (chatId) currentChatId.current = chatId
      // Carry the first prompt WITH warmup so the backend persists it
      // at send-time and server-kicks the turn on warmup_ready (survives
      // navigate-away / refresh during spawn — no client-side deferred send).
      // execution_mode is the per-chat interactive override — the backend
      // resolver's chat_override.
      const msg: Record<string, unknown> = { type: OUT.WARMUP, agent, chat_id: chatId, permission_mode: permissionMode, model, execution_path: executionPath }
      if (executionMode) msg.execution_mode = executionMode
      if (theme) msg.theme = theme
      if (prompt?.text) {
        msg.text = prompt.text
        if (prompt.images?.length) msg.images = prompt.images.map(wireImage)
        if (prompt.files?.length) msg.files = prompt.files
      }
      send(msg)
    },
    [send],
  )

  const sendMessage = useCallback(
    (text: string, chatId: string, images?: WireImage[], files?: Array<{ path: string; name: string }>) => {
      setStreaming(true)
      useChatStore.getState().setStreaming(chatId)
      // The message's id in the chat's queue: a re-send after a reconnect
      // is answered once, and a cancel names it.
      const msg: Record<string, unknown> = {
        type: OUT.CHAT, text, chat_id: chatId, queue_id: mintQueueId(),
      }
      if (images?.length) {
        msg.images = images.map(wireImage)
      }
      if (files?.length) {
        msg.files = files
      }
      send(msg)
    },
    [send],
  )

  const sendPermission = useCallback(
    (requestId: string, approved: boolean) => {
      send({ type: OUT.PERMISSION_RESPONSE, request_id: requestId, approved })
    },
    [send],
  )

  const sendLocationResponse = useCallback(
    (requestId: string, result: { lat?: number; lng?: number; accuracy?: number; error?: string }) => {
      send({ type: OUT.LOCATION_RESPONSE, request_id: requestId, ...result })
    },
    [send],
  )

  const sendPlanReviewResponse = useCallback(
    (requestId: string, action: string, filename?: string) => {
      send({ type: OUT.PLAN_REVIEW_RESPONSE, request_id: requestId, action, filename: filename || '' })
    },
    [send],
  )

  const sendQuestionResponse = useCallback(
    (requestId: string, answers: Record<string, { answers: string[] }>) => {
      send({ type: OUT.QUESTION_RESPONSE, request_id: requestId, answers })
    },
    [send],
  )

  // display_ui backchannel: an artifact's otodock.send payload, forwarded for
  // server-side validation + AskUserQuestion-style delivery. The server acks
  // with `artifact_ack` (generic subscribe registry — no switch case).
  const sendArtifactInteraction = useCallback(
    (chatId: string, token: string, title: string, payload: unknown) => {
      send({ type: OUT.ARTIFACT_INTERACTION, chat_id: chatId, token, title, payload })
    },
    [send],
  )

  // Pinned app send_prompt action — same delivery rails, gated on the
  // user-approved manifest server-side. Acked with `app_action_ack`.
  const sendAppAction = useCallback(
    (chatId: string, appId: string, actionId: string, args: unknown) => {
      send({ type: OUT.APP_ACTION, chat_id: chatId, app_id: appId, action_id: actionId, args })
    },
    [send],
  )

  // Interactive CLI (PTY) — subscribe to raw frames + send keystrokes/resize.
  const subscribe = useCallback((frameType: WireType, fn: (msg: any) => void) => {
    const m = frameSubsRef.current
    let set = m.get(frameType)
    if (!set) { set = new Set(); m.set(frameType, set) }
    set.add(fn)
    return () => {
      const s = frameSubsRef.current.get(frameType)
      if (s) { s.delete(fn); if (s.size === 0) frameSubsRef.current.delete(frameType) }
    }
  }, [])

  // Signals the backend that this socket's terminal has mounted + subscribed,
  // so it attaches the PTY viewer + replays scrollback now (no subscribe race).
  const sendPtyAttach = useCallback(
    (chatId: string) => {
      send({ type: OUT.PTY_ATTACH, chat_id: chatId })
    },
    [send],
  )

  const sendPtyInput = useCallback(
    (chatId: string, dataB64: string, composer = false) => {
      // `composer` marks a discrete chat-box send (vs raw terminal
      // keystrokes) — the backend holds flagged sends while the TUI is
      // parked on a question picker so the paste can't answer the dialog.
      send({ type: OUT.PTY_INPUT, chat_id: chatId, data: dataB64, ...(composer ? { composer: true } : {}) })
    },
    [send],
  )

  const sendPtyResize = useCallback(
    (chatId: string, rows: number, cols: number) => {
      send({ type: OUT.PTY_RESIZE, chat_id: chatId, rows, cols })
    },
    [send],
  )

  // Claim control of a terminal another user warmed (Shared-only agents put
  // several people on one chat). The backend ends that session; the terminal
  // then re-warms under THIS user on the next send, resuming the conversation.
  const sendPtyTakeover = useCallback(
    (chatId: string) => {
      send({ type: OUT.PTY_TAKEOVER, chat_id: chatId })
    },
    [send],
  )

  // Interactive CLI attachments: photos (base64) +
  // already-uploaded files. The backend saves the photos to the agent workspace
  // (same path as a normal chat turn), then types the prompt + the Read-tool
  // path references into the live PTY so the TUI's Read tool can open them.
  const sendPtyAttachments = useCallback(
    (chatId: string, text: string,
     images?: WireImage[],
     files?: Array<{ path: string; name: string }>) => {
      send({
        type: OUT.PTY_ATTACHMENTS, chat_id: chatId, text,
        images: images?.map(wireImage),
        files,
      })
    },
    [send],
  )

  // Mode and model picks NAME their chat. Without a chat_id the server
  // treats the pick as the new-chat state (deferred for the chat the first
  // warmup mints) and never applies it to the chat this socket is still
  // bound to — the previous chat, whose live session and row a pick made
  // right after "+ New Chat" used to change.
  const changeMode = useCallback(
    (mode: string, chatId?: string | null) => {
      send({ type: OUT.MODE_CHANGE, mode, ...(chatId ? { chat_id: chatId } : {}) })
    },
    [send],
  )

  const changeModel = useCallback(
    (model: string, chatId?: string | null) => {
      send({ type: OUT.MODEL_CHANGE, model, ...(chatId ? { chat_id: chatId } : {}) })
    },
    [send],
  )

  // Interactive CLI toggle. Persists the per-chat
  // execution_mode ('interactive' or '-p' for headless) to the chat row so
  // it survives reload/resume before the next send (mirrors changeModel). The
  // frame names its chat: the server writes nothing without a chat_id and
  // never substitutes the connection's bound chat (the last one opened on
  // this socket — a new-chat toggle used to rewrite the previous chat's mode).
  // A brand-new chat has no row and sends no frame; its mode rides the first
  // warmup. This path is persist-only; the live kill+rewarm on an
  // already-running session is handled by the live toggle below.
  const changeExecutionMode = useCallback(
    (executionMode: string, chatId: string) => {
      send({ type: OUT.EXECUTION_MODE_CHANGE, execution_mode: executionMode, chat_id: chatId })
    },
    [send],
  )

  // Live toggle: switch a LIVE chat between
  // interactive and headless -p — the backend kills the current session, reloads
  // the conversation (chat_history), and re-warms in the target mode resuming the
  // same JSONL, then emits warmup_ready{interactive} to drive the UI swap. Theme
  // is sent so an interactive re-warm seeds the TUI to match light/dark.
  const switchExecutionMode = useCallback(
    (executionMode: string, chatId: string, theme?: string) => {
      send({ type: OUT.EXECUTION_MODE_SWITCH, execution_mode: executionMode, chat_id: chatId, theme })
    },
    [send],
  )

  const compactContext = useCallback(
    () => {
      send({ type: OUT.COMPACT_CONTEXT })
    },
    [send],
  )

  // "Move this chat to the current target" — rebinds the connection's OPEN
  // chat to the agent's currently-resolved execution target (the pin is
  // cleared, a fresh session starts there, history reloads from DB). Acked
  // with chat_moved; refusals (mid-turn, no mismatch) arrive as standard
  // error frames.
  const moveChat = useCallback(
    () => {
      send({ type: OUT.MOVE_CHAT })
    },
    [send],
  )

  // Cross-engine resume: flip the OPEN chat's engine + model (dead sessions
  // only — the backend gate is authoritative). Acked with engine_switched
  // (direct + per-user broadcast); refusals arrive as switch_engine_denied.
  const switchEngine = useCallback(
    (executionPath: string, model: string) => {
      send({ type: OUT.SWITCH_ENGINE, execution_path: executionPath, model })
    },
    [send],
  )

  // Lazy liveness refresh for the bound chat (answered with a liveness
  // frame) — fired when the model dropdown opens, since headless idle-reap
  // emits no frame a viewer would see.
  const probeLiveness = useCallback(
    () => {
      send({ type: OUT.PROBE_LIVENESS })
    },
    [send],
  )

  const implementPlan = useCallback(
    (planPath: string, mode = 'acceptEdits') => {
      send({ type: OUT.IMPLEMENT_PLAN, plan_path: planPath, mode })
    },
    [send],
  )

  const resetStreaming = useCallback(() => {
    setStreaming(false)
    currentChatId.current = null
  }, [])

  const resumeChat = useCallback(
    (chatId: string, opts?: { delta?: boolean }) => {
      currentChatId.current = chatId
      setStreaming(false)  // Reset streaming state before switching chats
      // `delta`: the rows this view lacks are enough (the post-done refetch);
      // the server answers in full when it holds no base for the chat.
      send(opts?.delta
        ? { type: OUT.RESUME_CHAT, chat_id: chatId, delta: true }
        : { type: OUT.RESUME_CHAT, chat_id: chatId })
    },
    [send],
  )

  const sendChatRead = useCallback(
    (chatId: string) => {
      // Viewer actually saw the chat (open + focused) — clear the unread dot
      // locally and persist the read marker server-side (which echoes
      // chat_read to this user's other tabs / a shared chat's other users).
      useChatStore.getState().setUnread(chatId, false)
      send({ type: OUT.CHAT_READ, chat_id: chatId })
    },
    [send],
  )

  // By its id, with its index for a 1.7.0 proxy that reads only the index.
  const cancelQueued = useCallback(
    (key: { queueId?: string; index: number }, chatId?: string) => {
      const msg: Record<string, unknown> = { type: OUT.CANCEL_QUEUED, index: key.index }
      if (key.queueId) msg.queue_id = key.queueId
      if (chatId) msg.chat_id = chatId
      send(msg)
    },
    [send],
  )

  const cancelAllQueued = useCallback((chatId?: string) => {
    send(chatId ? { type: OUT.CANCEL_ALL_QUEUED, chat_id: chatId } : { type: OUT.CANCEL_ALL_QUEUED })
  }, [send])

  const abort = useCallback(() => {
    send({ type: OUT.ABORT })
    // Don't set streaming=false — wait for the CLI to finish cancellation
    // and send done/aborted event. The pump stays attached to receive it.
  }, [send])

  const disconnect = useCallback(() => {
    intentionalClose.current = true
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
    }
    if (pingTimer.current) {
      clearTimeout(pingTimer.current)
      pingTimer.current = null
    }
    send({ type: OUT.CLOSE })
    wsRef.current?.close()
    wsRef.current = null
    setConnected(false)
    setStreaming(false)
  }, [send])

  // Visibility change + Android onResume bridge — verify the WS is actually
  // alive when the app returns to foreground. Browsers report readyState=OPEN
  // even when the underlying TCP connection died (Android networks scrub idle
  // sockets aggressively, sometimes in <2s during a screen off cycle), so we
  // always send a ping/pong roundtrip on return. ~50-100ms cost; catches dead
  // WS within 4s and triggers reconnect → resume_chat → backend self-heal.
  useEffect(() => {
    const runHealthCheck = () => {
      const ws = wsRef.current
      if (healthCheckTimer.current) return  // already in-flight

      if (!ws || ws.readyState !== WebSocket.OPEN) {
        if (!intentionalClose.current) connect()
        return
      }

      pongReceived.current = false
      try {
        ws.send(JSON.stringify({ type: OUT.PING }))
      } catch {
        ws.close()
        return
      }
      healthCheckTimer.current = setTimeout(() => {
        healthCheckTimer.current = null
        if (!pongReceived.current) {
          ws.close()  // triggers onclose → auto-reconnect
        }
      }, 4000)
    }

    const onVisibilityChange = () => {
      const visible = document.visibilityState === 'visible'
      const ws = wsRef.current

      if (visible) {
        // Returning to foreground counts as user activity — clear the idle
        // flag and arm a fresh 5-min countdown.
        isIdleRef.current = false
        armIdleTimer()
      }

      // sendActiveState computes (visible && !idle) and routes the correct
      // user_active / user_idle message, deduped.
      sendActiveState(ws)

      if (visible) {
        // If the IANA timezone changed since last send, re-send client_info.
        // Covers laptop-closed-in-Athens / opened-in-NYC and similar travel cases.
        try {
          const currentTz = Intl.DateTimeFormat().resolvedOptions().timeZone
          if (currentTz && currentTz !== lastSentTz.current && ws?.readyState === WebSocket.OPEN) {
            sendClientInfo(ws)
          }
        } catch { /* ignore */ }
        runHealthCheck()
      }
    }

    // `otodock:force-health-check` is dispatched by MainActivity's onResume
    // observer (Android). visibilitychange is unreliable for short screen
    // off/on cycles where the WebView never enters the hidden state.
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('otodock:force-health-check', runHealthCheck)

    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('otodock:force-health-check', runHealthCheck)
      if (healthCheckTimer.current) clearTimeout(healthCheckTimer.current)
    }
  }, [connect, armIdleTimer, sendActiveState])

  // Idle detection — listen for any user input on the tab. Mouse / keyboard /
  // touch / scroll / focus all reset the 5-min idle clock. When the clock
  // fires we transition the connection to "idle"; the backend then routes
  // notifications to native push instead of an in-tab toast nobody is around
  // to hear. Same code runs in Capacitor WebView so Android benefits too.
  useEffect(() => {
    const onUserActivity = () => {
      const now = Date.now()
      // Throttle to once per second — mousemove can fire dozens of times per
      // second; we only need to bump the timer occasionally. Always handles
      // the wake-from-idle transition though (no throttle when isIdleRef is true).
      if (now - lastActivityResetAt.current < 1000 && !isIdleRef.current) return
      lastActivityResetAt.current = now

      if (isIdleRef.current) {
        isIdleRef.current = false
        sendActiveState(wsRef.current)
      }
      armIdleTimer()
    }

    const events: (keyof DocumentEventMap)[] = [
      'mousemove', 'mousedown', 'keydown', 'touchstart', 'wheel', 'scroll',
    ]
    events.forEach(e => document.addEventListener(e, onUserActivity, { passive: true }))
    window.addEventListener('focus', onUserActivity)

    // Arm the initial timer; it'll be re-armed on every input + on ws.onopen.
    armIdleTimer()

    return () => {
      events.forEach(e => document.removeEventListener(e, onUserActivity))
      window.removeEventListener('focus', onUserActivity)
      if (idleTimer.current) clearTimeout(idleTimer.current)
    }
  }, [armIdleTimer, sendActiveState])

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      intentionalClose.current = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (pingTimer.current) clearTimeout(pingTimer.current)
      if (healthCheckTimer.current) clearTimeout(healthCheckTimer.current)
      if (taskChatsInvalidateTimer.current) clearTimeout(taskChatsInvalidateTimer.current)
      wsRef.current?.close()
    }
  }, [])

  // Memoize the returned bundle so consumers that include `ws` in useEffect /
  // useCallback dep arrays don't see a fresh reference every render. Methods
  // are stable (useCallback-memoized above with stable deps via refs); only
  // `connected` and `streaming` are reactive — when either flips, callers
  // legitimately need to re-fire their `[ws, ...]` effects.
  return useMemo(() => ({
    connected,
    streaming,
    setStreaming,
    connect,
    preWarmup,
    warmup,
    sendMessage,
    sendPermission,
    sendLocationResponse,
    sendPlanReviewResponse,
    sendQuestionResponse,
    sendArtifactInteraction,
    sendAppAction,
    changeMode,
    changeModel,
    changeExecutionMode,
    switchExecutionMode,
    implementPlan,
    compactContext,
    moveChat,
    switchEngine,
    probeLiveness,
    resetStreaming,
    resumeChat,
    sendChatRead,
    cancelQueued,
    cancelAllQueued,
    abort,
    disconnect,
    subscribe,
    sendPtyAttach,
    sendPtyInput,
    sendPtyResize,
    sendPtyTakeover,
    sendPtyAttachments,
  }), [
    connected,
    streaming,
    setStreaming,
    connect,
    preWarmup,
    warmup,
    sendMessage,
    sendPermission,
    sendLocationResponse,
    sendPlanReviewResponse,
    sendQuestionResponse,
    sendArtifactInteraction,
    sendAppAction,
    changeMode,
    changeModel,
    changeExecutionMode,
    switchExecutionMode,
    implementPlan,
    compactContext,
    moveChat,
    switchEngine,
    probeLiveness,
    resetStreaming,
    resumeChat,
    sendChatRead,
    cancelQueued,
    cancelAllQueued,
    abort,
    disconnect,
    subscribe,
    sendPtyAttach,
    sendPtyInput,
    sendPtyResize,
    sendPtyTakeover,
    sendPtyAttachments,
  ])
}
