/**
 * AgentChat — the main chat page. Intentionally kept as one file: its render
 * is woven through the useChatStream turn lifecycle (optimistic bubbles,
 * warmup, queueing, history paging, voice / terminal / artifact panels) whose
 * pieces share refs + closures that must read the latest state. Splitting it
 * would trade a smaller file for fragile cross-module ref plumbing. Sub-views
 * that DO stand alone were already extracted (ChatMessages, TopBar, ChatInput…).
 */
import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useParams, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { SearchProvider } from '../../contexts/SearchContext'
import { useChatStream } from '../../hooks/useChatStream'
import { useAgents, useExecutionLayers, useAgentTargetStatus } from '../../api/agents'
import { useChats, useTaskChats, fetchChatPage } from '../../api/chats'
import { useRunByChat } from '../../api/runs'
import TaskMetadata from '../../components/chat/TaskMetadata'
import ChatMessages from '../../components/chat/ChatMessages'
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'
import ChatInput, { PendingImage, PendingFile } from '../../components/chat/ChatInput'
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
import ChatStatusBar from '../../components/chat/ChatStatusBar'
import PlanPanel from '../../components/chat/plan/PlanPanel'
import TodoPanel from '../../components/chat/plan/TodoPanel'
import GoalPanel from '../../components/chat/plan/GoalPanel'
import WorkflowPanel from '../../components/chat/plan/WorkflowPanel'
import MeetingIndicator from '../../components/chat/MeetingIndicator'
import ResponsiveDrawer from '../../components/ui/ResponsiveDrawer'
import WorkspaceOverlay from '../../components/workspace/WorkspaceOverlay'
import AppsOverlay from '../../components/apps/AppsOverlay'
import { useWorkspaceState } from '../../hooks/useWorkspaceState'
import { canManageAgent, canEditAgent } from '../../lib/permissions'
import { hasAgentScope, isPersonalOnly, isSharedOnly, modeOfAgent } from '../../lib/visibility'
import { setNativeSwitchBusy } from '../../lib/nativeBridge'
import { useChatStore, newChatKey } from '../../store/chatStore'
import { useAgentPrefsStore } from '../../store/agentPrefsStore'
import { useAudioPrefsStore } from '../../store/audioPrefsStore'
import { useVoiceMode } from '../../hooks/useVoiceMode'
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
  const { data: agents } = useAgents()
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

  // Build model groups with layer-prefixed values (layer::model_id) to avoid duplicates
  const modelGroups = useMemo(() => {
    if (!layers) return undefined
    const groups: { layer: string; layerLabel: string; models: { value: string; label: string }[] }[] = []
    const pathsToShow = chatActiveLayer ? [chatActiveLayer] : agentExecutionPaths

    for (const path of pathsToShow) {
      const cap = layers[path]
      if (cap) {
        const models = (cap.models || [])
          .filter((m: { value: string }) => m.value !== '')
          .map((m: { value: string; label: string }) => ({
            value: `${path}::${m.value}`,
            label: m.label,
          }))
        if (models.length > 0) {
          groups.push({ layer: path, layerLabel: cap.display_name, models })
        }
      }
    }

    return groups.length > 0 ? groups : undefined
  }, [layers, chatActiveLayer, agentExecutionPaths])

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
  // Interactive CLI state lives in
  // `useInteractiveChat`, created after useChatStream below since it needs `ws`.

  // Notification surface — inbox/toast/badge + per-severity sound + danger
  // alarm + Web Push / FCM + the deep-link routing rule.
  // Declared before useChatStream so its WS callbacks can be passed as the
  // notification options below.
  const chatNotif = useChatNotifications()

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
      // render the terminal, NOT the pump UI. The hook flushes any text typed
      // before the terminal was ready, or — if the backend declined interactive
      // (kill-switch off / non-Claude / remote) — replays the stashed prompt as
      // a normal turn. 'none' = not interactive: fall through to the server-kick.
      const interactiveStatus = interactive.onWarmupReady(data, {
        onDecline: (t, cid) => { serverKickPendingRef.current = false; ws.sendMessage(t, cid) },
      })
      if (interactiveStatus !== 'none') {
        serverKickPendingRef.current = false
      } else if (serverKickPendingRef.current) {
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
    },
    onTitleUpdated: () => refetchChats(),
    // Live rich view: new interactive-history rows persisted while the
    // terminal ⇄ transcript toggle shows the transcript → refetch the newest
    // page (same seed path as the toggle). Trailing debounce coalesces the
    // per-batch nudges a busy turn produces.
    onChatRows: (data) => {
      if (!showRichViewRef.current) return
      if (!chatId || data.chat_id !== chatId) return
      if (chatRowsTimerRef.current) window.clearTimeout(chatRowsTimerRef.current)
      chatRowsTimerRef.current = window.setTimeout(async () => {
        chatRowsTimerRef.current = null
        try {
          const { messages: rows, has_more } = await fetchChatPage(chatId, 50)
          // Re-check: the user may have toggled back to the terminal.
          if (showRichViewRef.current) seedDbHistory(rows, has_more)
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
      // Task chats store permission_mode 'auto' (the scheduler's posture) —
      // restore it so the status bar reflects the run's real mode (rendered
      // as Don't Ask) instead of this page's 'default' seed.
      if (data.mode && data.chat_id?.startsWith('task-')) setMode(data.mode)
      // Restore the per-chat interactive toggle from the stored execution_mode.
      // The live flag stays false until a warmup_ready{interactive} arrives — a
      // dead interactive chat shows its DB history with the toggle reflected on.
      interactive.restoreFromMeta(data.execution_mode)
    },
    onChatHistoryLoaded: () => {
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
      if (!meetingActive && !bgStillRunning && document.visibilityState === 'visible') {
        // Visible/foreground tab → in-app ping, on desktop AND the native app.
        // It's visibility-gated, so a BACKGROUNDED native app pings via FCM
        // instead (never both). Plays on the WebView media stream, so it's
        // audible even with the phone on silent (like a video's audio).
        chatNotif.playPing()
      }
      // Fire an interactive switch that was deferred so it wouldn't cut
      // this (now-finished) -p turn.
      interactive.flushDeferredSwitch(chatIdRef.current)
      refetchChats()
    },
    onTurnComplete: () => {
      // Origin-routed end-of-turn ping: a hidden tab or a background chat
      // (useChatStream already drops it for the visible viewed chat, which
      // onTurnDone pings). The native app alerts via FCM instead.
      try {
        if ((window as any).Capacitor?.isNativePlatform?.()) return
      } catch { /* not native */ }
      chatNotif.playPing()
      refetchChats()
    },
    enableDefensiveRefetch: true,
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

  // Voice mode (hands-free): speak the streaming reply aloud, mic auto-sends, and
  // a mic-press barges in. Resolves native-vs-platform TTS via the same
  // capability/prefs as the SoundIcon, so it behaves identically across the
  // browser, native Android, and the platform streaming WS.
  const voiceModeEnabled = useAudioPrefsStore((s) => s.voiceModeEnabled)
  const setVoiceModeEnabled = useAudioPrefsStore((s) => s.setVoiceModeEnabled)
  const { data: audioCapability } = useChatAudioCapability()
  const voiceMode = useVoiceMode(messages, viewedStreaming)
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
    if (!model) setModel(stickyModel || agentDefaultModel)
    if (stickyMode && mode === 'default') setMode(stickyMode)
    // Seed the interactive toggle from the agent's sticky preference too, so a
    // new chat (or a page refresh on /chat/<agent>) keeps the user's last
    // interactive on/off choice. No-op unless an explicit value was set.
    interactive.seedExecMode(prefs.lastInteractive[agentName] || '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName, urlChatId, agentDefaultModel])

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
  // not sendMessage, so nothing else flips ws.streaming).
  const serverKickPendingRef = useRef(false)

  const { data: chats, refetch: refetchChats } = useChats(agentName)

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
  const { data: taskChats } = useTaskChats(agentName, tasksMode || isTaskChat)

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
  // Apps-UI open/close rules — arrival on HOME opens (incl. agent switch),
  // entering any chat closes / never auto-opens. Extracted for direct unit
  // coverage; the rules live in the hook's header comment.
  useAppsAutoOpen(agentName, urlChatId, pinnedApps, setAppsOpen)
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
      return
    }
    if (lastResumedChatIdRef.current === urlChatId) return  // already loaded
    lastResumedChatIdRef.current = urlChatId

    // Full reset (mirrors handleNewChat — most-thorough variant, safe for both
    // same-agent and cross-agent transitions; agent-specific defaults like
    // chatActiveLayer/model are re-set by onChatHistory when the chat loads).
    discardingRef.current = true
    setMessages([])
    currentMsgRef.current = null
    pendingMessageRef.current = null
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
    ws.preWarmup(agentName, model, mode, agentExecutionPath)
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

  // Upload a single pending file eagerly. Updates state in place (uploading/uploadedPath/error).
  // Kicked off when the user picks the file, not when Send is clicked — so send is near-instant.
  const uploadPendingFile = useCallback(async (file: PendingFile) => {
    const form = new FormData()
    form.append('file', file.file)
    form.append('agent', agentName || '')
    try {
      const resp = await fetch('/v1/upload', {
        method: 'POST',
        body: form,
        credentials: 'same-origin',
        signal: file.abortController?.signal,
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: 'Upload failed' }))
        throw new Error(err.detail || `Upload failed: ${resp.status}`)
      }
      const data = await resp.json()
      if (draftKey) {
        useChatStore.getState().updatePendingFile(draftKey, file.id, {
          uploading: false,
          uploadedPath: data.path,
          name: data.filename || file.name,
        })
      }
    } catch (e: any) {
      if (e?.name === 'AbortError') return  // User removed the file — no state update needed
      if (draftKey) {
        useChatStore.getState().updatePendingFile(draftKey, file.id, {
          uploading: false,
          error: e?.message || 'Upload failed',
        })
      }
    }
  }, [agentName, draftKey])

  const handleAddFiles = useCallback((files: PendingFile[]) => {
    if (!draftKey) return
    // Tag each file with uploading=true + AbortController, add to state, kick off upload.
    const tagged = files.map(f => ({
      ...f,
      uploading: true as const,
      abortController: new AbortController(),
    }))
    useChatStore.getState().addPendingFiles(draftKey, tagged)
    for (const f of tagged) {
      void uploadPendingFile(f)
    }
  }, [uploadPendingFile, draftKey])

  const handleRemoveFile = useCallback((id: string) => {
    if (!draftKey) return
    // Abort the in-flight upload BEFORE the mutator drops the entry — the
    // mutator only removes by id, the abort logic stays here.
    const target = useChatStore.getState().byChat[draftKey]?.pendingFiles.find(f => f.id === id)
    target?.abortController?.abort()
    useChatStore.getState().removePendingFile(draftKey, id)
  }, [draftKey])

  const handleSend = useCallback(
    (text: string) => {
      if (!agentName) return
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

      // Interactive CLI: the human drives the PTY directly. A live
      // session → write the line straight to the terminal; toggle-on with
      // nothing live yet → cold-start an interactive warmup (the cold start adds
      // the bubble too — reused to stream into if the backend declines). Returns
      // null when not interactive, so we fall through to the normal `-p` path.
      const routed = interactive.routeSend(text, {
        chatId, sessionId, warmingUp,
        warmupParams: { agentName, chatId: chatId || undefined, mode, model, layer: chatActiveLayer ?? selectedLayer ?? undefined },
        onColdStart: () => { addUserAndPlaceholder(text); sentWithBubbleRef.current = text; setWarmingUp(true) },
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
        serverKickPendingRef.current = true  // adopt the server-kicked turn on warmup_ready
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
    [agentName, sessionId, chatId, mode, model, chatActiveLayer, selectedLayer, ws, warmingUp, interactive.routeSend, interactive.setShowRichView, interactive.chatExecMode, pendingImages, pendingFiles, workspace, draftKey],
  )

  const handleAbort = useCallback(() => {
    if (warmingUp) {
      // Cancel before warmup completes — don't send the pending message
      pendingMessageRef.current = null
      pendingImagesRef.current = null
      pendingFilesRef.current = null
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
    currentMsgRef.current = null
    pendingMessageRef.current = null
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
    preWarmedRef.current = null
    navigate(`/chat/${agentName}`, { replace: true })
    // Trigger eager pre-warmup for the new chat. Keep the current `mode` state
    // (sticky across New Chat) so the pre-warmed session is spawned with the
    // user's selected permission mode — otherwise the session's _session_modes
    // entry is locked to "default" at start_session time and the user has to
    // toggle the dropdown to re-apply.
    // Skip the headless pre-warm when the new chat will be interactive — the
    // interactive send spawns its own PTY session (which would supersede a
    // headless pre-warm). Use the just-seeded sticky choice (state setters above
    // haven't applied yet, so `interactive.interactiveMode` is still stale).
    const willBeInteractive =
      (stickyInteractive || (currentAgent?.default_execution_mode || '')) === 'interactive'
    // Eager only for the favorite — others warm lazily on first interaction.
    if (ws.connected && !willBeInteractive && isFavoriteAgent) {
      ws.preWarmup(agentName, agentDefaultModel, mode, agentExecutionPath)
    }
  }, [agentName, agentDefaultModel, agentExecutionPath, mode, navigate, ws, workspace, currentAgent?.default_execution_mode, interactive.seedExecMode, interactive.resetSession, isFavoriteAgent])

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
    ws.preWarmup(agentName, model, mode, agentExecutionPath)
  }, [ws, agentName, urlChatId, warmingUp, sessionId, model, mode, agentExecutionPath])

  const handleSelectChat = useCallback(
    (selectedChatId: string, searchQuery?: string) => {
      if (selectedChatId === chatId && !searchQuery) return
      // Discard stale in-flight WS events from old chat (cleared in onChatHistory)
      discardingRef.current = true
      if (workspace.state.open) workspace.closeWorkspace()
      setAppsOpen(false)
      // Reset ALL state to prevent cross-chat leakage
      setMessages([])
      currentMsgRef.current = null
      pendingMessageRef.current = null
      thinkingBufRef.current = ''
      setChatId(selectedChatId)
      setSessionId(null)
      setWarmingUp(false)
      interactive.resetSession()  // cleared until the resumed chat's warmup_ready{interactive}
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
    [agentName, chatId, ws, navigate, workspace, interactive.resetSession],
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
      // composer's own cold-start rail: routeSend stashes the framed text and
      // warmup_ready types it into the fresh terminal (a backend decline to
      // headless falls back through the page's normal send via onDecline).
      // Without this, the fresh terminal opened and the prompt never landed.
      if (interactive.interactiveMode && agentName) {
        const built = buildAppActionText(app.title || app.slug, action.label, substituted)
        if ('error' in built) return { status: 'denied', reason: built.error }
        const routed = interactive.routeSend(built.framed, {
          chatId, sessionId, warmingUp,
          warmupParams: { agentName, chatId: chatId || undefined, mode, model, layer: chatActiveLayer ?? selectedLayer ?? undefined },
          onColdStart: () => { setAppsOpen(false); setWarmingUp(true) },
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
    ws.changeModel(modelId)
    setModel(modelId)
    // Sticky for the same agent's next new chat.
    if (agentName) useAgentPrefsStore.getState().setLastModel(agentName, modelId)
    // Track the selected layer (drives warmup + the dropdown highlight) WITHOUT
    // collapsing the dropdown — a not-yet-started chat keeps every layer visible.
    // The committed chatActiveLayer is set only when the chat actually starts.
    if (!ws.streaming) {
      setSelectedLayer(layer)
    }
  }, [ws, parseModelValue, agentName])

  // Interactive CLI toggle. No live session:
  // set the per-chat intent + persist it (the next send spawns the chosen mode).
  // Live session: confirm (a running process is being restarted), then
  // kill+rewarm in the target mode (deferred to turn-end if a -p turn streams).
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
    const ok = window.confirm(next
      ? 'Switch this chat to the interactive terminal? The current session restarts (your conversation is kept).'
      : 'Switch this chat to normal mode? The terminal closes and the conversation continues in chat (kept).')
    if (!ok) return
    interactive.performSwitch(next, chatId, ws.streaming)
  }, [interactive.toggle, interactive.performSwitch, sessionId, chatId, ws.streaming, agentName])

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
    <ChatMessages
      messages={messages}
      agentName={agentName}
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
      onDismissPreview={(fileId, dbMessageId) => {
        // Drop the preview block from local UI state. The DocumentPreview
        // component already called the dismiss API before invoking this.
        setMessages(prev => prev.map(m => ({
          ...m,
          blocks: m.blocks.filter(b =>
            !(b.type === 'document_preview' && b.fileId === fileId)
          ),
        })))
      }}
      streaming={viewedStreaming}
      queuedMessages={queuedMessages}
      onCancelQueued={handleCancelQueued}
      onLoadOlder={loadOlder}
      hasMoreOlder={hasMoreOlder}
      loadingOlder={loadingOlder}
    />
  )

  // --- Render ---

  return (
    <div ref={swipeRef} className="flex h-screen-safe bg-p-bg">
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
        />
      </ResponsiveDrawer>

      <SearchProvider query={findBarOpen ? findQuery : ''}>
      <div className="flex-1 flex flex-col min-w-0 relative">
        <SetupBanner />
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
              ws.preWarmup(agentName, model, mode, agentExecutionPath)
            }
          }}
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
              model={model}
              modelValue={modelCompound}
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
              modelLocked={interactive.sessionInteractive}
              leftSlot={interactive.sessionInteractive && chatId
                ? <TerminalControlBar className="flex-1 min-w-0" send={(seq) => ws.sendPtyInput(chatId, utf8ToB64(seq))} />
                : undefined}
              onModeChange={handleModeChange}
              onModelChange={handleModelChange}
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
            streaming={(viewedStreaming && !permissionPending) || warmingUp}
            aborting={aborting}
            placeholder={limitReached ? 'Usage limit reached' : viewedStreaming ? 'Type to queue a message...' : 'Type a message...'}
            queuedCount={queuedMessages.length}
            editText={editText}
            onClearEditText={() => setEditText(null)}
            pendingImages={pendingImages}
            onAddImages={(imgs) => draftKey && useChatStore.getState().addPendingImages(draftKey, imgs)}
            onRemoveImage={(id) => draftKey && useChatStore.getState().removePendingImage(draftKey, id)}
            pendingFiles={pendingFiles}
            onAddFiles={handleAddFiles}
            onRemoveFile={handleRemoveFile}
            workspaceOpen={workspace.state.open}
            onToggleWorkspace={workspace.toggleWorkspace}
            workspaceHasNewMessage={workspace.state.hasNewMessage}
            appsOpen={appsActive}
            onToggleApps={toggleApps}
            voice={{
              ttsAvailable: !!audioCapability && audioCapability.tts !== 'unavailable',
              live: voiceModeEnabled,
              onSetLive: setVoiceModeEnabled,
              speaking: voiceMode.speaking,
              onBargeIn: voiceMode.cancel,
            }}
          />
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
