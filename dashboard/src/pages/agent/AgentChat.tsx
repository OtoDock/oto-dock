/**
 * AgentChat — the main chat page. Intentionally kept as one file: its render
 * is woven through the useChatStream turn lifecycle (optimistic bubbles,
 * warmup, queueing, history paging, voice / terminal / artifact panels) whose
 * pieces share refs + closures that must read the latest state. Splitting it
 * would trade a smaller file for fragile cross-module ref plumbing. Sub-views
 * that DO stand alone were already extracted (ChatMessages, TopBar, ChatInput…).
 */
import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { Link, useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { SearchProvider } from '../../contexts/SearchContext'
import { useChatStream } from '../../hooks/useChatStream'
import { useAgents, useExecutionLayers, useAgentTargetStatus } from '../../api/agents'
import { useUserExecutionLayers } from '../../api/executionLayers'
import { useChats, useTaskChats, fetchChatPage } from '../../api/chats'
import { isDictationActive, onIdle as speechIdle } from '../../audio/speechActivity'
import { computeModelGroups, visibleAgentPaths } from '../../lib/modelGroups'
import { useRunByChat } from '../../api/runs'
import TaskMetadata from '../../components/chat/TaskMetadata'
import ChatMessages from '../../components/chat/ChatMessages'
import { ChatFileProvider } from '../../components/chat/ChatFileContext'
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'
import ChatInput, { PendingImage, PendingFile } from '../../components/chat/ChatInput'
import { enqueueChatUpload, dequeueChatUpload } from '../../lib/chatUploadQueue'
import TopBar from '../../components/chat/TopBar'
import AppSettingsModal from '../../components/chat/AppSettingsModal'
import { SetupBanner } from '../../components/PlatformSetupGuard'
import FindBar from '../../components/chat/FindBar'
import { useChatNotifications } from '../../hooks/useChatNotifications'
import { useSwipeGesture } from '../../hooks/useSwipeGesture'
import ChatHistory from '../../components/chat/ChatHistory'
import ActiveChatsPanel from '../../components/chat/ActiveChatsPanel'
import { useActiveChats } from '../../hooks/useActiveChats'
import { useAppsAutoOpen } from '../../hooks/useAppsAutoOpen'
import InstallProgressBar from '../../components/chat/InstallProgressBar'
import MachineUpdateBanner from '../../components/chat/MachineUpdateBanner'
import RemoteFallbackBanner from '../../components/chat/RemoteFallbackBanner'
import ChatTargetBanner from '../../components/chat/ChatTargetBanner'
import EngineSwitchBanner from '../../components/chat/EngineSwitchBanner'
import ChatStatusBar from '../../components/chat/ChatStatusBar'
import PlanPanel from '../../components/chat/plan/PlanPanel'
import TodoPanel from '../../components/chat/plan/TodoPanel'
import GoalPanel from '../../components/chat/plan/GoalPanel'
import WorkflowPanel from '../../components/chat/plan/WorkflowPanel'
import MeetingIndicator from '../../components/chat/MeetingIndicator'
import ResponsiveDrawer from '../../components/ui/ResponsiveDrawer'
import WorkspaceOverlay from '../../components/workspace/WorkspaceOverlay'
import AppsOverlay from '../../components/apps/AppsOverlay'
import ProjectsOverlay from '../../components/projects/ProjectsOverlay'
import { useWorkspaceState } from '../../hooks/useWorkspaceState'
import { canManageAgent, canEditAgent } from '../../lib/permissions'
import { hasAgentScope, isPersonalOnly, isSharedOnly, modeOfAgent } from '../../lib/visibility'
import { setNativeSwitchBusy } from '../../lib/nativeBridge'
import { useChatStore, newChatKey } from '../../store/chatStore'
import { useAgentPrefsStore } from '../../store/agentPrefsStore'
import { useHydrateUiPrefs } from '../../api/userUiPrefs'
import { useDuplexVoice } from '../../hooks/useDuplexVoice'
import { useChatAudioCapability } from '../../hooks/useChatAudioCapability'
import { useInteractiveChat, currentDashboardTheme, utf8ToB64, ptyPasteB64, withInteractiveTime } from '../../hooks/useInteractiveChat'
import { useQueryClient } from '@tanstack/react-query'
import { useApps, useChatPins, type PinnedApp } from '../../api/apps'
import { onFileUpdate } from '../../lib/fileUpdates'
import { apiFetch } from '../../api/auth'
import { buildAppActionText, substituteArgs } from '../../lib/artifactInteraction'
import { pushEscHandler } from '../../lib/escStack'
import { useArtifactWindows } from '../../hooks/useArtifactWindows'
import TerminalControlBar from '../../components/chat/terminal/TerminalControlBar'
import ArtifactDock from '../../components/chat/artifacts/ArtifactDock'

// Stable empty-array references for the chatStore selectors below. Zustand
// uses Object.is to detect selector-result changes — returning a fresh `[]`
// literal on every call ([] !== []) would trigger an infinite re-render
// loop (React error #185 "max update depth exceeded").
const EMPTY_QUEUED_MESSAGES: string[] = []
const EMPTY_PENDING_IMAGES: PendingImage[] = []
const EMPTY_PENDING_FILES: PendingFile[] = []

// Interactive CLI terminal — lazy so xterm + addons stay out of the main bundle,
// loaded only when a chat runs interactively.
const TerminalView = React.lazy(() => import('../../components/chat/terminal/TerminalView'))


export default function AgentChat() {
  const { name: agentName, chatId: urlChatId } = useParams<{
    name: string
    chatId?: string
  }>()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const { user, logout } = useAuth()
  const { data: agents, isError: agentsLoadFailed } = useAgents()
  // Roam the server-side ui-prefs bag (per-agent interactive pick) into
  // agentPrefsStore — once per session load (the guard lives in the store), so
  // a fresh device inherits the user's sticky mode instead of the default.
  useHydrateUiPrefs()
  // The favorite/landing agent (drives DefaultAgentRedirect). Only this agent
  // gets an EAGER pre-warm on the new-chat page; every other agent warms lazily
  // on the user's first composer interaction (see onEngage below).
  const isFavoriteAgent = !!agentName && agentName === (user?.default_agent || user?.agents?.[0])
  // Live status of the agent's admin-paired remote target — drives the
  // offline dot next to the agent name in the TopBar. Returns
  // `{state: null}` for agents that run locally or whose target is
  // user-paired, so the badge silently hides in those cases.
  const { data: agentTargetStatus } = useAgentTargetStatus(agentName ?? '')
  const { data: layers } = useExecutionLayers()
  const currentAgent = agents?.find(a => a.name === agentName)
  const agentExecutionPath = currentAgent?.execution_path || 'claude-code-cli'
  const agentExecutionPaths = currentAgent?.execution_paths || [agentExecutionPath]
  const agentLayerModels = agentExecutionPaths.flatMap(p => layers?.[p]?.models?.filter((m: { value: string }) => m.value !== '') || [])
  const agentDefaultModel = currentAgent?.default_model || ''
  const agentDisplayName = currentAgent?.display_name
  const agentColor = currentAgent?.color || ''

  // Per-user engine access (server-computed `can_run` on
  // /v1/users/me/execution-layers) — drives the engine/model filtering. A
  // layer absent from the map (older proxy / loading) counts as runnable.
  const { data: userLayers } = useUserExecutionLayers()
  const canRunLayer = useMemo(() => {
    const m: Record<string, boolean> = {}
    for (const l of userLayers ?? []) m[l.name] = l.can_run !== false
    return m
  }, [userLayers])
  // The agent's enabled engines this user can run — unfiltered fallback when
  // that would be empty (an empty selector is a dead-end; the inline notice
  // below explains instead).
  const visiblePaths = useMemo(
    () => visibleAgentPaths(agentExecutionPaths, canRunLayer),
    [agentExecutionPaths, canRunLayer],
  )
  const zeroAccessible =
    (userLayers?.length ?? 0) > 0
    && agentExecutionPaths.every(p => canRunLayer[p] === false)
  // Pre-warms target the primary engine unless this user can't run it — then
  // the first accessible one (a doomed pre-warm burns a spawn and fails
  // silently server-side).
  const preWarmPath = (canRunLayer[agentExecutionPath] ?? true)
    ? agentExecutionPath
    : (visiblePaths[0] ?? agentExecutionPath)

  // The execution layer the chat has COMMITTED to. null until the chat actually
  // starts (warmup_ready) or is restored from DB (chat_history) — only then does
  // the model dropdown lock to a single layer. While null (a fresh, unsent chat)
  // the dropdown shows every enabled layer so the user can pick any.
  const [chatActiveLayer, setChatActiveLayer] = useState<string | null>(null)
  // The layer of the currently-SELECTED model on a not-yet-committed chat (the
  // user's pick, or the default-model reconciliation). Drives the dropdown
  // highlight + the warmup layer WITHOUT collapsing the dropdown — that's what
  // keeps "all layers visible before the first prompt" working.
  const [selectedLayer, setSelectedLayer] = useState<string | null>(null)
  // Best-effort liveness of the chat's session process. Set from the
  // chat_history meta, warmup_ready (alive), engine_switched (dead) and the
  // probe_liveness answer (fired when the model dropdown opens — headless
  // idle-reap emits no frame). While true the dropdown stays locked to the
  // chat's engine; a DEAD chat additionally offers the agent's other
  // accessible engines (the cross-engine switch flow). The backend re-checks
  // authoritatively — this only shapes the dropdown.
  const [processAlive, setProcessAlive] = useState(true)
  // Provisional cross-engine pick (banner + confirm flow). Invariant: never
  // coexists with chatActiveLayer === null; cleared centrally on chat/agent
  // change, on every chat_history arrival, and when the chat revives.
  const [pendingEngineSwitch, setPendingEngineSwitch] =
    useState<{ layer: string; model: string } | null>(null)
  const [engineSwitchBusy, setEngineSwitchBusy] = useState(false)
  const [engineSwitchError, setEngineSwitchError] = useState<string | null>(null)
  // (modelGroups — the layer::model_id dropdown groups — is declared further
  // down, after the viewedStreaming/warming state it depends on; the math
  // itself lives in lib/modelGroups.)

  // Helper: parse layer::model compound value
  const parseModelValue = useCallback((compound: string): { layer: string; model: string } => {
    const sep = compound.indexOf('::')
    if (sep >= 0) return { layer: compound.slice(0, sep), model: compound.slice(sep + 2) }
    return { layer: agentExecutionPath, model: compound }
  }, [agentExecutionPath])

  // Find bar state
  const [findBarOpen, setFindBarOpen] = useState(false)
  const [findInput, setFindInput] = useState('')   // raw input (debounced before passing to context)
  const [findQuery, setFindQuery] = useState('')    // debounced query for SearchProvider
  const pendingFindQuery = useRef<string | null>(null)  // deferred until chat_history loads

  const [historyOpen, setHistoryOpen] = useState(() => window.innerWidth >= 768)
  const [warmingUp, setWarmingUp] = useState(false)
  // Live duplex session flag, readable from ws callbacks declared above the
  // hook itself (the hook lands further down; refs dodge the TDZ).
  const duplexActiveRef = useRef(false)
  // Interactive CLI state lives in
  // `useInteractiveChat`, created after useChatStream below since it needs `ws`.

  // Notification surface — inbox/toast/badge + per-severity sound + danger
  // alarm + Web Push / FCM + the deep-link routing rule.
  // Declared before useChatStream so its WS callbacks can be passed as the
  // notification options below.
  const chatNotif = useChatNotifications()

  // Live-PTY flag mirrored into a ref for useDashboardWs's auto-attach gate —
  // the interactive hook is created after useChatStream (it needs `ws`), so
  // callbacks reach the current value through this ref, never the closure.
  const sessionInteractiveRef = useRef(false)

  // Shared chat-stream state machine — messages + status + meeting state, the
  // entire `useDashboardWs` callback object, and the shared handlers. Returned
  // values are destructured into the same names the page used when this logic
  // lived inline. Page-specific lifecycle (warmup/pre-warmup, sidebar,
  // notifications, model dropdown, workspace, chatStore draft/queue) stays here
  // and is wired in through the options below. NOTE: the option callbacks below
  // reference page values declared after this call (draftKey, refetchChats,
  // notif, …) — that is safe because they are only invoked later, from WS
  // events, by which point those bindings are initialised.
  const {
    ws,
    messages, setMessages,
    loadOlder, hasMoreOlder, loadingOlder, seedDbHistory,
    chatId, setChatId, chatIdRef,
    sessionId, setSessionId,
    mode, setMode,
    model, setModel,
    sessionExecutionTarget,
    sessionFallbackReason,
    offlineMachineName,
    turnStartTime, setTurnStartTime,
    thinkingActive, setThinkingActive,
    compressingActive, setCompressingActive,
    activeAgents,
    totalCost, setTotalCost,
    contextUsed, setContextUsed,
    contextMax, setContextMax,
    cacheStats,
    permissionPending, setPermissionPending,
    aborting, setAborting,
    limitReached, setLimitReached,
    limitWarning, setLimitWarning,
    sessionPlans, setSessionPlans,
    currentTodos, setCurrentTodos,
    currentGoal, setCurrentGoal,
    workflows, setWorkflows,
    meetingActive, setMeetingActive,
    meetingParticipants, setMeetingParticipants,
    meetingSpeaker, setMeetingSpeaker,
    setMeetingRound,
    meetingLeftParticipants,
    editText, setEditText,
    dismissPreview,
    currentMsgRef, thinkingBufRef, abortedRef, discardingRef, sentWithBubbleRef, meetingSpeakerRef,
    handlePermissionRespond, handleQuestionAnswer, handleQuestionAnswerStructured, handleSendMessage,
    sendArtifactInteraction, sendAppAction,
    handleImplementPlan, handlePlanFetched, resolvePlanReview, finalizeAbortedTurn,
  } = useChatStream({
    agents,
    defaultMode: 'default',
    initialChatId: urlChatId || null,
    fallbackModel: agentDefaultModel,
    // A failed pre-warm on an EMPTY new chat still gets the red
    // target_unavailable card — the only error surface since the
    // duplicate "Warmup failed" strip was removed from InstallProgressBar.
    appendErrorOnEmptyWarmupFail: true,
    queue: {
      addQueued: (index, text) => { if (draftKey) useChatStore.getState().addQueuedMessage(draftKey, index, text) },
      clearQueued: () => { if (draftKey) useChatStore.getState().clearQueuedMessages(draftKey) },
    },
    clearQueueOnAbort: false,
    onWarmupRefetch: () => refetchChats(),
    onWarmupReadyExtra: (data) => {
      setChatActiveLayer(data.execution_path || agentExecutionPath)  // Lock to chat's actual layer
      setWarmingUp(false)
      // Interactive CLI: a live PTY-backed session →
      // render the terminal, NOT the pump UI. Text-only cold sends ride the
      // warmup and the backend delivers them (submit_prompt / server kick);
      // the hook's flush + decline replay only remain for the attachments
      // stash. 'none' = not interactive: fall through to the server-kick.
      const interactiveStatus = interactive.onWarmupReady(data, {
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
        setTurnStartTime(Date.now())
      }
      // Near-instant paths (pre-warmed reuse / alive-session reuse) emit
      // warmup_ready WITHOUT a preceding warmup_started, so own the URL here
      // too. Idempotent via lastResumedChatIdRef — the slow spawn path already
      // navigated at warmup_started. Only auto-own from the NEW-chat screen
      // (!urlChatId) — with a backgrounded spawn the
      // user may have switched to another chat, and warmup_ready for the
      // warmed chat must not yank them away (the chatId-staleness guard in
      // useChatStream already suppresses the rest of this handler in that case).
      if (data.chat_id && !urlChatId && lastResumedChatIdRef.current !== data.chat_id) {
        lastResumedChatIdRef.current = data.chat_id
        navigate(`/chat/${agentName}/${data.chat_id}`, { replace: true })
      }
    },
    onWarmupStartedExtra: (data) => {
      refetchChats()
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
        navigate(`/chat/${agentName}/${data.chat_id}`, { replace: true })
      }
    },
    onPreWarmupReady: (data) => { preWarmedRef.current = data.session_id },
    isWarmingUp: warmingUp,
    onWarmupReset: () => {
      setWarmingUp(false)
      pendingMessageRef.current = null
      pendingImagesRef.current = null
      pendingFilesRef.current = null
    },
    onWarmupFailedReset: () => {
      // Clear warmup state so input re-enables and the user can retry
      // (e.g. once the admin brings the satellite back online).
      setWarmingUp(false)
      pendingMessageRef.current = null
      pendingImagesRef.current = null
      pendingFilesRef.current = null
      // A failed phone-mode mint must not leave the apps keep-open armed —
      // the NEXT ordinary chat entry would re-open a panel the user closed.
      keepAppsOnChatEntryRef.current = false
    },
    onTitleUpdated: () => refetchChats(),
    onChatMoved: () => {
      // move_chat ack for the open chat: the backend rebound the pin and
      // dropped this connection's session binding — re-resume so the fresh
      // warmup runs on the new target and the "moved" history card arrives
      // (the same re-open path as clicking the chat row; no page reset
      // needed, the chat_history reload is wholesale anyway).
      const cid = chatIdRef.current
      if (!cid) return
      setSessionId(null)
      interactive.resetSession()  // the old session (interactive included) was closed server-side
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
      refetchChats()
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
        navigate(`/chat/${data.agent}/${data.chat_id}`, { replace: true })
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
      if (data.mode && data.chat_id?.startsWith('task-')) setMode(data.mode)
      // Restore the per-chat interactive toggle from the stored execution_mode.
      // The live flag stays false until a warmup_ready{interactive} arrives — a
      // dead interactive chat shows its DB history with the toggle reflected on.
      interactive.restoreFromMeta(data.execution_mode)
    },
    onChatHistoryLoaded: (_data) => {
      // Open find bar if deferred from URL ?q= param (after messages are loaded)
      if (pendingFindQuery.current) {
        const q = pendingFindQuery.current
        pendingFindQuery.current = null
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
      interactive.flushDeferredSwitch(chatIdRef.current)
      // Turn end arrives while the reply's TTS tail is still PLAYING in a
      // live voice session (the engine paces audio in real time, so several
      // seconds of speech follow the done event). The refetch triggers a
      // render burst that can starve the 250ms audio jitter buffer — the
      // operator-reported ~1s mid-sentence gap right as the chat flips to
      // finished. Nothing about the chat list is urgent while the agent is
      // literally mid-sentence: run it when speech goes idle (immediately
      // when nothing is speaking — the old fixed 2.5s only guessed the
      // tail and only covered duplex).
      speechIdle(() => refetchChats())
    },
    onTurnComplete: (_data) => {
      // Origin-routed end-of-turn ping: a hidden tab or a background chat
      // (useChatStream already drops it for the visible viewed chat, which
      // onTurnDone pings). The native app alerts via FCM instead.
      try {
        if ((window as any).Capacitor?.isNativePlatform?.()) return
      } catch { /* not native */ }
      if (!duplexActiveRef.current && !isDictationActive()) chatNotif.playPing()
      speechIdle(() => refetchChats())
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

  // Interactive CLI — per-chat toggle + live-PTY flag
  // + send/warmup routing. Created here (after
  // useChatStream) since it needs `ws`; the option callbacks above reference it
  // via closures that only fire on later WS events, by which point it's set.
  // Live rich view (onChatRows): the debounce timeout must read the CURRENT
  // toggle state, not its closure's render — mirror it in a ref.
  const showRichViewRef = useRef(false)
  const chatRowsTimerRef = useRef<number | null>(null)
  const interactive = useInteractiveChat(ws, currentAgent?.default_execution_mode || '')
  showRichViewRef.current = interactive.showRichView
  sessionInteractiveRef.current = interactive.sessionInteractive

  // Interactive-CLI display/file-tools artifact windows.
  // Lifted here so the minimized dock can render in the top-left panel stack
  // (below Todo/Workflow) while the open windows float inside TerminalView. The
  // empty chatId when not interactive clears the windows + drops the subscription.
  const artifacts = useArtifactWindows(ws, interactive.sessionInteractive && chatId ? chatId : '')

  // Compound model value for dropdown matching (layer::model_id)
  const modelCompound = `${chatActiveLayer || selectedLayer || agentExecutionPath}::${model}`
  // Plan mode is a Claude-Code-CLI-only feature — hide the option for Codex /
  // Direct LLM (their layers declare supports_plan_mode=false). Gate on the
  // effective layer (committed → selected → agent default).
  const effectiveLayer = chatActiveLayer || selectedLayer || agentExecutionPath
  const supportsPlanMode = layers?.[effectiveLayer]?.supports_plan_mode ?? true
  // Interactive CLI: show the toggle when the agent has
  // an interactive-capable CLI layer (claude-code-cli OR codex-cli). Gated on
  // AGENT capability — not the selected model — so it stays put when a direct-llm
  // model is picked (the toggle is simply ignored for that layer). Hidden for
  // direct-llm-only agents, and platform-wide when the interactive
  // kill-switch is off (sessions always spawn headless then).
  const interactiveAvailable =
    (agentExecutionPaths.includes('claude-code-cli') || agentExecutionPaths.includes('codex-cli'))
    && user?.feature_flags?.interactive_terminal_enabled !== false
  // The toggle is free to flip any time EXCEPT while a cold-start is warming or a
  // live switch (kill+rewarm) is in flight — both would race a second
  // toggle. A live session is switchable (via confirm).
  const interactiveLocked = warmingUp || interactive.switching
  // Invariant: never leave the permission mode on "plan" for a layer that
  // doesn't support it (Codex / Direct LLM) — covers a model switch and a
  // stale per-agent sticky "plan" being restored onto such a layer.
  useEffect(() => {
    if (mode === 'plan' && !supportsPlanMode) setMode('default')
  }, [mode, supportsPlanMode, setMode])

  // A live meeting/voice session would be lost on an install switch — flag the
  // native switcher so it confirms first (LLM streaming is reported separately).
  useEffect(() => {
    setNativeSwitchBusy(meetingActive)
    return () => setNativeSwitchBusy(false)
  }, [meetingActive])
  const preWarmedRef = useRef<string | null>(null)
  // Tracks which chatId was last loaded so the URL-change effect doesn't double-resume
  // when in-component handlers (handleSelectChat) have already triggered the load.
  // Idempotent across StrictMode double-effects.
  const lastResumedChatIdRef = useRef<string | null>(null)
  // Which agent's NEW-chat view already had its exec-mode reset+seed (the
  // lastResumedChatIdRef twin for the !urlChatId branch). Guards the URL
  // effect's reset to once per new-chat visit; handleNewChat — which
  // resets+seeds inline — pre-stamps it to skip the duplicate.
  const newChatModeSeededRef = useRef<string | null>(null)
  const pendingFilesRef = useRef<Array<{ path: string; name: string }> | null>(null)

  // Draft text — persisted to localStorage so it survives chat nav + reload.
  // On the new-chat page (no chat_id yet), the slice is keyed by a synthetic
  // `__new__:<agent>` id; warmup_started transfers it onto the real chat_id.
  const draftKey = chatId ?? (agentName ? newChatKey(agentName) : '')
  const draftInput = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.draftInput ?? '' : ''))
  // "Getting ready…" badge signal — true from send until warmup_ready. warmingUp
  // is the local send-time flag; the chatStore 'warming' status also covers a
  // resumed in-flight warmup after a refresh/navigate.
  const warmingStatus = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.status === 'warming' : false))
  const warming = warmingUp || warmingStatus
  // Stop button / live-input state derive from the VIEWED chat's slice (per-chat),
  // NOT connection-global ws.streaming — else the stop button + "type to queue" leak
  // onto whatever chat you switch to while another streams. The slice is set
  // 'streaming' by every turn-start path (user_message / queue_sent / server_turn_start
  // / live_state / server-kick) and back to 'ready' on done/aborted, keyed by chat_id.
  const viewedStreaming = useChatStore((s) => (chatId ? s.byChat[chatId]?.status === 'streaming' : false))
  // Pin-vs-current target mismatch for the VIEWED chat. Read from the
  // per-chat slice (warmup_ready stores it there) rather than useChatStream
  // state so the sidebar kebab reads the exact same fact — and the slice is
  // cleared by the first mismatch-free warmup_ready, e.g. after a move.
  const targetMismatch = useChatStore((s) => (chatId ? s.byChat[chatId]?.targetMismatch ?? null : null))

  // Model dropdown groups — locked to the chat's engine while its session
  // process is alive/streaming/warming; a DEAD chat's dropdown adds the
  // agent's other engines this user can run (cross-engine switch flow).
  const modelGroups = useMemo(() => computeModelGroups({
    layers,
    agentPaths: agentExecutionPaths,
    chatActiveLayer,
    processAlive,
    canRun: canRunLayer,
    streaming: viewedStreaming,
    warming,
    activeModel: model,
  }), [layers, agentExecutionPaths, chatActiveLayer, processAlive, canRunLayer, viewedStreaming, warming, model])

  // Centralized pendingEngineSwitch reset — the ONLY chat-exit clear. The
  // sidebar select path (handleSelectChat) pre-stamps lastResumedChatIdRef
  // and SKIPS the URL-effect reset, so per-path sprinkles would leak the
  // banner + disabled composer onto the next chat.
  useEffect(() => {
    setPendingEngineSwitch(null)
    setEngineSwitchBusy(false)
    setEngineSwitchError(null)
  }, [chatId, agentName])
  // A second tab / server-initiated turn revived the chat under an open
  // banner — the dropdown collapses back to the active engine; drop the
  // provisional pick instead of deadlocking send against a hidden group.
  useEffect(() => {
    if (viewedStreaming || warming) {
      setPendingEngineSwitch(null)
      setEngineSwitchBusy(false)
      setEngineSwitchError(null)
    }
  }, [viewedStreaming, warming])
  // warmup_ready adopted a session — the process is alive again.
  useEffect(() => { if (sessionId) setProcessAlive(true) }, [sessionId])

  // Read tracking for the sidebar unread dot: the viewer has SEEN this chat
  // whenever it is open in a visible tab — on open, when the viewed turn
  // finishes on-screen, and when the tab returns to the foreground. The
  // backend persists the marker (per owner identity — shared-only chats
  // clear for everyone) and echoes chat_read to other tabs/users.
  useEffect(() => {
    if (!chatId) return
    const markRead = () => {
      if (document.visibilityState === 'visible') {
        try { ws.sendChatRead(chatId) } catch { /* best-effort */ }
      }
    }
    markRead()
    document.addEventListener('visibilitychange', markRead)
    return () => document.removeEventListener('visibilitychange', markRead)
    // viewedStreaming in deps: re-fires when the viewed turn ends, so a
    // response the user watched arrive never counts as unread.
  }, [chatId, viewedStreaming])  // eslint-disable-line react-hooks/exhaustive-deps

  // Full-duplex conversation mode (phone engine) — the only live voice mode
  // (the half-duplex hands-free loop was retired in its favor; the mic is
  // plain dictation now).
  const { data: audioCapability } = useChatAudioCapability()
  const duplexVoice = useDuplexVoice(chatId)
  // Fresh-chat start: a duplex session attaches to an existing chat+session,
  // so on a never-warmed chat the toggle first fires the normal warmup (the
  // same one the first message runs) and starts the session once it lands.
  const duplexPendingRef = useRef(false)
  useEffect(() => {
    if (duplexPendingRef.current && chatId && !warming) {
      duplexPendingRef.current = false
      duplexVoice.start()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, warming])
  // Exit note: after a live conversation ends, the next TYPED message IN THAT
  // CHAT carries a short bracketed note so the engine drops the spoken-reply
  // style (the duplex context told it the user hears replies aloud — that
  // stopped being true the moment the session closed). Bound to the chat the
  // session lived in: an armed note must never leak into another chat (it
  // showed up as the first message of a brand-new chat in live testing).
  const duplexLiveChatRef = useRef<string | null>(null)
  const duplexExitNoteRef = useRef<string | null>(null)
  // A session that never ran a turn (failed connect, instant close) never
  // injected the spoken-mode context — a note would be wrong. 'thinking'
  // marks the first dispatched utterance.
  const duplexSawTurnRef = useRef(false)
  useEffect(() => {
    if (duplexVoice.phase === 'thinking') duplexSawTurnRef.current = true
  }, [duplexVoice.phase])
  useEffect(() => {
    duplexActiveRef.current = duplexVoice.active
    if (duplexVoice.active) {
      // Captured at activation: on a chat switch the teardown flips active
      // AFTER chatId already points at the new chat, so reading chatId at
      // deactivation would bind the note to the wrong chat.
      duplexLiveChatRef.current = chatId ?? null
      duplexSawTurnRef.current = false
    } else if (duplexLiveChatRef.current) {
      // No note when the AGENT closed the session ([DUPLEX_COMPLETE]
      // farewell) — it knows the spoken mode ended; the note is only for
      // exits the agent didn't see (user toggle, idle timeout, drops).
      if (duplexSawTurnRef.current && !duplexVoice.endedByAgent) {
        duplexExitNoteRef.current = duplexLiveChatRef.current
      }
      duplexLiveChatRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [duplexVoice.active])
  const setDraftInput = useCallback(
    (text: string) => {
      if (!draftKey) return
      useChatStore.getState().setDraftInput(draftKey, text)
    },
    [draftKey],
  )

  // Queue + pending attachments. Source of truth is
  // chatStore (per-chat slice keyed by draftKey). queuedMessages persists
  // to localStorage; the backend re-syncs with a queue_snapshot event on
  // resume_chat so any drift is reconciled. Pending images / files are
  // in-memory only.
  const queuedMessages = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.queuedMessages ?? EMPTY_QUEUED_MESSAGES : EMPTY_QUEUED_MESSAGES))
  const pendingImages = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.pendingImages ?? EMPTY_PENDING_IMAGES : EMPTY_PENDING_IMAGES))
  const pendingFiles = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.pendingFiles ?? EMPTY_PENDING_FILES : EMPTY_PENDING_FILES))

  // Seed model + mode on initial render / new chat.
  // For NEW chats (no urlChatId), the user's per-agent sticky preference
  // wins — opening a new chat for an agent reuses the last pick instead
  // of resetting to default. For EXISTING chats (urlChatId set), the DB
  // value from chat_history later overrides this seed.
  useEffect(() => {
    if (!agentName) return
    if (urlChatId) return  // existing chat; chat_history will set model/mode
    const prefs = useAgentPrefsStore.getState()
    const stickyModel = prefs.lastModel[agentName]
    const stickyMode = prefs.lastMode[agentName]
    // Unconditional re-seed: navigating here from an EXISTING chat leaves its
    // restored model/mode in state — for a task-run chat that's the run's
    // model + 'auto', which must never carry into a new chat ('auto' would
    // spawn it with Don't Ask). The sticky prefs hold the user's own last
    // explicit picks (handleModeChange/handleModelChange save every change),
    // so re-seeding never loses a real choice.
    setModel(stickyModel || agentDefaultModel)
    setMode(stickyMode || 'default')
    // (The interactive toggle's sticky seed lives in the dedicated effect
    // below — it must also re-fire when the ui-prefs hydration lands.)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, urlChatId, agentDefaultModel])

  // Seed the interactive toggle from the agent's sticky preference, so a new
  // chat (or a page refresh on /chat/<agent>) keeps the user's last
  // interactive on/off choice. SUBSCRIBED (not a getState snapshot): on a
  // fresh device the sticky map is hydrated from the server ui-prefs AFTER
  // mount, and the seed must re-fire when the roamed value lands. No-op
  // unless an explicit value was set; a manual toggle rewrites the sticky to
  // the value it just set, so the re-fire is idempotent.
  const stickyInteractive = useAgentPrefsStore((s) => (agentName ? s.lastInteractive[agentName] ?? '' : ''))
  useEffect(() => {
    if (!agentName || urlChatId) return
    interactive.seedExecMode(stickyInteractive)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, urlChatId, stickyInteractive])

  // Reconcile model ↔ chatActiveLayer for NEW chats. The seed effect above
  // sets ``model`` (a bare model_id) but doesn't set ``chatActiveLayer``,
  // and ``modelCompound`` falls back to ``agentExecutionPath`` (primary)
  // when chatActiveLayer is null. That breaks two scenarios:
  //
  //   1. Agent has multiple execution_paths enabled (e.g. codex-cli +
  //      claude-code-cli) and default_model belongs to the SECONDARY layer.
  //      Compound becomes ``<primary>::<secondary-model>`` → no match in
  //      ``modelGroups`` → dropdown renders empty → user clicks Send →
  //      ``ws.warmup`` runs with the wrong execution_path → backend
  //      resolves nothing → message vanishes with no response.
  //
  //   2. Agent has no ``default_model`` set at all. Seed leaves ``model``
  //      as empty string. Compound becomes ``<primary>::`` → no match →
  //      same silent failure on send.
  //
  // Once a chat is active (chatActiveLayer set by warmup_ready or
  // chat_history) this effect bails. Same for existing chats — those
  // restore both model + layer from DB and we shouldn't second-guess.
  useEffect(() => {
    if (urlChatId) return
    if (chatActiveLayer) return    // committed → the layer is already locked
    if (selectedLayer) return      // already reconciled / the user already picked
    // Wait until the agents query resolves. Before it does, agentDefaultModel
    // is "" and ``model`` may be "" — picking firstGroup.models[0] here (the
    // primary layer's first model, e.g. opus-4.7) and persisting it as sticky
    // would override the agent's real default for every future new chat (G1).
    if (!currentAgent) return
    if (!modelGroups || modelGroups.length === 0) return

    // Find the group whose layer owns the current model. Set the SELECTED layer
    // (NOT the committed chatActiveLayer) so warmup uses the right layer while
    // the dropdown keeps showing every enabled layer until the chat starts.
    const matching = modelGroups.find(g =>
      g.models.some(m => m.value === `${g.layer}::${model}`),
    )
    if (matching) {
      setSelectedLayer(matching.layer)
      return
    }

    // No group owns the current model — the agent has no default_model, or its
    // default belongs to a disabled layer. Pick the first available so the
    // dropdown has a valid selection, but do NOT persist it as the sticky pref:
    // this is an automatic reconciliation, not a user choice (handleModelChange
    // is the only place a pick becomes sticky). Persisting here is G1 poisoning.
    const firstGroup = modelGroups[0]
    const firstOption = firstGroup?.models[0]
    if (!firstOption) return
    const { layer, model: modelId } = parseModelValue(firstOption.value)
    setModel(modelId)
    setSelectedLayer(layer)
  }, [urlChatId, chatActiveLayer, selectedLayer, modelGroups, model, agentName, agentDefaultModel, currentAgent, parseModelValue])

  const [appSettingsOpen, setAppSettingsOpen] = useState(false)
  const isNative = !!(window as any).Capacitor?.isNativePlatform?.()

  // Swipe gestures for chat history drawer (mobile only)
  const swipeRef = useRef<HTMLDivElement>(null)
  useSwipeGesture(swipeRef, {
    onSwipeRight: () => { if (!historyOpen) setHistoryOpen(true) },
    onSwipeLeft: () => { if (historyOpen) setHistoryOpen(false) },
  })

  // Warmup-pending message + attachments (held while the session spins up;
  // sent by the post-warmup effect once sessionId + chatId land).
  const pendingMessageRef = useRef<string | null>(null)
  const pendingImagesRef = useRef<Array<{ base64: string; name: string }> | null>(null)
  // Set when we send a prompt WITH warmup (server-kicked first turn). On
  // warmup_ready we adopt the server-driven turn into the streaming UI so the
  // stop button + timer + live generation engage (the client sent `warmup`,
  // not sendMessage, so nothing else flips ws.streaming). CHAT-TAGGED (null =
  // brand-new chat, matches the id the backend mints): adopt only the sending
  // chat's warmup_ready — after send→switch-away the flag would otherwise
  // survive (that chat's ready is dropped by the staleness guard) and flip a
  // phantom "generating" UI on the NEXT chat's resume. Cleared on chat switch.
  const serverKickPendingRef = useRef<false | { chatId: string | null }>(false)
  // A first send held because the AGENTS QUERY hadn't resolved yet (no
  // explicit per-chat mode → the effective interactive mode is unknowable
  // until the agent default arrives). The bubble + warming badge are shown at
  // hold time; the flush effect below routes it once the query settles.
  const heldForAgentsRef = useRef<{
    text: string
    images?: Array<{ base64: string; name: string }>
    files?: Array<{ path: string; name: string }>
  } | null>(null)

  // Poll paused while the sidebar's inline rename is open (a reorder from a
  // refetch blurs the input into a half-typed commit — see useChats) AND
  // while a duplex session is live (the 30s poll can land mid-reply and
  // feed the render burst that starves TTS scheduling; turn end refetches
  // via speechIdle anyway).
  const [renameEditing, setRenameEditing] = useState(false)
  const { data: chats, refetch: refetchChats } = useChats(
    agentName, renameEditing || duplexVoice.active)

  // ---- Task mode ----
  // Task runs render on this page: a `task-…` chat id marks the open chat as
  // a task-run chat, and its latest run feeds the pinned TaskMetadata popup.
  // The sidebar's Task history toggle is page state so ?tasks=1 deep links
  // (notifications, the /runs resolver, Active-now task rows) open with the
  // task view on.
  const isTaskChat = !!chatId?.startsWith('task-')
  const { data: taskRun } = useRunByChat(isTaskChat ? chatId : null)
  const [tasksMode, setTasksMode] = useState(() => searchParams.get('tasks') === '1')
  useEffect(() => {
    if (searchParams.get('tasks') === '1') setTasksMode(true)
  }, [searchParams])
  // The task-chat list also backs the row lookups below (origin / delegation
  // markers) — task chats never appear in the chat-mode list.
  const { data: taskChats } = useTaskChats(
    agentName, tasksMode || isTaskChat, duplexVoice.active)

  // ---- Workspace overlay ----
  // Lifted to this page so the overlay can swap the message-area while
  // keeping TopBar, status bar, and ChatInput visible. State is persisted
  // per-agent so chat switches and auto-close-on-send don't lose the user's
  // folder. Reset on agent switch.
  const lastAssistantMessageId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return messages[i].id
    }
    return null
  }, [messages])
  const workspace = useWorkspaceState(agentName ?? '', lastAssistantMessageId)
  const canManageThisAgent =
    !!user && !!agentName && canManageAgent(user, agentName)
  const canEditThisAgent =
    !!user && !!agentName && canEditAgent(user, agentName)

  // ---- Pinned mini-apps overlay ----
  // Permanent agent-level surface (standing dashboards), toggled from the
  // composer button right of the workspace toggle. Slot precedence:
  // workspace > apps > projects.
  const [appsOpen, setAppsOpen] = useState(false)
  const { data: pinnedApps } = useApps(agentName ?? '')
  const appsActive = appsOpen && !workspace.state.open && !!agentName
  // Agent HOME live-sessions strip (operator call 2026-07-11): on the front
  // page (no chat open) the cross-agent "Active now" rows show permanently
  // on top — dashboards/landing render below. Hook is enabled only there;
  // it returns [] otherwise, so nothing extra runs inside chats.
  const homeActiveRows = useActiveChats(!chatId && !!agentName)
  const showHomeActive =
    !chatId && !!agentName && homeActiveRows.length > 0 && !workspace.state.open
  // Phone-mode chat entries (a wake hit or the mic-button duplex start from
  // the home page) KEEP the pinned-apps overlay showing (operator
  // 2026-08-15: spoken mode wants the dashboards visible; a manual close
  // still sticks). The ref is consumed by useAppsAutoOpen AT ITS CLOSE SITE
  // — the only ordering-proof spot: the duplex mint sets internal chat
  // state in one commit and react-router lands the URL rewrite in a later
  // startTransition commit, so any effect-side "re-open after close"
  // one-shot loses the race (live-hit 2026-08-14, the failed first fix).
  const keepAppsOnChatEntryRef = useRef(false)
  // Deep-link wake (?wake=1 on a concrete chat URL) must arm the keep at
  // RENDER time: on a fresh mount the hook's close runs before ANY effect
  // of this component, so an effect-set ref is always too late. The param
  // is stripped by the consume effect in the same commit.
  if (searchParams.get('wake') === '1') keepAppsOnChatEntryRef.current = true
  // Apps-UI open/close rules — arrival on HOME opens (incl. agent switch),
  // entering any chat closes / never auto-opens (except a kept phone-mode
  // entry). Extracted for direct unit coverage; the rules live in the
  // hook's header comment.
  useAppsAutoOpen(agentName, urlChatId, pinnedApps, setAppsOpen, keepAppsOnChatEntryRef)
  useEffect(() => {
    if (!appsActive) return
    return pushEscHandler(() => setAppsOpen(false))
  }, [appsActive])
  const toggleApps = useCallback(() => {
    // Reveal-intent toggle: the flag can be stale-true UNDER an open
    // workspace (opening the workspace hides apps without clearing appsOpen,
    // so closing it restores them). Toggling the raw flag from that state
    // made the first click an invisible no-op — key on VISIBILITY instead.
    if (appsActive) {
      setAppsOpen(false)
      return
    }
    if (workspace.state.open) workspace.closeWorkspace()
    setAppsOpen(true)
  }, [appsActive, workspace])

  // The active chat's row — task chats resolve through the task list (they
  // never appear in the chat-mode list).
  const activeChatRow = chats?.find((c) => c.id === chatId)
    ?? taskChats?.find((c) => c.id === chatId)

  // Dock overlay (staged feature — this block + the entry button strip from
  // the public cut). Offered when the active chat participates in a
  // delegation project OR carries a chat-scoped pinned dashboard; closes on
  // chat switch like the find bar.
  const [projectsOpen, setProjectsOpen] = useState(false)
  // Every delegation participant gets the dock: orchestrators (stamped even
  // without a project slug) and workers (chat- or task-surface — the lane
  // graph falls back to lineage server-side when project_id is empty).
  const isProjectChat =
    !!activeChatRow?.project_id ||
    activeChatRow?.delegate_role === 'orchestrator' ||
    activeChatRow?.delegate_role === 'worker' ||
    activeChatRow?.origin === 'delegated'
  const { data: chatPins } = useChatPins(chatId ?? undefined)
  const hasChatPin = !!chatPins?.chat || (chatPins?.files?.length ?? 0) > 0
  const dockAvailable = isProjectChat || hasChatPin
  useEffect(() => { setProjectsOpen(false) }, [chatId])
  const projectsActive = projectsOpen && dockAvailable && !workspace.state.open && !appsOpen
  // An agent pin/re-pin broadcasts file_updated for its apps/*.html — that's
  // the signal a Dock pin appeared/changed (the toggle may need to show up
  // while the overlay is closed, so this lives here, not in the overlay).
  // File pin/unpin broadcasts carry the `pin` marker instead (any path).
  const queryClient = useQueryClient()
  useEffect(() => onFileUpdate((u) => {
    if (u.pin || /(^|\/)apps\/[^/]+\.html$/.test(u.rel_path)) {
      // Agents pin/write apps exactly at turn end — wait out live speech
      // (immediate when nothing is playing).
      speechIdle(() => queryClient.invalidateQueries({ queryKey: ['chat-pins'] }))
    }
  }), [queryClient])

  // Ctrl/Cmd+E toggles the workspace overlay.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'e' || e.key === 'E')) {
        const tag = (e.target as HTMLElement | null)?.tagName
        // Don't hijack the shortcut while the user is typing into the
        // textarea — but we still let the textarea bubble it through; if
        // they really mean to fire it they can drop focus first.
        if (tag === 'INPUT') return
        e.preventDefault()
        workspace.toggleWorkspace()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [workspace])

  // --- Find bar: URL param integration + Ctrl+F + debounce ---

  // Capture ?q= param from URL but defer opening until chat_history loads.
  // Opening immediately causes the find bar to render before messages are available.
  useEffect(() => {
    const q = searchParams.get('q')
    if (q) {
      pendingFindQuery.current = q
      // Remove ?q from URL without re-navigation
      setSearchParams({}, { replace: true })
    }
  }, [searchParams, setSearchParams])

  // Deep-link from a file-conflict notification: ?recover=1 opens the workspace
  // overlay + the Recover bin modal, then clears the param.
  const [recoverRequested, setRecoverRequested] = useState(false)
  useEffect(() => {
    if (searchParams.get('recover') === '1') {
      if (!workspace.state.open) workspace.toggleWorkspace()
      setRecoverRequested(true)
      setSearchParams(prev => { prev.delete('recover'); return prev }, { replace: true })
    }
  }, [searchParams, setSearchParams, workspace])

  // Wake word: ?wake=1 (set by useWakeWord's navigate) auto-starts a duplex
  // session on this agent's NEW chat — consume-and-strip like ?recover=1,
  // then run the mic toggle's never-warmed branch once the dashboard socket
  // is up (ws.send silently drops before that).
  const [wakeRequested, setWakeRequested] = useState(false)
  // Marks the CURRENT duplex session as wake-initiated (10 s silence guard).
  const wakeInitiatedRef = useRef(false)
  useEffect(() => {
    if (searchParams.get('wake') === '1') {
      setWakeRequested(true)
      setSearchParams(prev => { prev.delete('wake'); return prev }, { replace: true })
    }
  }, [searchParams, setSearchParams])
  // (The wake ARM effect lives below handleNewChat — it reuses it for the
  // stale-state reset and a dep-array reference above its declaration would
  // be a TDZ error.)

  // Wake-initiated sessions auto-close after 10 s of silence — the safety
  // valve for a false trigger (TV speech wakes an empty room). Any sign of a
  // real conversation (a caption, a dispatched turn) disarms it; a manual
  // toggle never arms it.
  useEffect(() => {
    if (!wakeInitiatedRef.current) return
    const phase = duplexVoice.phase
    if (phase === 'off' || phase === 'error') { wakeInitiatedRef.current = false; return }
    if (duplexVoice.caption || phase === 'thinking' || phase === 'speaking') {
      wakeInitiatedRef.current = false // real conversation — guard off
      return
    }
    if (phase !== 'listening') return
    const timer = window.setTimeout(() => {
      if (wakeInitiatedRef.current && duplexVoice.active) {
        wakeInitiatedRef.current = false
        duplexVoice.stop()
        chatNotif.playPing()
      }
    }, 10_000)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [duplexVoice.phase, duplexVoice.caption])

  // Debounce find input → findQuery (200ms)
  useEffect(() => {
    const timer = setTimeout(() => setFindQuery(findInput), 200)
    return () => clearTimeout(timer)
  }, [findInput])

  // Ctrl+F / Cmd+F intercept
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault()
        setFindBarOpen(true)
      }
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [])

  const closeFindBar = useCallback(() => {
    setFindBarOpen(false)
    setFindInput('')
    setFindQuery('')
  }, [])

  // --- Connect on mount ---

  useEffect(() => {
    ws.connect()
    return () => ws.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Resume / reset chat state when the URL chat changes. Handles three cases:
  //   - Initial mount with /chat/:agent/:chatId
  //   - In-component navigation via handleSelectChat / handleNewChat (which pre-set
  //     lastResumedChatIdRef to skip this branch — they already reset state inline)
  //   - External URL change: notification deeplinks (incl. cross-agent), browser
  //     back/forward, direct URL paste. AgentChat is reused across same-Route
  //     navigations so we must reset all chat state explicitly here.
  useEffect(() => {
    if (!ws.connected || !agentName) return
    if (!urlChatId) {
      lastResumedChatIdRef.current = null  // exited chat — clear tracking
      // Entering the NEW-chat view: the previously-viewed chat's execution-
      // mode override must NOT leak into the new chat (it ran new chats in
      // the wrong mode both ways). Reset the interactive state, then re-seed
      // the agent's sticky pick. Once per visit — the ref guard keeps a WS
      // reconnect / re-render from clobbering a toggle made while composing.
      if (newChatModeSeededRef.current !== agentName) {
        newChatModeSeededRef.current = agentName
        interactive.resetSession()
        interactive.seedExecMode(useAgentPrefsStore.getState().lastInteractive[agentName] || '')
      }
      return
    }
    newChatModeSeededRef.current = null  // viewing a chat — re-arm for the next new-chat visit
    if (lastResumedChatIdRef.current === urlChatId) return  // already loaded
    lastResumedChatIdRef.current = urlChatId

    // Full reset (mirrors handleNewChat — most-thorough variant, safe for both
    // same-agent and cross-agent transitions; agent-specific defaults like
    // chatActiveLayer/model are re-set by onChatHistory when the chat loads).
    discardingRef.current = true
    setMessages([])
    currentMsgRef.current = null
    pendingMessageRef.current = null
    heldForAgentsRef.current = null  // a held first send must not flush into the opened chat
    thinkingBufRef.current = ''
    setChatId(urlChatId)
    setSessionId(null)
    setChatActiveLayer(null)
    setSelectedLayer(null)
    setWarmingUp(false)
    preWarmedRef.current = null
    setTotalCost(0)
    setLimitReached(false)
    setLimitWarning(null)
    setContextUsed(0)
    setContextMax(0)
    setSessionPlans([])
    setCurrentTodos([])
    setCurrentGoal(null)
    setMeetingActive(false)
    setMeetingParticipants([])
    setMeetingSpeaker(null)
    setMeetingRound(0)
    meetingSpeakerRef.current = null
    setTurnStartTime(null)
    setThinkingActive(false)
    setCompressingActive(false)
    setWorkflows([])
    setPermissionPending(false)
    setAborting(false)
    setFindBarOpen(false)
    setFindInput('')
    setFindQuery('')
    // Same sticky re-seed as handleSelectChat: a task chat's restored model +
    // 'auto' mode must not leak into the chat this navigation opens.
    if (agentName) {
      const prefs = useAgentPrefsStore.getState()
      setModel(prefs.lastModel[agentName] || agentDefaultModel)
      setMode(prefs.lastMode[agentName] || 'default')
    }

    ws.resumeChat(urlChatId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.connected, agentName, urlChatId])

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

  // After warmup completes, send any pending message (with images if any)
  useEffect(() => {
    if (sessionId && chatId && pendingMessageRef.current) {
      const text = pendingMessageRef.current
      const images = pendingImagesRef.current
      const files = pendingFilesRef.current
      pendingMessageRef.current = null
      pendingImagesRef.current = null
      pendingFilesRef.current = null
      ws.sendMessage(text, chatId, images || undefined, files || undefined)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, chatId])

  // --- Handlers ---

  // Files upload eagerly on pick (not on Send — send stays near-instant),
  // through the GLOBAL sequential queue (lib/chatUploadQueue): one wire chain
  // for any number of picked files, XHR byte progress on each chip, and
  // updates that survive chat navigation + the draft→chat re-key.
  const handleAddFiles = useCallback((files: PendingFile[]) => {
    if (!draftKey) return
    // Pre-errored entries (oversize — ChatInput tags them) render as error
    // chips only: no upload, no abort controller. Everything else queues as
    // uploading (queued files count as uploading so the send gate holds —
    // a Send while file 3/3 waits would silently drop it from the message).
    const tagged = files.map(f => f.error ? f : ({
      ...f,
      uploading: true as const,
      queued: true as const,
      abortController: new AbortController(),
    }))
    useChatStore.getState().addPendingFiles(draftKey, tagged)
    for (const f of tagged) {
      if (f.error || !f.abortController) continue
      enqueueChatUpload({
        fileId: f.id, file: f.file, agent: agentName || '',
        abort: f.abortController,
      })
    }
  }, [agentName, draftKey])

  const handleRemoveFile = useCallback((id: string) => {
    if (!draftKey) return
    // Still queued → drop from the chain; in-flight → abort BEFORE the
    // mutator removes the entry (the mutator only removes by id).
    dequeueChatUpload(id)
    const target = useChatStore.getState().byChat[draftKey]?.pendingFiles.find(f => f.id === id)
    target?.abortController?.abort()
    useChatStore.getState().removePendingFile(draftKey, id)
  }, [draftKey])

  // Re-queue a failed upload from its error chip (ChatInput only offers the
  // affordance for retryable failures — oversize chips stay remove-only).
  const handleRetryFile = useCallback((id: string) => {
    if (!draftKey) return
    const target = useChatStore.getState().byChat[draftKey]?.pendingFiles.find(f => f.id === id)
    if (!target?.file) return
    const abort = new AbortController()
    useChatStore.getState().updatePendingFile(draftKey, id, {
      error: undefined,
      uploading: true, queued: true,
      uploadSent: 0, uploadTotal: target.size,
      abortController: abort,
    })
    enqueueChatUpload({
      fileId: id, file: target.file, agent: agentName || '', abort,
    })
  }, [agentName, draftKey])

  const handleSend = useCallback(
    (text: string) => {
      if (!agentName) return
      // Live conversation: a typed send rides the duplex socket as a
      // SPOKEN-mode turn — the reply comes back as TTS, and none of the
      // overlay-closing below runs (an open mini-app view survives the
      // send). Attachments can't ride the frame — fall through to a
      // normal typed send when any are pending, or if the socket is gone.
      if (duplexActiveRef.current && pendingImages.length === 0
          && pendingFiles.length === 0 && duplexVoice.sendTyped(text)) {
        if (draftKey) useChatStore.getState().clearDraft(draftKey)
        return
      }
      if (duplexExitNoteRef.current && duplexExitNoteRef.current === chatId) {
        duplexExitNoteRef.current = null
        text = '[The user left the live spoken conversation mode — this is '
          + 'normal typed chat again; respond normally in text.]\n\n' + text
      }
      abortedRef.current = false  // Reset abort guard on new send
      discardingRef.current = false  // User is sending — accept events
      // Draft persisted only as long as it's unsent. Clear immediately so a
      // tab crash mid-streaming doesn't leave the just-sent text dangling.
      if (draftKey) useChatStore.getState().clearDraft(draftKey)
      // Drop the workspace/apps overlay when the user sends — they're done
      // browsing and should see the turn. The path/view memory persists so
      // re-opening returns to the same folder/tab.
      if (workspace.state.open) workspace.closeWorkspace()
      setAppsOpen(false)

      // Capture pending images and files before clearing. Files were uploaded eagerly
      // on pick, so each has uploadedPath set by now (the Send button is disabled
      // while any upload is still in-flight).
      const images = pendingImages.length > 0 ? [...pendingImages] : undefined
      const files = pendingFiles.length > 0 ? [...pendingFiles] : undefined
      if (draftKey) {
        if (images) useChatStore.getState().setPendingImages(draftKey, [])
        if (files) useChatStore.getState().setPendingFiles(draftKey, [])
      }

      const uploadedFiles: Array<{ path: string; name: string }> | undefined = files
        ?.filter(f => f.uploadedPath)
        .map(f => ({ path: f.uploadedPath!, name: f.name }))

      // Helper: add user bubble + empty assistant placeholder (shows typing dots)
      const addUserAndPlaceholder = (text: string) => {
        const msgId = `user-${Date.now()}`
        // Build blocks: file badges + image thumbnails + text
        const blocks: MessageBlock[] = []
        if (uploadedFiles?.length) {
          blocks.push({ type: 'file_attachments', files: uploadedFiles.map(f => ({ name: f.name, path: f.path })) })
        }
        if (images) {
          blocks.push({ type: 'image_attachments', images: images.map(i => i.base64) })
        }
        blocks.push({ type: 'text', content: text })

        const userMsg: DisplayMessage = {
          id: msgId,
          role: 'user',
          blocks,
          createdAt: new Date().toISOString(),
        }
        const assistantPlaceholder: DisplayMessage = {
          id: `stream-${Date.now()}`,
          role: 'assistant',
          blocks: [],
          createdAt: new Date().toISOString(),
        }
        currentMsgRef.current = assistantPlaceholder
        setMessages((prev) => [...prev, userMsg, assistantPlaceholder])
        setTurnStartTime(Date.now())
      }

      const wsImages = images?.map(i => ({ base64: i.base64, name: i.name }))
      const wsFiles = uploadedFiles?.length ? uploadedFiles : undefined

      // First send racing the agents query: with no explicit per-chat mode and
      // the agents list still loading, the effective mode is UNKNOWABLE —
      // `interactiveMode` computes false only because the agent default hasn't
      // arrived, so routing now would commit a default-interactive agent's
      // first send to headless. Hold the send exactly like a during-warmup
      // queue (bubble + warming badge now, server-kick adoption armed); the
      // flush effect below routes it when the query settles. A failed query
      // never holds (routing falls back to what we have).
      if (!sessionId && !warmingUp && agents === undefined && !agentsLoadFailed && !interactive.chatExecMode) {
        addUserAndPlaceholder(text)
        sentWithBubbleRef.current = text
        setWarmingUp(true)
        serverKickPendingRef.current = { chatId: chatId || null }
        heldForAgentsRef.current = { text, images: wsImages, files: wsFiles }
        return
      }

      // Interactive CLI: the human drives the PTY directly. A live
      // session → write the line straight to the terminal; toggle-on with
      // nothing live yet → cold-start an interactive warmup (the cold start adds
      // the bubble too — reused to stream into if the backend declines). Returns
      // null when not interactive, so we fall through to the normal `-p` path.
      const routed = interactive.routeSend(text, {
        chatId, sessionId, warmingUp,
        warmupParams: { agentName, chatId: chatId || undefined, mode, model, layer: chatActiveLayer ?? selectedLayer ?? undefined },
        onColdStart: () => {
          addUserAndPlaceholder(text)
          sentWithBubbleRef.current = text
          setWarmingUp(true)
          // A cold interactive send carries its text with the warmup (Codex
          // argv / Claude server submit) — if the backend DECLINES interactive
          // it server-kicks that text as a -p turn, so arm the adoption like
          // the -p cold path below.
          serverKickPendingRef.current = { chatId: chatId || null }
        },
        images: wsImages, files: wsFiles,
      })
      if (routed) {
        // Sending while reviewing the rich history → snap back to the live
        // terminal so the input lands + the response streams in view.
        interactive.setShowRichView(false)
        return
      }

      if (sessionId && chatId) {
        if (ws.streaming) {
          ws.sendMessage(text, chatId, wsImages, wsFiles)
        } else {
          addUserAndPlaceholder(text)
          sentWithBubbleRef.current = text
          ws.sendMessage(text, chatId, wsImages, wsFiles)
        }
      } else if (!warmingUp) {
        addUserAndPlaceholder(text)
        setWarmingUp(true)
        serverKickPendingRef.current = { chatId: chatId || null }  // adopt the server-kicked turn on warmup_ready
        // Server-owned first turn: the prompt rides WITH warmup —
        // the backend persists it at send-time and kicks the turn on
        // warmup_ready, so it runs even if we navigate away / refresh during
        // spawn. No client-side pending message for the first turn.
        // Pass the explicit per-chat execution_mode so this -p spawn is
        // self-contained: routeSend already returned null (NOT interactive), but a
        // brand-new chat has no row for the toggle's changeExecutionMode to persist
        // into, and a toggle-then-send-fast races that write — so without the mode
        // ON the warmup the backend falls back to the agent default (interactive)
        // and the terminal opens despite the toggle being OFF. `'' → undefined`
        // keeps an unset chat following the agent default (no pinning).
        // ALWAYS carry the dashboard theme: the backend may still resolve this
        // warmup to interactive (agent default + unset override, or a dead
        // interactive chat re-warmed by a plain send) and would otherwise seed
        // the TUI dark — a light dashboard then gets a dark terminal (the
        // e88020d attach ack makes the xterm follow the baked theme). Ignored
        // for -p spawns.
        ws.warmup(agentName, chatId || undefined, mode, model, chatActiveLayer ?? selectedLayer ?? undefined, { text, images: wsImages, files: wsFiles }, interactive.chatExecMode || undefined, currentDashboardTheme())
      } else {
        pendingMessageRef.current = text
        pendingImagesRef.current = wsImages || null
        pendingFilesRef.current = wsFiles || null
      }
    },
    [agentName, sessionId, chatId, mode, model, chatActiveLayer, selectedLayer, ws, warmingUp, agents, agentsLoadFailed, interactive.routeSend, interactive.setShowRichView, interactive.chatExecMode, pendingImages, pendingFiles, workspace, draftKey, duplexVoice.sendTyped],
  )

  // Flush a send held above once the agents query settles: route it with the
  // now-known effective mode. The bubble/badge/kick-adoption were set at hold
  // time, so the cold-start callback is a no-op and ctx.warmingUp is passed
  // false (the "warmup" in flight is our own hold, not a real spawn).
  useEffect(() => {
    if (agents === undefined && !agentsLoadFailed) return
    const held = heldForAgentsRef.current
    if (!held || !agentName) return
    heldForAgentsRef.current = null
    const routed = interactive.routeSend(held.text, {
      chatId, sessionId, warmingUp: false,
      warmupParams: { agentName, chatId: chatId || undefined, mode, model, layer: chatActiveLayer ?? selectedLayer ?? undefined },
      onColdStart: () => {},
      images: held.images, files: held.files,
    })
    if (routed) return
    // Not interactive → the normal server-owned headless first turn (same
    // call as the un-held send path).
    ws.warmup(agentName, chatId || undefined, mode, model, chatActiveLayer ?? selectedLayer ?? undefined, { text: held.text, images: held.images, files: held.files }, interactive.chatExecMode || undefined, currentDashboardTheme())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, agentsLoadFailed, interactive.routeSend])

  const handleAbort = useCallback(() => {
    if (warmingUp) {
      // Cancel before warmup completes — don't send the pending message
      pendingMessageRef.current = null
      pendingImagesRef.current = null
      pendingFilesRef.current = null
      heldForAgentsRef.current = null  // a send held on the agents query is cancelled too
      setWarmingUp(false)
      // Remove the user message bubble that was added on send
      setMessages((prev) => {
        if (prev.length > 0 && prev[prev.length - 1].role === 'user') {
          return prev.slice(0, -1)
        }
        return prev
      })
      setTurnStartTime(null)
      // Tell the backend to cancel the in-flight spawn too — without this the
      // server finishes warming and runs the server-kicked first turn anyway
      // (the "stop while getting ready didn't stop codex" bug). The backend
      // flags the warming chat, lets the spawn finish, then kills the session
      // and suppresses the first turn (abort-during-spawn).
      ws.abort()
      return
    }
    if (aborting) return  // Already aborting — don't send again
    abortedRef.current = true  // Guard against stale chunks
    setAborting(true)  // Disable stop button, show "Stopping..."
    ws.abort()
    // Run the cleanup immediately rather than waiting for the server's
    // `aborted` event. The server confirmation may be dropped on a flaky
    // network and was the root cause of stuck spinners reported by users.
    // `finalizeAbortedTurn` is idempotent so the duplicate run from
    // `onAborted` is harmless.
    finalizeAbortedTurn()
  }, [ws, warmingUp, aborting, finalizeAbortedTurn])

  const handleNewChat = useCallback(() => {
    if (!agentName) return
    // Discard stale in-flight WS events from old chat (cleared in handleSend)
    discardingRef.current = true
    ws.resetStreaming()  // Stop button → send button immediately
    if (workspace.state.open) workspace.closeWorkspace()
    setMessages([])
    setChatId(null)
    setSessionId(null)
    setChatActiveLayer(null)
    setSelectedLayer(null)
    setWarmingUp(false)
    interactive.resetSession()  // no live terminal on a fresh chat
    // Re-apply the agent's sticky interactive choice (resetSession cleared the
    // override) so a new chat keeps the user's last on/off pick — the interactive
    // twin of the sticky model/mode below. The seed effect also covers a
    // page refresh; doing it here makes the in-page New-Chat action deterministic.
    const prefs = useAgentPrefsStore.getState()
    const stickyInteractive = prefs.lastInteractive[agentName] || ''
    interactive.seedExecMode(stickyInteractive)
    // Pre-stamp the URL effect's new-chat guard — this handler just did the
    // reset+seed; the navigate below must not trigger a duplicate reset.
    newChatModeSeededRef.current = agentName
    currentMsgRef.current = null
    pendingMessageRef.current = null
    heldForAgentsRef.current = null  // a held first send belongs to the abandoned chat view
    setTotalCost(0)
    setLimitReached(false)
    setLimitWarning(null)
    setContextUsed(0)
    setContextMax(0)
    setSessionPlans([])
    setCurrentTodos([])
    setCurrentGoal(null)
    setMeetingActive(false)
    setMeetingParticipants([])
    setMeetingSpeaker(null)
    setMeetingRound(0)
    meetingSpeakerRef.current = null
    setTurnStartTime(null)
    setThinkingActive(false)
    setCompressingActive(false)
    setWorkflows([])
    setPermissionPending(false)
    setAborting(false)
    // Seed the agent's STICKY model (the reconciliation effect below then derives
    // its layer) so New Chat keeps the user's last layer+model pick, not the agent
    // default. `setModel(agentDefaultModel)` here was the regression: it left
    // `model` non-empty so the seed effect's `if (!model)` guard skipped the
    // sticky model, and the layer followed the default model.
    setModel(prefs.lastModel[agentName] || agentDefaultModel)
    // Sticky MODE from prefs — not the current state: coming from a task-run
    // chat the state holds the run's 'auto', and the pre-warm below would
    // spawn the new session with Don't Ask. prefs.lastMode holds the user's
    // own last explicit pick, so New Chat stays sticky without the leak.
    const nextMode = prefs.lastMode[agentName] || 'default'
    setMode(nextMode)
    preWarmedRef.current = null
    navigate(`/chat/${agentName}`, { replace: true })
    // Trigger eager pre-warmup for the new chat with the just-seeded sticky
    // mode so the pre-warmed session spawns with the user's selected
    // permission mode — otherwise the session's _session_modes entry is
    // locked to "default" at start_session time and the user has to toggle
    // the dropdown to re-apply.
    // Skip the headless pre-warm when the new chat will be interactive — the
    // interactive send spawns its own PTY session (which would supersede a
    // headless pre-warm). Use the just-seeded sticky choice (state setters above
    // haven't applied yet, so `interactive.interactiveMode` is still stale).
    const willBeInteractive =
      (stickyInteractive || (currentAgent?.default_execution_mode || '')) === 'interactive'
    // Eager only for the favorite — others warm lazily on first interaction.
    if (ws.connected && !willBeInteractive && isFavoriteAgent) {
      ws.preWarmup(agentName, agentDefaultModel, nextMode, preWarmPath)
    }
  }, [agentName, agentDefaultModel, preWarmPath, navigate, ws, workspace, currentAgent?.default_execution_mode, interactive.seedExecMode, interactive.resetSession, isFavoriteAgent])

  // Wake word ARM (?wake=1 was consumed above into wakeRequested): start
  // duplex on a FRESH chat of the route agent. Never trust the page's chatId
  // state here — right after a wake navigation from another open chat it is
  // STALE (nothing resets state for an EXTERNAL entry into the new-chat
  // view), and starting on it attached duplex to the previous agent's dead
  // interactive chat (live-hit 2026-08-14: session_dead → heal refused
  // interactive_chat → spoken error, no reply). Stale state runs
  // handleNewChat — the canonical full reset, and wake means "new chat" by
  // decision — then the effect re-fires once chatId clears.
  useEffect(() => {
    if (!wakeRequested || !ws.connected || warming) return
    if (!audioCapability?.duplex) return // capability still loading
    if (urlChatId) {
      // Explicit deep-link (?wake=1 on a concrete chat URL): start on that
      // chat once the page state has synced to it.
      if (chatId !== urlChatId) return
      setWakeRequested(false)
      if (!audioCapability.duplex.available || duplexVoice.active) return
      // (Apps keep-open was armed at render time from the ?wake=1 param —
      // the mount-commit close runs before any effect could set it here.)
      wakeInitiatedRef.current = true
      duplexVoice.start()
      return
    }
    if (chatId) { handleNewChat(); return }
    setWakeRequested(false)
    if (!audioCapability.duplex.available || duplexVoice.active) return
    if (!agentName) return
    wakeInitiatedRef.current = true
    keepAppsOnChatEntryRef.current = true // belt-and-braces beside the render-time arm
    duplexPendingRef.current = true
    ws.warmup(agentName, undefined, mode, model,
      chatActiveLayer ?? selectedLayer ?? undefined, undefined,
      interactive.chatExecMode || undefined,
      currentDashboardTheme())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wakeRequested, ws.connected, warming, audioCapability, chatId, urlChatId, handleNewChat])

  // Lazy pre-warm: a non-favorite agent's new-chat page warms the moment the
  // user genuinely engages the composer (first keydown/pointerdown). Deduped by
  // the same guards as the eager effect so the favorite's eager warm (which
  // wins the race) makes this a no-op. Fires at most once per pre-warm cycle
  // (ChatInput's onEngage is itself one-shot; preWarmedRef seals it server-side).
  const handleEngage = useCallback(() => {
    if (!ws.connected || !agentName) return
    if (urlChatId) return               // existing chat — resume path owns it
    if (warmingUp || sessionId) return   // already active
    if (preWarmedRef.current) return     // already pre-warmed / warming
    ws.preWarmup(agentName, model, mode, preWarmPath)
  }, [ws, agentName, urlChatId, warmingUp, sessionId, model, mode, preWarmPath])

  const handleSelectChat = useCallback(
    (selectedChatId: string, searchQuery?: string) => {
      if (selectedChatId === chatId && !searchQuery) return
      // Discard stale in-flight WS events from old chat (cleared in onChatHistory)
      discardingRef.current = true
      if (workspace.state.open) workspace.closeWorkspace()
      setAppsOpen(false)
      // A user-driven chat pick always closes the panel — disarm any stale
      // phone-mode keep (an abandoned wake must not invert this close).
      keepAppsOnChatEntryRef.current = false
      // Reset ALL state to prevent cross-chat leakage
      setMessages([])
      currentMsgRef.current = null
      pendingMessageRef.current = null
      heldForAgentsRef.current = null  // a held first send must not flush into the selected chat
      thinkingBufRef.current = ''
      setChatId(selectedChatId)
      setSessionId(null)
      setWarmingUp(false)
      interactive.resetSession()  // cleared until the resumed chat's warmup_ready{interactive}
      serverKickPendingRef.current = false  // the left chat's kick must not adopt a later ready
      // Re-seed model/mode from the sticky prefs: a task-run chat restored
      // its RUN's model + 'auto' permission into this state (chat_history
      // mode), and neither may leak into the next chat — 'auto' would
      // silently run a normal chat with Don't Ask. chat_history overrides
      // for the opened chat (model always; mode for task chats).
      if (agentName) {
        const prefs = useAgentPrefsStore.getState()
        setModel(prefs.lastModel[agentName] || agentDefaultModel)
        setMode(prefs.lastMode[agentName] || 'default')
      }
      preWarmedRef.current = null
      setTotalCost(0)
      setLimitReached(false)
      setLimitWarning(null)
      setContextUsed(0)
      setContextMax(0)
      setSessionPlans([])
      setTurnStartTime(null)
      setThinkingActive(false)
      setCompressingActive(false)
      setWorkflows([])
      setPermissionPending(false)
      setAborting(false)
      // Close find bar when switching chats (reopened by ?q= param if from search)
      setFindBarOpen(false)
      setFindInput('')
      setFindQuery('')
      const qParam = searchQuery ? `?q=${encodeURIComponent(searchQuery)}` : ''
      navigate(`/chat/${agentName}/${selectedChatId}${qParam}`)
      if (selectedChatId !== chatId) {
        // Mark loaded so the URL-change effect doesn't double-resume on next render.
        lastResumedChatIdRef.current = selectedChatId
        ws.resumeChat(selectedChatId)
      }
    },
    [agentName, agentDefaultModel, chatId, ws, navigate, workspace, interactive.resetSession],
  )

  // Mini-app send_prompt router — the host page decides the delivery rail:
  // interactive chat → typed into the terminal (composer rail, same as the
  // artifact PiP backchannel; the manifest approval gates it client-side
  // since PTY input is the user's own channel); open headless chat → the
  // app_action WS frame (server-gated); front page → create a chat, view it,
  // then deliver (retrying past the resume race). fire_task never comes here
  // (AppFrame routes it over REST).
  const handleAppSendPrompt = useCallback(
    async (
      app: PinnedApp,
      action: { id: string; label: string; prompt: string },
      args: unknown,
    ): Promise<{ status: string; reason?: string }> => {
      const substituted = substituteArgs(action.prompt, args)
      if (interactive.sessionInteractive && chatId) {
        const built = buildAppActionText(app.title || app.slug, action.label, substituted)
        if ('error' in built) return { status: 'denied', reason: built.error }
        ws.sendPtyInput(chatId, ptyPasteB64(withInteractiveTime(built.framed)), true)
        return { status: 'sent' }
      }
      // No live terminal but interactive is the effective mode (agent default
      // or per-chat toggle) — front page or a dead interactive chat. Ride the
      // composer's own cold-start rail: the framed text goes up WITH the
      // warmup and the backend delivers it into the fresh terminal (or
      // server-kicks it as a -p turn on a decline — arm the adoption ref so
      // that turn engages the streaming UI, like the composer path).
      // Without this, the fresh terminal opened and the prompt never landed.
      if (interactive.interactiveMode && agentName) {
        const built = buildAppActionText(app.title || app.slug, action.label, substituted)
        if ('error' in built) return { status: 'denied', reason: built.error }
        const routed = interactive.routeSend(built.framed, {
          chatId, sessionId, warmingUp,
          warmupParams: { agentName, chatId: chatId || undefined, mode, model, layer: chatActiveLayer ?? selectedLayer ?? undefined },
          onColdStart: () => {
            setAppsOpen(false)
            setWarmingUp(true)
            serverKickPendingRef.current = { chatId: chatId || null }
          },
        })
        if (routed) {
          interactive.setShowRichView(false)
          return { status: 'sent' }
        }
      }
      if (chatId) {
        return sendAppAction(app, action.id, action.label, substituted, args)
      }
      try {
        const res = await apiFetch('/v1/chats', {
          method: 'POST',
          body: JSON.stringify({ agent: agentName }),
        })
        if (!res.ok) return { status: 'denied', reason: 'could not start a chat' }
        const chat = (await res.json()).chat
        setAppsOpen(false)
        keepAppsOnChatEntryRef.current = false // app-action chats close the panel
        handleSelectChat(chat.id)
        for (let i = 0; i < 6; i++) {
          await new Promise((r) => setTimeout(r, 700))
          const ack = await sendAppAction(app, action.id, action.label, substituted, args)
          if (!(ack.status === 'denied' && ack.reason === 'not the viewed chat')) return ack
        }
        return { status: 'denied', reason: 'chat not ready' }
      } catch {
        return { status: 'denied', reason: 'could not start a chat' }
      }
    },
    [interactive.sessionInteractive, interactive.interactiveMode, interactive.routeSend,
     interactive.setShowRichView, chatId, sessionId, warmingUp, mode, model,
     chatActiveLayer, selectedLayer, ws, sendAppAction, agentName, handleSelectChat],
  )

  const handleModeChange = useCallback((m: string) => {
    ws.changeMode(m)
    setMode(m)
    // Sticky for the same agent's next new chat.
    if (agentName) useAgentPrefsStore.getState().setLastMode(agentName, m)
  }, [ws, agentName])

  // Codex plan card "Implement": leave plan mode (→ the next turn clears codex's
  // plan collaboration mode) and kick the build turn. Codex has no plan file, so
  // this replaces the Claude implement_plan (session-recreate) path.
  const handleImplementPlanCodex = useCallback((m: string) => {
    handleModeChange(m)
    handleSendMessage('Implement the plan you proposed above.')
  }, [handleModeChange, handleSendMessage])
  const handleModelChange = useCallback((compound: string) => {
    const { layer, model: modelId } = parseModelValue(compound)
    // Cross-engine pick on a committed chat → the provisional switch flow
    // (banner + confirm), NEVER an immediate model_change (the backend
    // rightly refuses foreign models and its refusal echo would snap the
    // selector around), no sticky write (chat-scoped decision, not a
    // preference), and no setSelectedLayer (a cancelled switch must not
    // poison later warmup-layer fallbacks). Gate on the PER-CHAT streaming
    // flag — ws.streaming is connection-global.
    if (chatActiveLayer && layer !== chatActiveLayer) {
      if (viewedStreaming || warming) return  // stale expansion — chat revived
      setEngineSwitchBusy(false)
      setEngineSwitchError(null)
      setPendingEngineSwitch({ layer, model: modelId })
      return
    }
    ws.changeModel(modelId)
    setModel(modelId)
    // Sticky for the same agent's next new chat — but never from a task
    // chat: a one-off pick on a run's chat must not rewrite the user's
    // default.
    if (agentName && !isTaskChat) useAgentPrefsStore.getState().setLastModel(agentName, modelId)
    // Track the selected layer (drives warmup + the dropdown highlight) WITHOUT
    // collapsing the dropdown — a not-yet-started chat keeps every layer visible.
    // The committed chatActiveLayer is set only when the chat actually starts.
    if (!ws.streaming) {
      setSelectedLayer(layer)
    }
  }, [ws, parseModelValue, agentName, chatActiveLayer, viewedStreaming, warming, isTaskChat])

  // Cross-engine switch confirm/cancel (EngineSwitchBanner + its dialog).
  const handleEngineSwitchConfirm = useCallback(() => {
    if (!pendingEngineSwitch) return
    setEngineSwitchBusy(true)
    setEngineSwitchError(null)
    ws.switchEngine(pendingEngineSwitch.layer, pendingEngineSwitch.model)
  }, [ws, pendingEngineSwitch])
  const handleEngineSwitchCancel = useCallback(() => {
    setPendingEngineSwitch(null)
    setEngineSwitchBusy(false)
    setEngineSwitchError(null)
  }, [])

  // Interactive CLI toggle. No live session:
  // set the per-chat intent + persist it (the next send spawns the chosen mode).
  // Live session: kill+rewarm in the target mode (deferred to turn-end if a -p
  // turn streams); a turn in flight confirms first — the switch kills it.
  const handleInteractiveToggle = useCallback((next: boolean) => {
    // Sticky for the same agent's next new chat (the interactive twin of the
    // model/mode stickiness). Persist the EXPLICIT choice both ways so
    // turning OFF overrides an interactive agent default on the next new chat.
    if (agentName) {
      useAgentPrefsStore.getState().setLastInteractive(agentName, next ? 'interactive' : '-p')
    }
    if (!sessionId || !chatId) {
      interactive.toggle(next, chatId)
      return
    }
    // A live/streaming turn dies with the switch (switch_execution_mode kills
    // the running process) — confirm before destroying a response in flight.
    // Gate on the VIEWED chat's per-chat slice OR the connection-global flag
    // so a turn started in another tab still warns. An IDLE live session
    // switches without a prompt: the restart is lossless (the conversation is
    // kept and reloaded). window.confirm is this page's existing confirm idiom.
    if (viewedStreaming || ws.streaming) {
      if (!window.confirm('Switching mode stops the current response. Switch anyway?')) return
    }
    interactive.performSwitch(next, chatId, ws.streaming)
  }, [interactive.toggle, interactive.performSwitch, sessionId, chatId, ws.streaming, viewedStreaming, agentName])

  // Interactive PTY died (the user quit the TUI with Ctrl+C/Ctrl+D, or it was
  // reaped). Leave the dead terminal for the DB rich view — reload chat_history
  // (the message list never synced the terminal's turns) — and the lazy
  // warmup_ready clears sessionId so the next send re-warms + RESUMES the session
  // (routeSend cold-start).
  const handleTerminalExit = useCallback(() => {
    if (!chatId) return
    interactive.setSessionInteractive(false)
    ws.resumeChat(chatId)
  }, [chatId, interactive.setSessionInteractive, ws])

  // View toggle: flip the live terminal ⇄ the DB rich
  // conversation history WITHOUT touching the session. Turning ON fetches a
  // FRESH snapshot — GET /v1/chats/{id} reads the DB and never touches the live
  // PTY (unlike resume_chat) — then maps it through the shared history mapper;
  // the terminal stays mounted (hidden) and keeps streaming underneath. OFF
  // re-shows it. The page `messages` only back the rich view here (the terminal
  // is the live surface), so overwriting them is safe.
  const handleToggleRichView = useCallback(async () => {
    if (!chatId) return
    if (interactive.showRichView) {
      interactive.setShowRichView(false)
      return
    }
    try {
      const { messages: rows, has_more } = await fetchChatPage(chatId, 50)
      seedDbHistory(rows, has_more)  // newest page + lazy scroll-back, same as resume
      interactive.setShowRichView(true)
    } catch { /* network error — stay on the terminal */ }
  }, [chatId, interactive.showRichView, interactive.setShowRichView, seedDbHistory])

  const handleCancelQueued = useCallback(
    (i: number) => {
      ws.cancelQueued(i)
      if (draftKey) useChatStore.getState().removeQueuedMessageByIndex(draftKey, i)
    },
    [ws, draftKey],
  )

  // Pull ALL queued messages back to input for editing (they're combined on the backend)
  const handleEditQueued = useCallback(
    () => {
      if (queuedMessages.length === 0) return
      const combined = queuedMessages.join('\n\n')
      ws.cancelAllQueued()
      if (draftKey) useChatStore.getState().clearQueuedMessages(draftKey)
      setEditText(combined)
    },
    [queuedMessages, ws, draftKey],
  )

  // The DB rich conversation view. Reused as the normal (non-interactive) view
  // AND — via the view-toggle — as an overlay above a live, mounted-but-
  // hidden terminal, so both paths render history identically.
  const messageListView = messages.length === 0 && !warmingUp ? (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center px-4">
        <h1 className="text-2xl font-bold text-brand mb-1">OtoDock</h1>
        <p className="text-sm text-p-text-secondary">
          What can I help you with today?
        </p>
      </div>
    </div>
  ) : (
    // ChatFileProvider makes the markdown file-path chips clickable (resolve →
    // preview); one mount here covers the normal view AND the rich-view overlay.
    <ChatFileProvider chatId={chatId || undefined} agent={agentName}>
      <ChatMessages
        messages={messages}
        agentName={agentName}
        agentDisplayName={agentDisplayName}
        agentColor={agentColor}
        chatId={chatId || undefined}
        onPermissionRespond={handlePermissionRespond}
        onPlanReviewResponse={resolvePlanReview}
        onImplementPlan={handleImplementPlan}
        onImplementPlanCodex={handleImplementPlanCodex}
        onQuestionAnswer={handleQuestionAnswer}
        onQuestionAnswerStructured={handleQuestionAnswerStructured}
        onSendMessage={handleSendMessage}
        onArtifactInteraction={sendArtifactInteraction}
        onPlanFetched={handlePlanFetched}
        onDismissPreview={(fileId, key) => {
          // Drop the preview blocks from local UI state (ref-safe; `key` scopes
          // to one frozen instance). The DocumentPreview component already
          // called the dismiss API before invoking this.
          dismissPreview(fileId, key)
        }}
        streaming={viewedStreaming}
        queuedMessages={queuedMessages}
        onCancelQueued={handleCancelQueued}
        onLoadOlder={loadOlder}
        hasMoreOlder={hasMoreOlder}
        loadingOlder={loadingOlder}
      />
    </ChatFileProvider>
  )

  // --- Render ---

  // overflow-clip (both axes): the PresenceHalo canvas deliberately pokes
  // past the viewport sides and bottom — clip (NOT hidden: clip doesn't
  // create a scroll container) so no geometry bug can ever make the page
  // itself pannable/scrollable; children own their scrolling.
  return (
    <div ref={swipeRef} className="flex h-screen-safe bg-p-bg overflow-clip">
      <ResponsiveDrawer open={historyOpen} onClose={() => setHistoryOpen(false)} width="w-64" widthPx={256}>
        <ChatHistory
          chats={chats || []}
          activeChatId={chatId}
          agentName={agentName}
          onSelect={handleSelectChat}
          onNew={handleNewChat}
          onNavigate={() => setHistoryOpen(false)}
          tasksMode={tasksMode}
          onTasksModeChange={setTasksMode}
          onMoveChat={() => ws.moveChat()}
          onRenameEditingChange={setRenameEditing}
        />
      </ResponsiveDrawer>

      <SearchProvider query={findBarOpen ? findQuery : ''}>
      <div className="flex-1 flex flex-col min-w-0">
      {/* The banner sits ABOVE the positioning context: the floating TopBar
          and side panels anchor to the inner relative div, so a visible
          banner pushes them down instead of covering them (the z-50 banner
          used to bury the z-20 TopBar and swallow its clicks). */}
      <SetupBanner />
      {/* min-h-0 is load-bearing: as a COLUMN flex item this div's
          min-height:auto tracks its content, so a long chat blows the
          100dvh root open and the DOCUMENT becomes the scroller (input +
          sidebar drift off-screen; the rich view opens at the top). The
          chat must always scroll inside ChatMessages, never the page. */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
        {/* Floating top bar */}
        <TopBar
          agentName={agentName || ''}
          displayName={agentDisplayName}
          executionTarget={sessionExecutionTarget}
          fallbackReason={sessionFallbackReason}
          machineName={agentTargetStatus?.machine_name ?? null}
          machineStatus={agentTargetStatus?.state ?? null}
          machineScope={agentTargetStatus?.scope}
          machineLastHeartbeatAgeS={agentTargetStatus?.last_heartbeat_age_s ?? null}
          machineLastSeenIso={agentTargetStatus?.last_seen_iso ?? null}
          onToggleHistory={() => setHistoryOpen(!historyOpen)}
          user={user}
          onLogout={logout}
          onAppSettings={isNative ? () => setAppSettingsOpen(true) : undefined}
          notificationBell={chatNotif.notificationBell}
        />

        {/* Find bar */}
        {findBarOpen && (
          <FindBar value={findInput} onChange={setFindInput} onClose={closeFindBar} />
        )}

        {/* Right-side floating panels (stacked). Hidden while the workspace
            overlay is open — they'd otherwise float over the chip row. */}
        {!workspace.state.open && (
          <div className="absolute top-14 right-3 z-10 flex flex-col gap-2 items-end">
            {/* Task chats keep the pinned run-info popup (name, status, cost). */}
            {isTaskChat && taskRun && <TaskMetadata run={taskRun} />}
            <PlanPanel plans={sessionPlans} />
            <GoalPanel goal={currentGoal} />
          </div>
        )}

        {/* Left-side floating panels (stacked: meeting above todo) */}
        {!workspace.state.open && (
          <div className="absolute top-14 left-3 z-10 flex flex-col gap-2 items-start">
            {meetingActive && (
              <MeetingIndicator
                participants={meetingParticipants}
                currentSpeaker={meetingSpeaker}
                leftParticipants={meetingLeftParticipants}
              />
            )}
            <TodoPanel todos={currentTodos} />
            <WorkflowPanel workflows={workflows} />
            {/* Minimized interactive-CLI artifact windows dock here. */}
            <ArtifactDock
              windows={artifacts.windows}
              minimized={artifacts.minimized}
              onRestore={artifacts.restore}
              onClose={artifacts.close}
            />
          </div>
        )}

        {/* Agent HOME: the live-sessions strip rides permanently on top —
            the platform panel above whatever the slot shows (dashboards or
            the landing hero), mirroring the project dock's composition. It
            carries the floating-TopBar clearance, so AppsOverlay drops its
            own (topPadding) while the strip is visible. */}
        {showHomeActive && agentName && (
          <ActiveChatsPanel
            variant="home"
            currentAgent={agentName}
            activeChatId={null}
            onSelect={handleSelectChat}
          />
        )}

        {/* Main content area — scrollable, with padding for floating bars.
            The workspace overlay swaps this slot in place when toggled. */}
        {workspace.state.open && agentName && isTaskChat ? (
          <div className="flex-1 min-h-0">
            {(() => {
              // The overlay reflects the TASK's operating scope, not the
              // viewer's role: agent-scoped runs → shared dirs only
              // (Knowledge read-only, no Config, no My-* — mirrors the
              // agent-scope sandbox mount); user-scoped runs → the personal
              // set plus whatever agent folders the viewer's role allows.
              const agentMode = modeOfAgent(currentAgent)
              const isAgentScope =
                isSharedOnly(agentMode) || (taskRun?.scope ?? 'agent') !== 'user'
              return (
                <WorkspaceOverlay
                  agent={agentName}
                  canManage={isAgentScope ? false : canManageThisAgent}
                  canEdit={canEditThisAgent}
                  state={workspace.state}
                  actions={workspace}
                  topPadding
                  allowedScopes={
                    isAgentScope
                      ? ['agent-workspace', 'agent-knowledge']
                      : hasAgentScope(agentMode)
                        ? canManageThisAgent
                          ? ['my-workspace', 'my-context', 'agent-workspace', 'agent-knowledge', 'agent-config']
                          : ['my-workspace', 'my-context', 'agent-workspace', 'agent-knowledge']
                        : canManageThisAgent
                          ? ['my-workspace', 'my-context', 'agent-config']
                          : ['my-workspace', 'my-context']
                  }
                  defaultScope={isAgentScope ? 'agent-workspace' : 'my-workspace'}
                />
              )
            })()}
          </div>
        ) : workspace.state.open && agentName ? (
          <div className="flex-1 min-h-0">
            <WorkspaceOverlay
              agent={agentName}
              canManage={canManageThisAgent}
              canEdit={canEditThisAgent}
              state={workspace.state}
              actions={workspace}
              topPadding
              defaultScope={isSharedOnly(modeOfAgent(currentAgent)) ? 'agent-workspace' : 'my-workspace'}
              initialRecover={recoverRequested}
              onRecoverConsumed={() => setRecoverRequested(false)}
              // Mode decides which workspace chips exist: Shared-only has no
              // user scope (agent chips only — workspace, knowledge, config);
              // Personal-only has no shared workspace/knowledge (My chips +
              // config only); collaborative shows the default full set.
              allowedScopes={
                isSharedOnly(modeOfAgent(currentAgent))
                  ? ['agent-workspace', 'agent-knowledge', 'agent-config']
                  : isPersonalOnly(modeOfAgent(currentAgent))
                    ? ['my-workspace', 'my-context', 'agent-config']
                    : undefined
              }
            />
          </div>
        ) : appsActive && agentName ? (
          <div className="flex-1 min-h-0 flex flex-col">
            <AppsOverlay agent={agentName} onSendPrompt={handleAppSendPrompt} topPadding={!showHomeActive} />
          </div>
        ) : projectsActive && chatId && agentName ? (
          <div className="flex-1 min-h-0 flex flex-col">
            <ProjectsOverlay
              agent={agentName}
              chatId={chatId}
              isProjectChat={isProjectChat}
              pins={chatPins}
              onSelectChat={(id) => { setProjectsOpen(false); handleSelectChat(id) }}
              onClose={() => setProjectsOpen(false)}
              onSendPrompt={handleAppSendPrompt}
            />
          </div>
        ) : !ws.connected ? (
          <div className="flex-1 flex items-center justify-center text-p-text-light text-sm">
            Connecting...
          </div>
        ) : interactive.sessionInteractive ? (
          // Interactive CLI: the live themed terminal replaces the message list.
          // The view-toggle can overlay the DB rich
          // history WITHOUT detaching the PTY — keep TerminalView MOUNTED (hidden
          // when showRichView) so the session keeps streaming + xterm re-fits on
          // return. pt-12 sits just under the floating TopBar (h-12 absolute).
          // (Rich DB view also auto-shows on session death — handleTerminalExit.)
          <>
            <div className={`flex-1 min-h-0 flex-col pt-12 ${interactive.showRichView ? 'hidden' : 'flex'}`}>
              <React.Suspense fallback={
                <div className="flex-1 flex items-center justify-center text-p-text-light text-sm">Loading terminal…</div>
              }>
                <TerminalView ws={ws} chatId={chatId || ''} agent={agentName} artifacts={artifacts} onExit={handleTerminalExit} />
              </React.Suspense>
            </div>
            {interactive.showRichView && messageListView}
          </>
        ) : (
          messageListView
        )}

        <MachineUpdateBanner
          machineId={sessionExecutionTarget && sessionExecutionTarget !== 'local'
            ? sessionExecutionTarget
            : null}
        />

        <RemoteFallbackBanner
          fallbackReason={sessionFallbackReason}
          machineName={offlineMachineName}
        />

        {zeroAccessible && (
          <div
            role="status"
            data-testid="no-engine-access-notice"
            className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-amber-300/40 bg-amber-50/40 text-amber-900 dark:bg-amber-500/10 dark:text-amber-200"
          >
            You can't run any of this agent's AI engines —{' '}
            <Link to="/user-settings?tab=ai-engines" className="underline hover:no-underline">
              connect one in your AI-engine settings
            </Link>{' '}
            or ask an admin.
          </div>
        )}

        {/* Suppressed while an engine switch is pending — a dead chat pinned
            to an offline machine would otherwise stack two competing
            "restart from DB history" offers (move vs switch). */}
        <ChatTargetBanner
          chatId={chatId}
          mismatch={pendingEngineSwitch ? null : targetMismatch}
          moveDisabled={viewedStreaming || warming}
          onMove={() => ws.moveChat()}
        />

        <InstallProgressBar
          chatId={chatId}
          machineId={sessionExecutionTarget !== 'local' ? sessionExecutionTarget : null}
          agent={agentName}
          onRetry={() => {
            // Re-fire warmup with the same agent + mode + model (used by the
            // install-failed banner). Backend unregisters the previous
            // in-flight entry on terminal event, so the next warmup_started
            // reuses the same chat_id cleanly.
            if (chatId && agentName) {
              // Theme rides unconditionally — the backend may resolve this
              // warmup interactive even when the client doesn't know it yet
              // (ignored for -p spawns).
              ws.warmup(agentName, chatId, mode, model, chatActiveLayer ?? selectedLayer ?? undefined, undefined,
                interactive.chatExecMode || undefined,
                currentDashboardTheme())
            } else if (agentName) {
              // New-chat page (no chatId yet) — re-fire pre-warmup. Reset
              // the guard so the eager useEffect picks it up.
              preWarmedRef.current = null
              ws.preWarmup(agentName, model, mode, preWarmPath)
            }
          }}
        />

        {/* Cross-engine switch: provisional pick banner + confirm (closest
            to the composer — the switch blocks sending until resolved). */}
        <EngineSwitchBanner
          chatId={chatId}
          pending={pendingEngineSwitch ? {
            layer: pendingEngineSwitch.layer,
            model: pendingEngineSwitch.model,
            layerLabel: layers?.[pendingEngineSwitch.layer]?.display_name || pendingEngineSwitch.layer,
            modelLabel: agentLayerModels.find(m => m.value === pendingEngineSwitch.model)?.label
              || pendingEngineSwitch.model,
          } : null}
          fromLabel={(chatActiveLayer && layers?.[chatActiveLayer]?.display_name) || chatActiveLayer || ''}
          busy={engineSwitchBusy}
          error={engineSwitchError}
          onConfirm={handleEngineSwitchConfirm}
          onCancel={handleEngineSwitchCancel}
        />

        {/* Floating bottom bar — status + input */}
        <div className="shrink-0 relative bg-p-bg">
          {/* Gradient fade overlay — extends above into chat scroll area */}
          <div className="absolute left-0 right-0 bottom-full h-4 bg-linear-to-t from-p-bg to-transparent pointer-events-none" />
          <div className="max-w-4xl mx-auto">
            <ChatStatusBar
              streaming={viewedStreaming}
              warming={warming}
              startTime={turnStartTime}
              thinkingActive={thinkingActive}
              compressingActive={compressingActive}
              activeAgents={activeAgents}
              mode={mode === 'auto' ? 'dontAsk' : mode}
              // While a cross-engine pick is pending these are DISPLAY-ONLY
              // overrides — the real `model` state stays on the chat's
              // engine (it feeds handleSend/pre-warm; mutating it would warm
              // the wrong engine if the switch is cancelled).
              model={pendingEngineSwitch ? pendingEngineSwitch.model : model}
              modelValue={pendingEngineSwitch
                ? `${pendingEngineSwitch.layer}::${pendingEngineSwitch.model}`
                : modelCompound}
              costUsd={totalCost}
              contextUsed={contextUsed}
              contextMax={contextMax}
              cacheStats={cacheStats}
              meetingActive={meetingActive}
              supportsPlanMode={supportsPlanMode}
              modelOptions={agentLayerModels}
              modelGroups={modelGroups}
              interactiveAvailable={interactiveAvailable}
              interactiveOn={interactive.interactiveMode}
              interactiveDisabled={interactiveLocked}
              onInteractiveToggle={handleInteractiveToggle}
              richViewAvailable={interactive.sessionInteractive}
              richViewActive={interactive.showRichView}
              onToggleRichView={handleToggleRichView}
              hidePermissions={interactive.interactiveMode || interactive.sessionInteractive}
              interactiveActive={interactive.interactiveMode || interactive.sessionInteractive}
              // A task run's PERMISSIONS are the run's fact ('auto'
              // posture) — display-only. Its MODEL (1.5) follows the same
              // rule as every chat: while the run/session is alive the
              // picker offers the active engine's models only
              // (computeModelGroups), once dead the cross-engine expansion
              // + confirm flow applies — follow-up turns resolve
              // model/engine from the chat row, so the pick governs
              // exactly those. Interactive PTY sessions stay locked (the
              // terminal owns its process).
              modelLocked={interactive.sessionInteractive}
              modeLocked={isTaskChat}
              leftSlot={interactive.sessionInteractive && chatId
                ? <TerminalControlBar className="flex-1 min-w-0" send={(seq) => ws.sendPtyInput(chatId, utf8ToB64(seq))} />
                : undefined}
              onModeChange={handleModeChange}
              onModelChange={handleModelChange}
              // Lazy liveness re-probe on dropdown open (committed chats
              // only): headless idle-reap emits no frame, so without this
              // the cross-engine options never appear on a chat that died
              // while being viewed.
              onModelMenuOpen={chatId && chatActiveLayer
                ? () => ws.probeLiveness()
                : undefined}
              onCompactContext={
                // Manual compaction is Codex-only (thread/compact/start) and
                // headless-only (interactive users type /compact in the TUI);
                // hidden while a compaction is already in flight.
                (effectiveLayer || '').startsWith('codex')
                && !interactive.sessionInteractive && !compressingActive
                  ? () => ws.compactContext()
                  : undefined
              }
            />
          </div>
          {/* Usage limit banner */}
          {limitReached && (
            <div className="mx-4 mb-2 p-3 rounded-lg bg-p-error/10 border border-p-error/30 text-sm text-p-error">
              <strong>Usage limit reached.</strong>{' '}
              Contact your administrator to increase your limit.
            </div>
          )}
          {/* Usage limit warning toast */}
          {limitWarning && (
            <div className="fixed top-4 right-4 z-50 max-w-sm p-4 rounded-xl bg-p-accent-yellow/10 border border-p-accent-yellow/40 text-sm shadow-lg backdrop-blur-xs">
              <div className="flex items-start gap-2">
                <span className="text-p-accent-yellow text-lg leading-none">&#9888;</span>
                <div>
                  <p className="font-medium text-p-text">Usage limit warning</p>
                  <p className="text-p-text-secondary mt-0.5">
                    {limitWarning.monthly && limitWarning.monthly.percent >= 80
                      ? `You've used ${limitWarning.monthly.percent}% of your monthly limit ($${limitWarning.monthly.used.toFixed(2)} / $${limitWarning.monthly.limit?.toFixed(2)}).`
                      : limitWarning.weekly && limitWarning.weekly.percent >= 80
                        ? `You've used ${limitWarning.weekly.percent}% of your weekly limit ($${limitWarning.weekly.used.toFixed(2)} / $${limitWarning.weekly.limit?.toFixed(2)}).`
                        : 'You are approaching your usage limit.'}
                  </p>
                </div>
                <button onClick={() => setLimitWarning(null)} className="text-p-text-light hover:text-p-text ml-auto">&times;</button>
              </div>
            </div>
          )}
          <ChatInput
            value={draftInput}
            onChange={setDraftInput}
            onSend={handleSend}
            onAbort={handleAbort}
            onEditQueued={handleEditQueued}
            onEngage={handleEngage}
            disabled={!ws.connected || limitReached}
            sendDisabled={!!pendingEngineSwitch}
            streaming={(viewedStreaming && !permissionPending) || warmingUp}
            aborting={aborting}
            placeholder={pendingEngineSwitch
              ? 'Confirm or cancel the engine switch first…'
              : limitReached ? 'Usage limit reached' : viewedStreaming ? 'Type to queue a message...' : 'Type a message...'}
            queuedCount={queuedMessages.length}
            editText={editText}
            onClearEditText={() => setEditText(null)}
            pendingImages={pendingImages}
            onAddImages={(imgs) => draftKey && useChatStore.getState().addPendingImages(draftKey, imgs)}
            onRemoveImage={(id) => draftKey && useChatStore.getState().removePendingImage(draftKey, id)}
            pendingFiles={pendingFiles}
            onAddFiles={handleAddFiles}
            onRemoveFile={handleRemoveFile}
            onRetryFile={handleRetryFile}
            workspaceOpen={workspace.state.open}
            onToggleWorkspace={workspace.toggleWorkspace}
            workspaceHasNewMessage={workspace.state.hasNewMessage}
            appsOpen={appsActive}
            onToggleApps={toggleApps}
            projectsOpen={projectsActive}
            dockKind={isProjectChat ? 'project' : 'chat'}
            onToggleProjects={dockAvailable ? () => {
              // Same reveal-intent shape as toggleApps (visibility, not flag).
              if (projectsActive) { setProjectsOpen(false); return }
              if (workspace.state.open) workspace.closeWorkspace()
              setAppsOpen(false)
              setProjectsOpen(true)
            } : undefined}
            voice={{
              duplex: {
                available: !!audioCapability?.duplex?.available,
                active: duplexVoice.active,
                phase: duplexVoice.phase,
                caption: duplexVoice.caption,
                endReason: duplexVoice.endReason,
                getLevels: duplexVoice.getLevels,
                onToggle: () => {
                  if (duplexVoice.active) { duplexVoice.stop(); return }
                  if (chatId) { duplexVoice.start(); return }
                  if (!agentName) return
                  // Never-warmed chat: run the normal warmup first (no
                  // message payload — the same shape the install-retry path
                  // uses), then start once chatId + session land. A
                  // phone-mode start from the home keeps the dashboards
                  // panel showing through the chat entry (operator rule —
                  // the only phone entry not derivable from the URL).
                  keepAppsOnChatEntryRef.current = true
                  duplexPendingRef.current = true
                  ws.warmup(agentName, undefined, mode, model,
                    chatActiveLayer ?? selectedLayer ?? undefined, undefined,
                    interactive.chatExecMode || undefined,
                    currentDashboardTheme())
                },
                muted: duplexVoice.muted,
                onToggleMute: duplexVoice.toggleMute,
                onHold: duplexVoice.hold,
                onRelease: duplexVoice.release,
                onReleaseWithDraft: duplexVoice.releaseWithDraft,
              },
            }}
          />
        </div>
      </div>
      </div>
      </SearchProvider>

      {/* Notification toasts (fixed position, always rendered) */}
      {chatNotif.notificationToast}

      {/* App settings modal (native only) */}
      <AppSettingsModal open={appSettingsOpen} onClose={() => setAppSettingsOpen(false)} />
    </div>
  )
}
