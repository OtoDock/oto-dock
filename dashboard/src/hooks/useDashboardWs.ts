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
import { useInstallStore } from '@/store/installStore'
import { useMachineUpdateStore } from '@/store/machineUpdateStore'
import { emitFileUpdate } from '../lib/fileUpdates'
import type { WsCallbacks } from './useDashboardWs.types'

/**
 * Frame types that belong to a single chat's stream. A frame carrying a
 * chat_id different from the viewed chat is dropped before dispatch. Untagged
 * frames pass (not every backend send site tags). Global frames (warmup_*,
 * install_*, chat_status, notifications, title_updated, file_updated,
 * chat_history — which has its own stale-guard) are never filtered, nor are
 * server_turn_start / user_message / plan_status, which may legitimately
 * target a non-viewed chat.
 */
const PER_CHAT_FRAMES = new Set([
  'text', 'thinking', 'tool_start', 'tool_info', 'tool_end', 'tool_result',
  'task_spawn', 'delegate_spawn', 'delegate_result', 'bg_agent_done',
  'bg_command_spawn', 'bg_command_done',
  'bg_agents_complete', 'bg_commands_complete', 'fg_agents_complete', 'workflow_start',
  'workflow_progress', 'workflow_end', 'permission_prompt', 'location_request',
  'plan_mode', 'plan_review', 'system', 'metadata', 'done', 'error', 'images',
  'video', 'audio', 'media_processing', 'media_failed', 'image_generating',
  'mcp_cost', 'image_gen_failed', 'limit_warning', 'limit_reached', 'url',
  'file', 'document_preview', 'mode_changed', 'model_changed',
  'thinking_changed', 'queued', 'queue_removed', 'queue_sent', 'queue_cleared',
  'queue_snapshot', 'steered', 'question', 'aborted', 'live_state', 'todo_update',
  'goal_update', 'context_compact', 'chat_rows', 'chat_meta',
  // Interactive CLI (PTY) frames — belong to one chat's terminal.
  'pty_output', 'pty_exit', 'pty_permission', 'pty_artifact', 'pty_status',
  // NOT 'turn_complete' — it is the origin-routed cross-chat end-of-turn
  // alert and deliberately targets chats this view is not showing.
])

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
    try { (window as any).Android?.setStreaming(value) } catch { /* not native */ }
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
        active ? { type: 'user_active' } : { type: 'user_idle', away: visible },
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
        type: 'client_info',
        platform: isNative ? 'android' : 'web',
        time_zone: tz,
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
        ws.send(JSON.stringify({ type: 'ping' }))
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

      // Tell backend what platform + IANA timezone we're on. Platform controls
      // ephemeral notification routing; time_zone snapshots onto the session
      // so scheduled tasks/notifications resolve in the user's local TZ.
      sendClientInfo(ws)

      // Immediately tell the backend the real visibility/idle state so it
      // doesn't sit on its default active=true assumption when the tab opens
      // already hidden or after we've been idle through a reconnect. Force-send
      // bypasses the dedup since this WS is a fresh connection.
      sendActiveState(ws, true)
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
          ws.send(JSON.stringify({ type: 'resume_chat', chat_id: cid }))
        }
      }
      reconnectedRef.current = true
    }

    ws.onclose = () => {
      const wasStreaming = streamingRef.current
      setConnected(false)
      setStreaming(false)
      wsRef.current = null
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
      let msg: any
      try {
        msg = JSON.parse(event.data)
      } catch {
        return  // non-JSON frame — ignore
      }
      try {
        const cb = callbacksRef.current
        // Per-chat stream frames for a chat this view is NOT showing are
        // dropped (cross-chat contamination guard). viewedChatId === null
        // means a brand-new chat with no id yet — every tagged stream frame
        // is foreign until warmup_started mints the id.
        if (cb.viewedChatId !== undefined && msg.chat_id
            && msg.chat_id !== cb.viewedChatId && PER_CHAT_FRAMES.has(msg.type)) {
          return
        }
        switch (msg.type) {
          case 'text':
            cb.onText?.(msg.content)
            break
          case 'thinking':
            cb.onThinking?.(msg)
            break
          case 'tool_start':
            cb.onToolStart?.(msg)
            break
          case 'tool_info':
            cb.onToolInfo?.(msg)
            break
          case 'tool_end':
            cb.onToolEnd?.(msg)
            break
          case 'task_spawn':
            cb.onTaskSpawn?.(msg)
            break
          case 'delegate_spawn':
            cb.onDelegateSpawn?.(msg)
            break
          case 'delegate_result':
            cb.onDelegateResult?.(msg)
            break
          case 'bg_agent_done':
            cb.onBgAgentDone?.(msg)
            break
          case 'bg_command_spawn':
            cb.onBgCommandSpawn?.(msg)
            break
          case 'bg_command_done':
            cb.onBgCommandDone?.(msg)
            break
          case 'bg_agents_complete':
            cb.onBgAgentsComplete?.(msg)
            break
          case 'bg_commands_complete':
            cb.onBgCommandsComplete?.(msg)
            break
          case 'workflow_start':
            cb.onWorkflowStart?.(msg)
            break
          case 'workflow_progress':
            cb.onWorkflowProgress?.(msg)
            break
          case 'workflow_end':
            cb.onWorkflowEnd?.(msg)
            break
          case 'fg_agents_complete':
            cb.onFgAgentsComplete?.()
            break
          case 'permission_prompt':
            cb.onPermissionPrompt?.(msg)
            break
          case 'location_request':
            cb.onLocationRequest?.(msg)
            break
          case 'plan_mode':
            cb.onPlanMode?.(msg)
            break
          case 'plan_review':
            cb.onPlanReview?.(msg)
            break
          case 'system':
            cb.onSystem?.(msg)
            break
          case 'metadata':
            cb.onMetadata?.(msg)
            break
          case 'done':
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
          case 'error':
            cb.onError?.(msg.message)
            break
          case 'images':
            cb.onImages?.(msg)
            break
          case 'video':
            cb.onVideo?.(msg)
            break
          case 'audio':
            cb.onAudio?.(msg)
            break
          case 'media_processing':
            cb.onMediaProcessing?.(msg)
            break
          case 'media_failed':
            cb.onMediaFailed?.(msg)
            break
          case 'image_generating':
            cb.onImageGenerating?.(msg)
            break
          case 'mcp_cost':
            cb.onMcpCost?.(msg)
            break
          case 'image_gen_failed':
            cb.onImageGenFailed?.()
            break
          case 'limit_warning':
            cb.onLimitWarning?.(msg)
            break
          case 'limit_reached':
            cb.onLimitReached?.(msg)
            break
          case 'url':
            cb.onUrl?.(msg)
            break
          case 'file':
            cb.onFile?.(msg)
            break
          case 'document_preview':
            cb.onDocumentPreview?.(msg)
            break
          case 'file_updated':
            // Fan out to any open Collabora preview (module bus) + let the host
            // invalidate the workspace file-tree query.
            emitFileUpdate(msg)
            cb.onFileUpdated?.(msg)
            break
          case 'pre_warmup_ready':
            cb.onPreWarmupReady?.(msg)
            break
          case 'warmup_ready':
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
          case 'warmup_started':
            // Order matters: transfer the new-chat draft FIRST so the slice
            // for the freshly-minted chat_id carries it; beginWarmup then
            // merges (does not replace) so the draft survives.
            if (msg.chat_id && msg.agent) {
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
          case 'warmup_heartbeat':
            useChatStore.getState().touchHeartbeat(msg.chat_id)
            break
          case 'warmup_failed':
            useChatStore.getState().failWarmup(msg.chat_id, msg.error || 'warmup failed')
            cb.onWarmupFailed?.(msg)
            break
          case 'chat_moved':
            // move_chat ack (the op acts on the connection's OPEN chat). The
            // consumer re-resumes the chat so the fresh warmup runs on the
            // new target and the "moved" history card arrives. Failures come
            // as standard error frames instead.
            cb.onChatMoved?.(msg)
            break
          // ── Install lifecycle (keyed by machine_id + agent, NOT chat_id) ──
          case 'install_started':
            useInstallStore.getState().begin(msg)
            break
          case 'install_mcp_plan':
            useInstallStore.getState().setPlan(msg)
            break
          case 'install_progress':
            useInstallStore.getState().recordProgress({
              machine_id: msg.machine_id,
              agent: msg.agent,
              mcp: msg.mcp,
              phase: msg.phase,
              pct: msg.pct,
              message: msg.message || '',
            })
            break
          case 'install_heartbeat':
            useInstallStore.getState().touchHeartbeat(msg)
            break
          case 'mcp_install_failed':
            useInstallStore.getState().recordFailure({
              machine_id: msg.machine_id,
              agent: msg.agent,
              mcp: msg.mcp,
              error: msg.error || 'install failed',
            })
            break
          case 'install_verifying':
            useInstallStore.getState().verifying(msg)
            break
          case 'install_done':
            useInstallStore.getState().finish(msg)
            break
          case 'install_failed':
            useInstallStore.getState().fail({
              machine_id: msg.machine_id,
              agent: msg.agent,
              error: msg.error || 'install failed',
            })
            break
          case 'satellite_updating':
            useMachineUpdateStore.getState().beginUpdate(msg)
            cb.onSatelliteUpdating?.(msg)
            break
          case 'satellite_updated':
            useMachineUpdateStore.getState().markUpdated(msg)
            cb.onSatelliteUpdated?.(msg)
            break
          case 'satellite_update_failed':
            useMachineUpdateStore.getState().markFailed(msg)
            cb.onSatelliteUpdateFailed?.(msg)
            break
          case 'satellite_update_sync':
            // Connect-time reconciliation: clears stale 'updating' banners whose
            // update finished while we were briefly disconnected (missed the
            // transient 'satellite_updated'), + surfaces any in-flight update.
            useMachineUpdateStore.getState().reconcile(msg.inflight || [])
            break
          case 'mode_changed':
            cb.onModeChanged?.(msg.mode)
            break
          case 'model_changed':
            cb.onModelChanged?.(msg.model)
            break
          case 'thinking_changed':
            cb.onThinkingChanged?.(msg.max_tokens)
            break
          case 'chat_history':
            cb.onChatHistory?.(msg)
            break
          case 'queued':
            cb.onQueued?.(msg)
            break
          case 'steered':
            // Mid-turn steer accepted by the engine — the message is part of
            // the RUNNING turn (no queue entry, no new turn starts).
            cb.onSteered?.(msg)
            break
          case 'queue_removed':
            // If server sends back text, it's for edit-return
            if (msg.text) {
              cb.onQueueEditReturn?.(msg)
            }
            cb.onQueueRemoved?.(msg)
            break
          case 'queue_sent': {
            setStreaming(true)  // Queue starts a new streaming turn
            // Key the slice by the FRAME's chat, not the viewed chat.
            const qsChatId = msg.chat_id || currentChatId.current
            if (qsChatId) useChatStore.getState().setStreaming(qsChatId)
            cb.onQueueSent?.(msg)
            break
          }
          case 'queue_cleared':
            // Backend confirmed all queue items cleared (from cancel_all_queued)
            break
          case 'queue_snapshot':
            // Backend-authoritative reconciliation on resume_chat — replaces
            // any reload-persisted queuedMessages with the pump's actual
            // queue. Strict replace, backend wins.
            if (msg.chat_id) {
              useChatStore.getState().setQueuedMessages(msg.chat_id, Array.isArray(msg.messages) ? msg.messages : [])
            }
            break
          case 'user_message': {
            // Backend-injected message starts a new streaming turn. Key the
            // slice by the FRAME's chat — a frame for a background chat must
            // never light the viewed chat's dot or flip its input state.
            const umChatId = msg.chat_id || currentChatId.current
            if (umChatId) useChatStore.getState().setStreaming(umChatId)
            if (!msg.chat_id || msg.chat_id === currentChatId.current) setStreaming(true)
            cb.onUserMessage?.(msg.content)
            break
          }
          case 'server_turn_start':
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
          case 'chat_status':
            // Authoritative per-chat live-dot signal, broadcast to ALL this user's
            // connections — lights/clears the sidebar dot for a chat generating
            // in the BACKGROUND, regardless of which chat this socket is viewing.
            if (msg.chat_id) {
              if (msg.status === 'streaming') {
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
                      && autoAttachRef.current !== viewed) {
                    autoAttachRef.current = viewed
                    ws.send(JSON.stringify({ type: 'resume_chat', chat_id: viewed }))
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
                if (!msg.chat_id.startsWith('task-')
                    && (msg.chat_id !== viewed || document.visibilityState === 'hidden')) {
                  useChatStore.getState().setUnread(msg.chat_id, true)
                }
              }
            }
            break
          case 'chat_status_snapshot': {
            // Connect-time authoritative "streaming right now" set — clears
            // stale streaming dots from missed frames and lights ones this
            // client never saw start (mirror of satellite_update_sync).
            const live = new Set<string>((msg.chat_ids as string[]) || [])
            const store = useChatStore.getState()
            for (const [cid, slice] of Object.entries(store.byChat)) {
              if (slice.status === 'streaming' && !live.has(cid)) store.setReady(cid)
            }
            for (const cid of live) store.setStreaming(cid)
            break
          }
          case 'chat_read':
            // Someone (this user's other tab, or any user of a shared-only
            // chat) opened the chat — drop the unread dot everywhere.
            if (msg.chat_id) useChatStore.getState().setUnread(msg.chat_id, false)
            break
          case 'plan_status':
            cb.onPlanStatus?.(msg)
            break
          case 'question':
            cb.onQuestion?.(msg)
            break
          case 'tool_result':
            cb.onToolResult?.(msg)
            break
          case 'title_updated':
            // Patch the Active-now seed in place: its rows render straight from
            // this cache, and a retitle landing AFTER the seed fetch (auto-title
            // of a still-streaming chat) otherwise shows the stale pre-title
            // until the next natural refetch.
            queryClient.setQueryData<ActiveChat[]>(['active-chats'], (old) =>
              old?.map((r) => (r.id === msg.chat_id ? { ...r, title: msg.title } : r)))
            cb.onTitleUpdated?.(msg)
            break
          case 'chat_rows':
            cb.onChatRows?.(msg)
            break
          case 'chat_meta':
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
          case 'aborted':
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
          case 'live_state': {
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
          case 'todo_update':
            cb.onTodoUpdate?.(msg)
            break
          case 'goal_update':
            cb.onGoalUpdate?.(msg)
            break
          case 'context_compact':
            cb.onContextCompact?.(msg)
            break
          case 'notification':
            cb.onNotification?.(msg)
            break
          case 'notification_silent':
            cb.onNotificationSilent?.(msg)
            break
          case 'notification_count':
            cb.onNotificationCount?.(msg)
            break
          case 'turn_complete':
            cb.onTurnComplete?.(msg)
            break
          case 'pong':
            pongReceived.current = true
            break
        }
        // Generic subscribers (after the switch + per-chat gating). A handler
        // throw is isolated per-subscriber so one bad listener can't drop the
        // frame for others.
        const subs = frameSubsRef.current.get(msg.type)
        if (subs) {
          for (const fn of Array.from(subs)) {
            try { fn(msg) } catch (e2) { console.error('[ws] subscriber failed:', msg?.type, e2) }
          }
        }
      } catch (e) {
        // A handler throw must not be silently swallowed: the frame is lost
        // either way, but losing it INVISIBLY turns real bugs into "the chat
        // froze" mysteries. Per-frame isolation — later frames still process.
        console.error('[ws] frame handler failed:', msg?.type, e)
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
      send({ type: 'pre_warmup', agent, model, permission_mode: permissionMode, execution_path: executionPath })
    },
    [send],
  )

  const warmup = useCallback(
    (agent: string, chatId?: string, permissionMode = 'default', model?: string, executionPath?: string,
     prompt?: { text: string; images?: Array<{ base64: string; name: string }>; files?: Array<{ path: string; name: string }> },
     executionMode?: string, theme?: string) => {
      if (chatId) currentChatId.current = chatId
      // Carry the first prompt WITH warmup so the backend persists it
      // at send-time and server-kicks the turn on warmup_ready (survives
      // navigate-away / refresh during spawn — no client-side deferred send).
      // execution_mode is the per-chat interactive override — the backend
      // resolver's chat_override.
      const msg: Record<string, unknown> = { type: 'warmup', agent, chat_id: chatId, permission_mode: permissionMode, model, execution_path: executionPath }
      if (executionMode) msg.execution_mode = executionMode
      if (theme) msg.theme = theme
      if (prompt?.text) {
        msg.text = prompt.text
        if (prompt.images?.length) msg.images = prompt.images.map(i => ({ data: i.base64, name: i.name }))
        if (prompt.files?.length) msg.files = prompt.files
      }
      send(msg)
    },
    [send],
  )

  const sendMessage = useCallback(
    (text: string, chatId: string, images?: Array<{ base64: string; name: string }>, files?: Array<{ path: string; name: string }>) => {
      setStreaming(true)
      useChatStore.getState().setStreaming(chatId)
      const msg: Record<string, unknown> = { type: 'chat', text, chat_id: chatId }
      if (images?.length) {
        msg.images = images.map(i => ({ data: i.base64, name: i.name }))
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
      send({ type: 'permission_response', request_id: requestId, approved })
    },
    [send],
  )

  const sendLocationResponse = useCallback(
    (requestId: string, result: { lat?: number; lng?: number; accuracy?: number; error?: string }) => {
      send({ type: 'location_response', request_id: requestId, ...result })
    },
    [send],
  )

  const sendPlanReviewResponse = useCallback(
    (requestId: string, action: string, filename?: string) => {
      send({ type: 'plan_review_response', request_id: requestId, action, filename: filename || '' })
    },
    [send],
  )

  const sendQuestionResponse = useCallback(
    (requestId: string, answers: Record<string, { answers: string[] }>) => {
      send({ type: 'question_response', request_id: requestId, answers })
    },
    [send],
  )

  // display_ui backchannel: an artifact's otodock.send payload, forwarded for
  // server-side validation + AskUserQuestion-style delivery. The server acks
  // with `artifact_ack` (generic subscribe registry — no switch case).
  const sendArtifactInteraction = useCallback(
    (chatId: string, token: string, title: string, payload: unknown) => {
      send({ type: 'artifact_interaction', chat_id: chatId, token, title, payload })
    },
    [send],
  )

  // Pinned mini-app send_prompt action — same delivery rails, gated on the
  // user-approved manifest server-side. Acked with `app_action_ack`.
  const sendAppAction = useCallback(
    (chatId: string, appId: string, actionId: string, args: unknown) => {
      send({ type: 'app_action', chat_id: chatId, app_id: appId, action_id: actionId, args })
    },
    [send],
  )

  // Interactive CLI (PTY) — subscribe to raw frames + send keystrokes/resize.
  const subscribe = useCallback((frameType: string, fn: (msg: any) => void) => {
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
      send({ type: 'pty_attach', chat_id: chatId })
    },
    [send],
  )

  const sendPtyInput = useCallback(
    (chatId: string, dataB64: string, composer = false) => {
      // `composer` marks a discrete chat-box send (vs raw terminal
      // keystrokes) — the backend holds flagged sends while the TUI is
      // parked on a question picker so the paste can't answer the dialog.
      send({ type: 'pty_input', chat_id: chatId, data: dataB64, ...(composer ? { composer: true } : {}) })
    },
    [send],
  )

  const sendPtyResize = useCallback(
    (chatId: string, rows: number, cols: number) => {
      send({ type: 'pty_resize', chat_id: chatId, rows, cols })
    },
    [send],
  )

  // Interactive CLI attachments: photos (base64) +
  // already-uploaded files. The backend saves the photos to the agent workspace
  // (same path as a normal chat turn), then types the prompt + the Read-tool
  // path references into the live PTY so the TUI's Read tool can open them.
  const sendPtyAttachments = useCallback(
    (chatId: string, text: string,
     images?: Array<{ base64: string; name: string }>,
     files?: Array<{ path: string; name: string }>) => {
      send({
        type: 'pty_attachments', chat_id: chatId, text,
        images: images?.map(i => ({ data: i.base64, name: i.name })),
        files,
      })
    },
    [send],
  )

  const changeMode = useCallback(
    (mode: string) => {
      send({ type: 'mode_change', mode })
    },
    [send],
  )

  const changeModel = useCallback(
    (model: string) => {
      send({ type: 'model_change', model })
    },
    [send],
  )

  // Interactive CLI toggle. Persists the per-chat
  // execution_mode ('interactive' or '' for headless `-p`) to the chat row so
  // it survives reload/resume before the next send (mirrors changeModel). Pass
  // chatId explicitly so a reopened dead chat persists even if the connection's
  // bound chat_id isn't set yet. This path is persist-only; the live
  // kill+rewarm on an already-running session is handled by the live toggle below.
  const changeExecutionMode = useCallback(
    (executionMode: string, chatId?: string) => {
      send({ type: 'execution_mode_change', execution_mode: executionMode, chat_id: chatId })
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
      send({ type: 'execution_mode_switch', execution_mode: executionMode, chat_id: chatId, theme })
    },
    [send],
  )

  const changeThinking = useCallback(
    (maxTokens: number | null) => {
      send({ type: 'thinking_change', max_tokens: maxTokens })
    },
    [send],
  )

  const compactContext = useCallback(
    () => {
      send({ type: 'compact_context' })
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
      send({ type: 'move_chat' })
    },
    [send],
  )

  const implementPlan = useCallback(
    (planPath: string, mode = 'acceptEdits') => {
      send({ type: 'implement_plan', plan_path: planPath, mode })
    },
    [send],
  )

  const resetStreaming = useCallback(() => {
    setStreaming(false)
    currentChatId.current = null
  }, [])

  const resumeChat = useCallback(
    (chatId: string) => {
      currentChatId.current = chatId
      setStreaming(false)  // Reset streaming state before switching chats
      send({ type: 'resume_chat', chat_id: chatId })
    },
    [send],
  )

  const sendChatRead = useCallback(
    (chatId: string) => {
      // Viewer actually saw the chat (open + focused) — clear the unread dot
      // locally and persist the read marker server-side (which echoes
      // chat_read to this user's other tabs / a shared chat's other users).
      useChatStore.getState().setUnread(chatId, false)
      send({ type: 'chat_read', chat_id: chatId })
    },
    [send],
  )

  const cancelQueued = useCallback(
    (index: number) => {
      send({ type: 'cancel_queued', index })
    },
    [send],
  )

  const cancelAllQueued = useCallback(() => {
    send({ type: 'cancel_all_queued' })
  }, [send])

  const abort = useCallback(() => {
    send({ type: 'abort' })
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
    send({ type: 'close' })
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
        ws.send(JSON.stringify({ type: 'ping' }))
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
    changeThinking,
    implementPlan,
    compactContext,
    moveChat,
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
    changeThinking,
    implementPlan,
    compactContext,
    moveChat,
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
    sendPtyAttachments,
  ])
}
