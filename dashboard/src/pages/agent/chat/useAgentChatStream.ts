/**
 * useAgentChatStream — AgentChat's useChatStream call with its options
 * object: the WS callback tails the page wires into the shared turn
 * lifecycle (warmup adoption + URL ownership, engine switch, liveness, the
 * rich-view refetch, chat_history normalisation, the find-bar deep link,
 * the end-of-turn pings), followed by the four WS-lifecycle effects that
 * only read the stream and the refs owned here: connect on mount, the
 * favorite agent's eager pre-warm, the preWarmedRef reset on WS disconnect,
 * and the sends typed during a warmup (useWarmupSends). Called where the page called
 * useChatStream, so the React hook order is unchanged.
 *
 * Cross-module ref plumbing is what makes a split of this page fragile, so
 * there is exactly one seam: the callbacks read the page values declared
 * AFTER this call — `interactive`, `draftKey`, `refetchChats` — through the
 * `latestRef` the page writes in a layout effect (`latestRef.current = {...}` right
 * after those values exist). useDashboardWs refreshes its callbacks every
 * render, so a callback always ran with the newest render's closure;
 * `latestRef.current` makes that same "read the latest state" guarantee
 * explicit across the module boundary. The stable refs the callbacks
 * share with the page's handlers are created here, before the call, and
 * returned — one instance each.
 */
import { useRef, useEffect, useState, useCallback, useLayoutEffect, useEffectEvent } from 'react'
import type { RefObject } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { NavigateFunction } from 'react-router-dom'
import { useChatStream } from '../../../hooks/useChatStream'
import { WARMUP_FAILED_REASON, WIRE } from '../../../api/wireEvents'
import { AUTO_REVIEW_MODE } from '../../../lib/engines/modes'
import type { useInteractiveChat } from '../../../hooks/useInteractiveChat'
import type { useChatNotifications } from '../../../hooks/useChatNotifications'
import type { useAgents } from '../../../api/agents'
import type { useChats } from '../../../api/chats'
import { fetchChatPage } from '../../../api/chats'
import { isDictationActive, onIdle as speechIdle } from '../../../audio/speechActivity'
import { useChatStore } from '../../../store/chatStore'
import type { useModelEngineSelection } from './useModelEngineSelection'
import type { useFindBar } from './useFindBar'
import { isTaskChatId } from '../../../lib/session/kind'
import { withWorkspaceParams } from '../../../lib/workspaceParams'
import { useWarmupSends, type HeldSend } from './useWarmupSends'
import { isNativePlatform } from '../../../audio/types'
import { chatTargetsKey } from '../../../api/remoteMachines'
import { emitAppToast } from '../../../lib/appToasts'

type Selection = ReturnType<typeof useModelEngineSelection>
type FindBar = ReturnType<typeof useFindBar>

/** Page values declared after the stream call that its callbacks read. */
export interface AgentChatLatest {
  interactive: ReturnType<typeof useInteractiveChat>
  draftKey: string
  refetchChats: ReturnType<typeof useChats>['refetch']
}

export function useAgentChatStream({
  agents, urlChatId, agentDefaultModel, agentExecutionPath, agentName, navigate, isFavoriteAgent, preWarmPath,
  warmingUp, setWarmingUp, chatNotif, sessionInteractiveRef, duplexActiveRef,
  setChatActiveLayer, setProcessAlive, setPendingEngineSwitch, setEngineSwitchBusy, setEngineSwitchError,
  pendingFindQueryRef, setFindInput, setFindQuery, setFindBarOpen, setDriveRefusal,
}: {
  agents: ReturnType<typeof useAgents>['data']
  urlChatId: string | undefined
  agentDefaultModel: string
  agentExecutionPath: string
  agentName: string | undefined
  navigate: NavigateFunction
  isFavoriteAgent: boolean
  preWarmPath: string
  warmingUp: boolean
  setWarmingUp: (v: boolean) => void
  chatNotif: ReturnType<typeof useChatNotifications>
  sessionInteractiveRef: RefObject<boolean>
  duplexActiveRef: RefObject<boolean>
  setChatActiveLayer: Selection['setChatActiveLayer']
  setProcessAlive: Selection['setProcessAlive']
  setPendingEngineSwitch: Selection['setPendingEngineSwitch']
  setEngineSwitchBusy: Selection['setEngineSwitchBusy']
  setEngineSwitchError: Selection['setEngineSwitchError']
  pendingFindQueryRef: FindBar['pendingFindQueryRef']
  setFindInput: FindBar['setFindInput']
  setFindQuery: FindBar['setFindQuery']
  setFindBarOpen: FindBar['setFindBarOpen']
  /** The drive gate's sentence for the chat that just loaded ("" when this
   *  person may drive it): the page locks its pickers with it. */
  setDriveRefusal: (refusal: { chatId: string; text: string } | null) => void
}) {
  // Written by the page in a layout effect (see the header); never read
  // before the first commit, so the placeholder is never observed.
  const latestRef = useRef<AgentChatLatest>(null as unknown as AgentChatLatest)
  // The place picked for the new chat this page composes ({agent, target};
  // null: where the agent's new chats start). State for the pill, a ref the
  // socket reads when it sends the chat's first frames.
  const [newChatPlace, setNewChatPlace] = useState<{ agent: string; target: string } | null>(null)
  const newChatPlaceRef = useRef(newChatPlace)
  const pickNewChatPlace = useCallback((agent: string, target: string | null) => {
    const next = target ? { agent, target } : null
    newChatPlaceRef.current = next
    setNewChatPlace(next)
  }, [])
  // The "Runs on" chip's query, refreshed when a move of the open chat ends.
  const queryClient = useQueryClient()
  // A phone call drives the viewed chat (chat_view_only): the banner shows
  // until the socket re-resumes the chat at the call's end.
  const [phoneCallActive, setPhoneCallActive] = useState(false)

  // The refs the callbacks share with the page's handlers.
  const chatRowsTimerRef = useRef<number | null>(null)
  const preWarmedRef = useRef<string | null>(null)
  // Tracks which chatId was last loaded so the URL-change effect doesn't double-resume
  // when in-component handlers (handleSelectChat) have already triggered the load.
  // Idempotent across StrictMode double-effects.
  const lastResumedChatIdRef = useRef<string | null>(null)
  // A failed or reset warmup hands the sends it held back to the composer
  // (useWarmupSends, created after the stream; the resets fire later).
  const warmupHandBackRef = useRef<() => void>(() => {})
  // Set when we send a prompt WITH warmup (server-kicked first turn). On
  // warmup_ready we adopt the server-driven turn into the streaming UI so the
  // stop button + timer + live generation engage (the client sent `warmup`,
  // not sendMessage, so nothing else flips ws.streaming). CHAT-TAGGED (null =
  // brand-new chat, matches the id the backend mints): adopt only the sending
  // chat's warmup_ready — after send→switch-away the flag would otherwise
  // survive (that chat's ready is dropped by the staleness guard) and flip a
  // phantom "generating" UI on the NEXT chat's resume. Cleared on chat switch.
  const serverKickPendingRef = useRef<false | { chatId: string | null }>(false)
  // Phone-mode chat entries (a wake hit or the mic-button duplex start from
  // the home page) KEEP the pinned-apps overlay showing (operator
  // 2026-08-15: spoken mode wants the dashboards visible; a manual close
  // still sticks). The ref is consumed by useAppsAutoOpen AT ITS CLOSE SITE
  // — the only ordering-proof spot: the duplex mint sets internal chat
  // state in one commit and react-router lands the URL rewrite in a later
  // startTransition commit, so any effect-side "re-open after close"
  // one-shot loses the race (live-hit 2026-08-14, the failed first fix).
  const keepAppsOnChatEntryRef = useRef(false)

  // Shared chat-stream state machine — messages + status + meeting state, the
  // entire `useDashboardWs` callback object, and the shared handlers. Returned
  // values are destructured into the same names the page used when this logic
  // lived inline. Page-specific lifecycle (warmup/pre-warmup, sidebar,
  // notifications, model dropdown, workspace, chatStore draft/queue) stays here
  // and is wired in through the options below. NOTE: the option callbacks below
  // and is wired in through the options below. NOTE: the option callbacks
  // below reference page values declared after this call (interactive,
  // draftKey, refetchChats) through `latestRef.current` — the page assigns it
  // every render, and the callbacks only run later, from WS events.
  const stream = useChatStream({
    agents,
    newChatTarget: (agent) => (newChatPlaceRef.current?.agent === agent
      ? newChatPlaceRef.current.target : undefined),
    defaultMode: AUTO_REVIEW_MODE,
    initialChatId: urlChatId || null,
    fallbackModel: agentDefaultModel,
    // A failed pre-warm on an EMPTY new chat still gets the red
    // target_unavailable card — the only error surface since the
    // duplicate "Warmup failed" strip was removed from InstallProgressBar.
    appendErrorOnEmptyWarmupFail: true,
    queue: {
      addQueued: (index, item) => { if (latestRef.current.draftKey) useChatStore.getState().addQueuedMessage(latestRef.current.draftKey, index, item) },
      clearQueued: () => { if (latestRef.current.draftKey) useChatStore.getState().clearQueuedMessages(latestRef.current.draftKey) },
      // A cancelled queued message hands its attachments back to the
      // composer: the photos by their saved path (re-sent in place, shown
      // through the agent files URL), or as the data a refused send carried
      // before anything was saved; the files as already-uploaded chips.
      restoreAttachments: (images, files) => {
        const key = latestRef.current.draftKey
        if (!key) return
        const st = useChatStore.getState()
        if (images.length) {
          st.addPendingImages(key, images.filter(i => i.path || i.base64).map((i, n) => ({
            id: i.path ? `img-${i.path}` : `img-returned-${n}-${i.name}`,
            path: i.path, base64: i.base64, name: i.name,
          })))
        }
        if (files.length) {
          st.addPendingFiles(key, files.map(f => ({
            id: `file-${f.path}`, name: f.name, size: 0, uploadedPath: f.path,
            ...(f.folder ? { folder: true } : {}),
          })))
        }
      },
    },
    clearQueueOnAbort: false,
    onWarmupRefetch: () => latestRef.current.refetchChats(),
    onWarmupReadyExtra: (data) => {
      setChatActiveLayer(data.execution_path || agentExecutionPath)  // Lock to chat's actual layer
      setWarmingUp(false)
      // Interactive CLI: a live PTY-backed session →
      // render the terminal, NOT the pump UI. Text-only cold sends ride the
      // warmup and the backend delivers them (submit_prompt / server kick);
      // the hook's flush + decline replay only remain for the attachments
      // stash. 'none' = not interactive: fall through to the server-kick.
      const interactiveStatus = latestRef.current.interactive.onWarmupReady(data, {
        onDecline: (t, cid) => { serverKickPendingRef.current = false; ws.sendMessage(t, cid) },
      })
      const kick = serverKickPendingRef.current
      if (interactiveStatus !== 'none') {
        serverKickPendingRef.current = false
      } else if (kick && (kick.chatId === null || kick.chatId === data.chat_id)) {
        // Adopt the server-kicked first turn into the streaming UI.
        // The backend runs the turn on warmup_ready, but the client sent `warmup`
        // (not sendMessage), so nothing flipped ws.streaming — do it here so the
        // stop button, timer, and live generation engage. turnStartTime is reset
        // to NOW (turn start) so the timer excludes the spawn window.
        serverKickPendingRef.current = false
        ws.setStreaming(true)
        if (data.chat_id) useChatStore.getState().setStreaming(data.chat_id)
        startTurnNow()
      }
      // The alive-session reuse paths emit warmup_ready WITHOUT a preceding
      // warmup_started (pre-warmed reuse is decided in the backgrounded spawn
      // tail, after the inline warmup_started), so own the URL here too.
      // Idempotent via lastResumedChatIdRef — the spawn path already
      // navigated at warmup_started. Only auto-own from the NEW-chat screen
      // (!urlChatId) — with a backgrounded spawn the
      // user may have switched to another chat, and warmup_ready for the
      // warmed chat must not yank them away (the chatId-staleness guard in
      // useChatStream already suppresses the rest of this handler in that case).
      if (data.chat_id && !urlChatId && lastResumedChatIdRef.current !== data.chat_id) {
        lastResumedChatIdRef.current = data.chat_id
        navigate(withWorkspaceParams(`/chat/${agentName}/${data.chat_id}`), { replace: true })
      }
    },
    onWarmupStartedExtra: (data) => {
      // The minted chat took the page's place pick: the next new chat starts afresh.
      if (data?.new_chat === true) {
        newChatPlaceRef.current = null
        setNewChatPlace(null)
      }
      latestRef.current.refetchChats()
      latestRef.current.interactive.switchStarted(data?.chat_id)
      // Own the URL as soon as the chat_id exists (during spawn), not at
      // warmup_ready — so a refresh/back re-resumes the in-flight warmup
      // instead of losing the chat.
      // lastResumedChatIdRef guards the URL-change effect from a redundant
      // reset+resumeChat (the warmup is already attached on this socket).
      // Gate on !urlChatId so a warmup_started never navigates
      // away from a chat the user is already viewing (warmup_started fires at
      // send-time from the NEW-chat screen; this just hardens against reorders).
      if (data?.chat_id && !urlChatId && lastResumedChatIdRef.current !== data.chat_id) {
        lastResumedChatIdRef.current = data.chat_id
        navigate(withWorkspaceParams(`/chat/${agentName}/${data.chat_id}`), { replace: true })
      }
    },
    onPreWarmupReady: (data) => { preWarmedRef.current = data.session_id },
    isWarmingUp: warmingUp,
    onWarmupReset: () => {
      setWarmingUp(false)
      warmupHandBackRef.current()
    },
    // A live switch refused before its re-warm started gets no warmup_ready:
    // its refusal unlocks the terminal toggle.
    onErrorExtra: (message) => latestRef.current.interactive.switchErrored(message),
    onWarmupFailedReset: (data) => {
      // Clear warmup state so input re-enables and the user can retry
      // (e.g. once the admin brings the satellite back online).
      setWarmingUp(false)
      // A place refused before the mint goes: the next send starts at the
      // default (the card says why).
      if (!data.chat_id && data.reason === WARMUP_FAILED_REASON.TARGET_UNAVAILABLE) {
        newChatPlaceRef.current = null
        setNewChatPlace(null)
      }
      latestRef.current.interactive.switchFailed()
      warmupHandBackRef.current()
      // A failed phone-mode mint must not leave the apps keep-open armed —
      // the NEXT ordinary chat entry would re-open a panel the user closed.
      keepAppsOnChatEntryRef.current = false
    },
    onTitleUpdated: () => latestRef.current.refetchChats(),
    onChatViewOnly: (data) => {
      // A phone call drives the viewed chat: the banner shows while the
      // history is read-only; the socket re-resumes the chat when the call
      // ends and clears this with null.
      setPhoneCallActive(data !== null)
    },
    onChatMoved: (data: { chat_id: string; ok?: boolean; message?: string }) => {
      // A move of the open chat ended (every tab viewing it hears it, the
      // mover's included): the backend closed the session and rebound the
      // pin (or, when the move failed, left it) — re-resume so the next
      // warmup runs where the chat now is and the "moved" history card
      // arrives (the same re-open path as clicking the chat row; no page
      // reset needed, the chat_history reload is wholesale anyway). The
      // "Runs on" chip reads its place again; a failed move says why.
      const cid = chatIdRef.current
      if (!cid) return
      void queryClient.invalidateQueries({ queryKey: chatTargetsKey(cid) })
      if (data.ok === false && data.message) {
        emitAppToast({ severity: 'warning', title: 'The chat was not moved', body: data.message })
      }
      setSessionId(null)
      latestRef.current.interactive.resetSession()  // the old session (interactive included) was closed server-side
      ws.resumeChat(cid)
    },
    onEngineSwitched: (data) => {
      // switch_engine ack (direct + per-user broadcast — idempotent): the
      // chat row now carries the new engine+model; re-home the local lock so
      // the next prompt warms up there with the DB-history digest.
      setChatActiveLayer(data.execution_path)
      setModel(data.model)
      setProcessAlive(false)
      setSessionId(null)
      // The rebind zeroed context_used server-side; clear the gauge
      // denominator too — the old engine's window would lie until the
      // first turn on the new engine reports its own.
      setContextMax(0)
      setPendingEngineSwitch(null)
      setEngineSwitchBusy(false)
      setEngineSwitchError(null)
      latestRef.current.refetchChats()
    },
    onSwitchEngineDenied: (data) => {
      // Rendered inside the switch dialog — the generic error rail only
      // renders into an open stream bubble, invisible on idle dead chats.
      setEngineSwitchBusy(false)
      setEngineSwitchError(data.message)
      if (typeof data.process_alive === 'boolean') setProcessAlive(data.process_alive)
    },
    onLiveness: (data) => {
      // probe_liveness answer (fired when the model dropdown opens).
      setProcessAlive(data.process_alive)
    },
    // New interactive-history rows persisted → refetch the newest page (same
    // seed path as the rich-view toggle). Applied in EVERY view, not just the
    // transcript: `messages` must stay current while the terminal is shown or
    // voice mode has nothing to speak (the list itself is hidden, seeding is
    // pure state). Trailing debounce coalesces the per-batch nudges a busy
    // turn produces.
    onChatRows: (data) => {
      if (!chatId || data.chat_id !== chatId) return
      if (chatRowsTimerRef.current) window.clearTimeout(chatRowsTimerRef.current)
      chatRowsTimerRef.current = window.setTimeout(async () => {
        chatRowsTimerRef.current = null
        try {
          const { messages: rows, has_more } = await fetchChatPage(chatId, 50)
          seedDbHistory(rows, has_more)
        } catch { /* transient — the next nudge retries */ }
      }, 800)
    },
    onChatHistoryMeta: (data) => {
      // Canonical-agent URL normalization: the chat row's agent (sent on
      // chat_history) is the agent of record, but the /chat/:name/:chatId
      // route trusts the slug — a deep-link/redirect with the wrong slug
      // rendered agent A's chat (live terminal included) inside agent B's
      // shell. Same-chatId navigate only swaps the slug: the URL-change
      // effect's lastResumedChatIdRef guard skips a redundant resume.
      if (data.agent && agentName && data.agent !== agentName && data.chat_id === urlChatId) {
        navigate(withWorkspaceParams(`/chat/${data.agent}/${data.chat_id}`), { replace: true })
      }
      // Restore execution layer + model from chat data (for resumed chats).
      if (data.execution_path) setChatActiveLayer(data.execution_path)
      if (data.model) setModel(data.model)
      // Best-effort session-process liveness (drives the cross-engine model
      // options). Absent (older proxy) reads alive → locked, the pre-feature
      // behavior. Any history (re)load also invalidates a provisional
      // engine-switch pick — the state it was judged against just reloaded.
      setProcessAlive(data.process_alive !== false)
      setPendingEngineSwitch(null)
      setEngineSwitchBusy(false)
      setEngineSwitchError(null)
      // Task chats store permission_mode 'auto' (the scheduler's posture) —
      // restore it so the status bar reflects the run's real mode (rendered
      // as Don't Ask) instead of this page's 'default' seed.
      // A chat whose pickers are locked below the editor tier shows the
      // chat's stored mode too (the person cannot pick one to correct it).
      if (data.mode && (isTaskChatId(data.chat_id) || data.drive_refusal)) setMode(data.mode)
      // Restore the per-chat interactive toggle from the stored execution_mode.
      // The live flag stays false until a warmup_ready{interactive} arrives — a
      // dead interactive chat shows its DB history with the toggle reflected on.
      latestRef.current.interactive.restoreFromMeta(data.execution_mode)
      setDriveRefusal(data.drive_refusal && data.chat_id
        ? { chatId: data.chat_id, text: data.drive_refusal } : null)
    },
    onExecutionModeChanged: (executionMode) => {
      latestRef.current.interactive.applyStoredExecMode(executionMode)
    },
    onChatHistoryLoaded: (_data) => {
      // Open find bar if deferred from URL ?q= param (after messages are loaded)
      if (pendingFindQueryRef.current) {
        const q = pendingFindQueryRef.current
        pendingFindQueryRef.current = null
        setFindInput(q)
        setFindQuery(q)
        setFindBarOpen(true)
      }
    },
    onTurnDone: ({ meetingActive, bgStillRunning }) => {
      // Play subtle ping on browser when the turn truly finishes. Skip during
      // meetings (turn transitions, not full completion) AND while a background
      // subagent is still running (the LLM just said "launched" — the genuine
      // completion is the nudge turn). Mirrors the backend fire_ephemeral guard.
      // Visible tab only — a hidden tab pings via onTurnComplete (never both).
      if (!meetingActive && !bgStillRunning && document.visibilityState === 'visible'
          && !duplexActiveRef.current && !isDictationActive()) {
        // Visible/foreground tab → in-app ping, on desktop AND the native app.
        // It's visibility-gated, so a BACKGROUNDED native app pings via FCM
        // instead (never both). Plays on the WebView media stream, so it's
        // audible even with the phone on silent (like a video's audio).
        // Suppressed during a duplex session: the chime rode on top of the
        // spoken reply and can leak into the open mic as a false barge-in.
        // Suppressed during DICTATION too (1.5): its capture runs without
        // AEC, so the chime would land in the transcript.
        chatNotif.playPing()
      }
      // Fire an interactive switch that was deferred so it wouldn't cut
      // this (now-finished) -p turn.
      latestRef.current.interactive.flushDeferredSwitch(chatIdRef.current)
      // Turn end arrives while the reply's TTS tail is still PLAYING in a
      // live voice session (the engine paces audio in real time, so several
      // seconds of speech follow the done event). The refetch triggers a
      // render burst that can starve the 250ms audio jitter buffer — the
      // operator-reported ~1s mid-sentence gap right as the chat flips to
      // finished. Nothing about the chat list is urgent while the agent is
      // literally mid-sentence: run it when speech goes idle (immediately
      // when nothing is speaking — the old fixed 2.5s only guessed the
      // tail and only covered duplex).
      speechIdle(() => latestRef.current.refetchChats())
    },
    onTurnComplete: (_data) => {
      // Origin-routed end-of-turn ping: a hidden tab or a background chat
      // (useChatStream already drops it for the visible viewed chat, which
      // onTurnDone pings). The native app alerts via FCM instead.
      try {
        if (isNativePlatform()) return
      } catch { /* not native */ }
      if (!duplexActiveRef.current && !isDictationActive()) chatNotif.playPing()
      speechIdle(() => latestRef.current.refetchChats())
    },
    enableDefensiveRefetch: true,
    // The defensive history refetch remounts the whole message list — a
    // hard cut for playing replay audio; wait out live speech.
    deferHeavy: speechIdle,
    isViewedChatPtyLive: () => sessionInteractiveRef.current,
    onNotification: chatNotif.onNotification,
    onNotificationSilent: chatNotif.onNotificationSilent,
    onNotificationCount: chatNotif.onNotificationCount,
  })
  // The stream values the callbacks above and the effects below read by name —
  // initialised right after the call, before any WS event can fire them.
  const {
    ws, setTurnStartTime, chatIdRef, setSessionId, setModel, setContextMax, chatId, seedDbHistory, setMode,
    sessionId, model, mode, setEditText,
  } = stream
  // The turn's clock starts at the adoption, not at the spawn.
  const startTurnNow = useCallback(() => setTurnStartTime(Date.now()), [setTurnStartTime])

  // Sends typed during "Getting ready": kept in order; what is still held
  // when the warmup fails goes back to the composer, attachments included.
  const warmupSends = useWarmupSends(ws, chatId, (held: HeldSend[]) => {
    setEditText(held.map((h) => h.text).filter(Boolean).join('\n\n'))
    const key = latestRef.current.draftKey
    if (!key) return
    const st = useChatStore.getState()
    const images = held.flatMap((h) => h.images ?? [])
    if (images.length) {
      st.addPendingImages(key, images.map((i, n) => ({
        id: `img-held-${n}-${i.name}`, name: i.name, base64: i.base64, path: i.path,
      })))
    }
    const files = held.flatMap((h) => h.files ?? [])
    if (files.length) {
      st.addPendingFiles(key, files.map((f) => ({
        id: `file-${f.path}`, name: f.name, size: 0, uploadedPath: f.path,
        ...(f.folder ? { folder: true } : {}),
      })))
    }
  })
  useLayoutEffect(() => { warmupHandBackRef.current = warmupSends.handBack })

  // --- Connect on mount ---

  useEffect(() => {
    ws.connect()
    return () => ws.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // "Shared with you" (SHARING.md): the frame reaches the notifications
  // hook through the socket's generic registry, the way any frame with no
  // option of its own does; the effect event sees the newest handler.
  const onShareInbox = useEffectEvent(chatNotif.onShareInbox)
  const wsSubscribe = ws.subscribe
  useEffect(() => wsSubscribe(WIRE.SHARE_INBOX, (msg) => {
    if (msg.type === WIRE.SHARE_INBOX) onShareInbox(msg)
  }), [wsSubscribe])

  // Eager pre-warmup: start MCP init when landing on the FAVORITE agent's
  // new-chat page. Non-favorite agents warm lazily on first interaction
  // (onEngage) so we don't spend a session+MCP-install slot on every agent
  // page the user merely passes through.
  useEffect(() => {
    if (!ws.connected || !agentName) return
    if (!isFavoriteAgent) return         // non-favorite → lazy (onEngage)
    if (urlChatId) return               // existing chat — resume path handles it
    if (warmingUp || sessionId) return   // already active
    if (preWarmedRef.current) return     // already pre-warmed
    ws.preWarmup(agentName, model, mode, preWarmPath)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.connected, agentName, urlChatId, isFavoriteAgent])

  // WS disconnect on the new-chat page: reset preWarmedRef so the eager
  // pre_warmup useEffect re-fires once the WS reconnects. Without this,
  // after a mobile network flap the new WS never attaches as install
  // listener and the user loses install-progress visibility. The
  // `otodock:ws-disconnect` event is dispatched by useDashboardWs's
  // ws.onclose.
  useEffect(() => {
    const onDisconnect = () => {
      preWarmedRef.current = null
    }
    window.addEventListener('otodock:ws-disconnect', onDisconnect)
    return () => window.removeEventListener('otodock:ws-disconnect', onDisconnect)
  }, [])

  return {
    ...stream,
    latestRef,
    chatRowsTimerRef, preWarmedRef, lastResumedChatIdRef,
    warmupSends, serverKickPendingRef, keepAppsOnChatEntryRef,
    phoneCallActive,
    newChatPlace, pickNewChatPlace,
  }
}
