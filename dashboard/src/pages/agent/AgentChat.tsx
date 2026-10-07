/**
 * AgentChat — the main chat page. Its render is woven through the
 * useChatStream turn lifecycle (optimistic bubbles, warmup, queueing, history
 * paging, voice / terminal / artifact panels) whose pieces share refs +
 * closures that must read the latest state — cross-module ref plumbing is
 * what makes splitting such a page fragile. So the page keeps the chat
 * lifecycle (the URL-change resume effect, handleSend / handleAbort /
 * handleNewChat / handleSelectChat / handleEngage), the destructured stream
 * state, messageListView and the render skeleton, and the split follows seams
 * that cannot break that guarantee: chat/useAgentChatStream.ts holds the
 * useChatStream call with its options object (+ the WS-lifecycle effects),
 * whose callbacks read the page
 * values declared after the call (`interactive`, `draftKey`, `refetchChats`)
 * through ONE `latest` ref this page assigns every render, and creates the
 * eight refs they share with the handlers here; chat/useFindBar,
 * useChatAttachments, useModelEngineSelection, useChatDuplexVoice,
 * useOverlayPanels and useInteractiveHandlers are value-returning hooks, each called at the exact
 * position the page declared its first hook (hook order and closure semantics
 * unchanged); pages/agent/chat/ChatSidePanels, ChatBanners, ChatWorkspaceSlot
 * and ChatComposerBar are presentational — every prop is computed here. The
 * three EMPTY_* constants below stay module-level here and are passed where
 * needed (a second copy breaks zustand's Object.is selector guard).
 */
import React, { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { SearchProvider } from '../../contexts/SearchContext'
import { useAgents, useExecutionLayers, useAgentTargetStatus } from '../../api/agents'
import { engineLabels, runsInteractive, supportsCompact } from '../../lib/engines'
import { useChats, useTaskChats } from '../../api/chats'
import { useRunByChat } from '../../api/runs'
import ChatMessages from '../../components/chat/ChatMessages'
import { ChatFileProvider } from '../../components/chat/ChatFileContext'
import type { DisplayMessage, MessageBlock } from '../../components/chat/types'
import type { PendingImage, PendingFile } from '../../components/chat/ChatInput'
import type { QueuedMessage } from '../../store/types'
import type { WireImage } from '../../hooks/useDashboardWs'
import TopBar from '../../components/chat/TopBar'
import AppSettingsModal from '../../components/chat/AppSettingsModal'
import { SetupBanner } from '../../components/PlatformSetupGuard'
import { ForwardingBanner } from '../../components/admin/ForwardingBanner'
import FindBar from '../../components/chat/FindBar'
import { useChatNotifications } from '../../hooks/useChatNotifications'
import { useSwipeGesture } from '../../hooks/useSwipeGesture'
import ChatHistory from '../../components/chat/ChatHistory'
import ActiveChatsPanel from '../../components/chat/ActiveChatsPanel'
import { CHAT_PHASE } from '../../lib/status/chat'
import { sendAgainPrompt } from '../../lib/messageBlocks'
import ChatComposerBar from './chat/ChatComposerBar'
import ChatBanners from './chat/ChatBanners'
import ChatSidePanels from './chat/ChatSidePanels'
import ResponsiveDrawer from '../../components/ui/ResponsiveDrawer'
import ChatWorkspaceSlot from './chat/ChatWorkspaceSlot'
import ChatDocumentPane, { useDocumentChips, useDocumentPaneForm } from './chat/ChatDocumentPane'
import AppsOverlay from '../../components/apps/AppsOverlay'
import ProjectsOverlay from '../../components/projects/ProjectsOverlay'
import { setNativeSwitchBusy } from '../../lib/nativeBridge'
import { useChatStore, newChatKey } from '../../store/chatStore'
import { useAgentPrefsStore } from '../../store/agentPrefsStore'
import { useHydrateUiPrefs } from '../../api/userUiPrefs'
import { useInteractiveChat, currentDashboardTheme } from '../../hooks/useInteractiveChat'
import { useArtifactWindows } from '../../hooks/useArtifactWindows'
import { notePushedDocument, pushedDocumentFromFrame, useDocumentPaneStore } from '../../store/documentPaneStore'
import { useAgentChatStream } from './chat/useAgentChatStream'
import { useFindBar } from './chat/useFindBar'
import { useChatAttachments } from './chat/useChatAttachments'
import {
  useModelEngineSelection, useModelEngineReconcile, useModelEngineHandlers, usePermissionModeInvariant,
} from './chat/useModelEngineSelection'
import { useChatDuplexVoice, useDuplexWakeArm, duplexVoiceProp } from './chat/useChatDuplexVoice'
import { useOverlayPanels, useAppSendPrompt, usePendingAppAction } from './chat/useOverlayPanels'
import { useInteractiveHandlers } from './chat/useInteractiveHandlers'
import { isTaskChatId } from '../../lib/session/kind'
import { isSharedOnly, modeOfAgent } from '../../lib/visibility'
import { canEditAgent, isAdmin } from '../../lib/permissions'

// Stable empty-array references for the chatStore selectors below. Zustand
// uses Object.is to detect selector-result changes — returning a fresh `[]`
// literal on every call ([] !== []) would trigger an infinite re-render
// loop (React error #185 "max update depth exceeded").
const EMPTY_QUEUED_MESSAGES: QueuedMessage[] = []
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
  // The agent row always carries its primary engine; until the agents query
  // settles the page has no engine (no default is assumed).
  const agentExecutionPath = currentAgent?.execution_path ?? ''
  const agentExecutionPaths = currentAgent?.execution_paths || (agentExecutionPath ? [agentExecutionPath] : [])
  const agentLayerModels = agentExecutionPaths.flatMap(p => layers?.[p]?.models?.filter((m: { value: string }) => m.value !== '') || [])
  const agentDefaultModel = currentAgent?.default_model || ''
  const agentDisplayName = currentAgent?.display_name
  const agentColor = currentAgent?.color || ''

  // Per-user engine access + the model/engine selection state (the committed
  // vs selected layer, liveness, the provisional cross-engine switch) —
  // see chat/useModelEngineSelection.ts.
  const {
    canRunLayer, zeroAccessible, preWarmPath,
    chatActiveLayer, setChatActiveLayer, selectedLayer, setSelectedLayer,
    processAlive, setProcessAlive,
    pendingEngineSwitch, setPendingEngineSwitch,
    engineSwitchBusy, setEngineSwitchBusy, engineSwitchError, setEngineSwitchError,
    parseModelValue,
  } = useModelEngineSelection({ agentExecutionPaths, agentExecutionPath })

  // Find bar — state, the ?q= deep link, the debounce, the Ctrl+F intercept
  // and closeFindBar (see chat/useFindBar.ts).
  const {
    findBarOpen, setFindBarOpen, findInput, setFindInput, findQuery, setFindQuery,
    pendingFindQuery, closeFindBar,
  } = useFindBar({ searchParams, setSearchParams })

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

  // The drive gate's sentence for the loaded chat (chat_history), set when
  // the chat runs as the agent and this person is below the editor tier.
  const [driveRefusal, setDriveRefusal] = useState<{ chatId: string; text: string } | null>(null)

  // Live-PTY flag mirrored into a ref for useDashboardWs's auto-attach gate —
  // the interactive hook is created after useChatStream (it needs `ws`), so
  // callbacks reach the current value through this ref, never the closure.
  const sessionInteractiveRef = useRef(false)

  // Shared chat-stream state machine — messages + status + meeting state, the
  // entire `useDashboardWs` callback object, and the shared handlers. The
  // options object (this page's WS callback tails) lives in
  // chat/useAgentChatStream.ts: it reads `interactive`, `draftKey` and
  // `refetchChats` through `latest` (assigned below), creates the eight
  // refs the callbacks share with the handlers here, and runs the WS-lifecycle
  // effects (connect, eager pre-warm, disconnect reset, post-warmup flush).
  const {
    ws,
    messages, setMessages,
    pendingSteers,
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
    costBilled,
    contextUsed, setContextUsed,
    contextMax, setContextMax,
    cacheStats,
    permissionPending, setPermissionPending,
    aborting, setAborting,
    limitReached, setLimitReached, limitReachedInfo,
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
    currentMsgRef, thinkingBufRef, abortedRef, discardingRef, sentWithBubbleRef, erroredRef,
    meetingSpeakerRef,
    handlePermissionRespond, handleQuestionAnswer, handleQuestionAnswerStructured, handleSendMessage,
    sendArtifactInteraction, sendAppAction,
    handleImplementPlan, handlePlanFetched, resolvePlanReview, finalizeAbortedTurn,
    latest,
    preWarmedRef, lastResumedChatIdRef, pendingFilesRef,
    pendingMessageRef, pendingImagesRef, serverKickPendingRef, keepAppsOnChatEntryRef,
  } = useAgentChatStream({
    agents, urlChatId, agentDefaultModel, agentExecutionPath, agentName, navigate, isFavoriteAgent, preWarmPath,
    warmingUp, setWarmingUp, chatNotif, sessionInteractiveRef, duplexActiveRef,
    setChatActiveLayer, setProcessAlive, setPendingEngineSwitch, setEngineSwitchBusy, setEngineSwitchError,
    pendingFindQuery, setFindInput, setFindQuery, setFindBarOpen, setDriveRefusal,
  })

  // Interactive CLI — per-chat toggle + live-PTY flag
  // + send/warmup routing. Created here (after
  // useChatStream) since it needs `ws`; the stream's option callbacks reach it
  // through `latest.current.interactive` (assigned below, every render).
  // Live rich view (onChatRows): the debounce timeout must read the CURRENT
  // toggle state, not its closure's render — mirror it in a ref.
  const showRichViewRef = useRef(false)
  const interactive = useInteractiveChat(ws, currentAgent?.default_execution_mode || '')
  showRichViewRef.current = interactive.showRichView
  sessionInteractiveRef.current = interactive.sessionInteractive

  // Interactive-CLI display/file-tools artifact windows.
  // Lifted here so the minimized dock can render in the top-left panel stack
  // (below Todo/Workflow) while the open windows float inside TerminalView. The
  // empty chatId when not interactive clears the windows + drops the subscription.
  // A terminal chat's document pushes go to the document pane, not a window.
  const onTerminalDocument = useCallback((evt: any) => {
    const doc = pushedDocumentFromFrame(evt)
    if (doc && chatIdRef.current) notePushedDocument(chatIdRef.current, doc)
  }, [chatIdRef])
  const artifacts = useArtifactWindows(ws, interactive.sessionInteractive && chatId ? chatId : '', onTerminalDocument)
  // Docked as the right half, or a window over the slot (a phone, a narrow
  // window, a terminal chat).
  const documentPaneForm = useDocumentPaneForm(interactive.interactiveMode, chatId)

  // Compound model value for dropdown matching (layer::model_id)
  const modelCompound = `${chatActiveLayer || selectedLayer || agentExecutionPath}::${model}`
  // The permission modes the effective engine declares (committed → selected
  // → agent default): the menu offers exactly those; undefined until the
  // catalog loads.
  const effectiveLayer = chatActiveLayer || selectedLayer || agentExecutionPath
  const permissionModes = layers?.[effectiveLayer]?.permission_modes
  // Interactive CLI: show the toggle when one of the agent's engines has a
  // native TUI (`runtime.supports_interactive_pty` on its descriptor). Gated
  // on AGENT capability — not the selected model — so it stays put when a
  // model on an engine without a TUI is picked (the toggle is simply ignored
  // for that layer). Hidden for agents with no such engine, until the catalog
  // loads, and platform-wide when the interactive kill-switch is off
  // (sessions always spawn headless then).
  const interactiveAvailable =
    agentExecutionPaths.some(p => runsInteractive(layers?.[p]))
    && user?.feature_flags?.interactive_terminal_enabled !== false
  // The toggle is free to flip any time EXCEPT while a cold-start is warming or a
  // live switch (kill+rewarm) is in flight — both would race a second
  // toggle. A live session is switchable (via confirm).
  const interactiveLocked = warmingUp || interactive.switching
  // A chat that runs as the agent takes the editor tier to change its mode,
  // model or terminal (the server refuses the pick and echoes the stored
  // value): the loaded chat's refusal from the server, or, for a new chat
  // on a Shared-only agent, the same rule read from this person's role.
  const pickersLockReason = chatId
    ? (!isTaskChatId(chatId) && driveRefusal && driveRefusal.chatId === chatId ? driveRefusal.text : '')
    : (currentAgent && agentName && isSharedOnly(modeOfAgent(currentAgent))
        && !canEditAgent(user, agentName)
      ? 'This agent is set to Shared only, so its chats run as the agent itself, which takes the editor role or above.'
      : '')
  usePermissionModeInvariant(mode, permissionModes, setMode)

  // A live meeting/voice session would be lost on an install switch — flag the
  // native switcher so it confirms first (LLM streaming is reported separately).
  useEffect(() => {
    setNativeSwitchBusy(meetingActive)
    return () => setNativeSwitchBusy(false)
  }, [meetingActive])
  // Which agent's NEW-chat view already had its exec-mode reset+seed (the
  // lastResumedChatIdRef twin for the !urlChatId branch). Guards the URL
  // effect's reset to once per new-chat visit; handleNewChat — which
  // resets+seeds inline — pre-stamps it to skip the duplicate.
  const newChatModeSeededRef = useRef<string | null>(null)

  // Draft text — persisted to localStorage so it survives chat nav + reload.
  // On the new-chat page (no chat_id yet), the slice is keyed by a synthetic
  // `__new__:<agent>` id; warmup_started transfers it onto the real chat_id.
  const draftKey = chatId ?? (agentName ? newChatKey(agentName) : '')
  const draftInput = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.draftInput ?? '' : ''))
  // "Getting ready…" badge signal — true from send until warmup_ready. warmingUp
  // is the local send-time flag; the chatStore 'warming' status also covers a
  // resumed in-flight warmup after a refresh/navigate.
  const warmingStatus = useChatStore((s) => (draftKey ? s.byChat[draftKey]?.status === CHAT_PHASE.WARMING : false))
  const warming = warmingUp || warmingStatus
  // Stop button / live-input state derive from the VIEWED chat's slice (per-chat),
  // NOT connection-global ws.streaming — else the stop button + "type to queue" leak
  // onto whatever chat you switch to while another streams. The slice is set
  // 'streaming' by every turn-start path (queue_sent / server_turn_start
  // / live_state / server-kick) and back to 'ready' on done/aborted, keyed by chat_id.
  const viewedStreaming = useChatStore((s) => (chatId ? s.byChat[chatId]?.status === CHAT_PHASE.STREAMING : false))
  // Pin-vs-current target mismatch for the VIEWED chat. Read from the
  // per-chat slice (warmup_ready stores it there) rather than useChatStream
  // state so the sidebar kebab reads the exact same fact — and the slice is
  // cleared by the first mismatch-free warmup_ready, e.g. after a move.
  const targetMismatch = useChatStore((s) => (chatId ? s.byChat[chatId]?.targetMismatch ?? null : null))

  // modelGroups + the engine-switch resets, the model/mode seed and the
  // model ↔ layer reconciliation for new chats (chat/useModelEngineSelection.ts).
  const { modelGroups } = useModelEngineReconcile({
    layers, agentExecutionPaths, chatActiveLayer, processAlive, canRunLayer, viewedStreaming, warming, model,
    setPendingEngineSwitch, setEngineSwitchBusy, setEngineSwitchError, chatId, agentName, sessionId, setProcessAlive,
    urlChatId, agentDefaultModel, setModel, setMode, selectedLayer, setSelectedLayer, currentAgent, parseModelValue,
  })

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

  // Full-duplex conversation mode — the duplex controller, fresh-chat start,
  // exit note, ?wake=1 consumption and wake-silence auto-close (chat/useChatDuplexVoice.ts).
  const {
    audioCapability, duplexVoice, duplexPendingRef, duplexExitNoteRef,
    wakeRequested, setWakeRequested, wakeInitiatedRef,
  } = useChatDuplexVoice({ chatId, warming, duplexActiveRef, searchParams, setSearchParams, chatNotif })
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

  const [appSettingsOpen, setAppSettingsOpen] = useState(false)
  const isNative = !!(window as any).Capacitor?.isNativePlatform?.()

  // Swipe gestures for chat history drawer (mobile only)
  const swipeRef = useRef<HTMLDivElement>(null)
  useSwipeGesture(swipeRef, {
    onSwipeRight: () => { if (!historyOpen) setHistoryOpen(true) },
    onSwipeLeft: () => { if (historyOpen) setHistoryOpen(false) },
  })

  // A first send held because the AGENTS QUERY hadn't resolved yet (no
  // explicit per-chat mode → the effective interactive mode is unknowable
  // until the agent default arrives). The bubble + warming badge are shown at
  // hold time; the flush effect below routes it once the query settles.
  const heldForAgentsRef = useRef<{
    text: string
    images?: WireImage[]
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
  // The stream callbacks read these late-bound values through `latest`
  // (chat/useAgentChatStream.ts) — assigned every render, right after the
  // last of them exists.
  latest.current = { interactive, draftKey, refetchChats }

  // ---- Task mode ----
  // Task runs render on this page: a `task-…` chat id marks the open chat as
  // a task-run chat, and its latest run feeds the pinned TaskMetadata popup.
  // The sidebar's Task history toggle is page state so ?tasks=1 deep links
  // (notifications, the /runs resolver, Active-now task rows) open with the
  // task view on.
  const isTaskChat = isTaskChatId(chatId)
  const { data: taskRun } = useRunByChat(isTaskChat ? chatId : null)
  const [tasksMode, setTasksMode] = useState(() => searchParams.get('tasks') === '1')
  useEffect(() => {
    if (searchParams.get('tasks') === '1') setTasksMode(true)
  }, [searchParams])
  // The task-chat list also backs the row lookups below (origin / delegation
  // markers) — task chats never appear in the chat-mode list.
  const { data: taskChats } = useTaskChats(
    agentName, tasksMode || isTaskChat, duplexVoice.active)

  // ---- Overlays: workspace, pinned apps, Dock (see chat/useOverlayPanels.ts) ----
  const {
    workspace, canManageThisAgent, canWriteThisWorkspace, recoverRequested, setRecoverRequested,
    appsActive, setAppsOpen, showHomeActive, toggleApps, appsActiveId, selectApp,
    setProjectsOpen, isProjectChat, chatPins, dockAvailable, projectsActive,
  } = useOverlayPanels({
    messages, agentName, user, chatId, searchParams, setSearchParams, urlChatId, chats, taskChats, keepAppsOnChatEntryRef,
  })
  // An overlay filling the chat's slot hides the document pane and its chips.
  let slotOverlay = workspace.state.open || appsActive
  slotOverlay = slotOverlay || projectsActive
  const documentChips = useDocumentChips(chatId, slotOverlay)
  // The composer's shrink and regrow animate only over the message list:
  // every row the slot gains or loses during an animation re-lays out what
  // else fills it (a live terminal resizes its PTY and redraws, an overlay or
  // a floating Collabora frame re-lays out), so those get an instant change.
  const floatingPaneShown = useDocumentPaneStore((s) => {
    const pane = chatId ? s.byChat[chatId] : undefined
    return !!pane && pane.open && !pane.minimized
  }) && documentPaneForm === 'floating'
  const composerAnimates = !interactive.sessionInteractive && !slotOverlay && !floatingPaneShown

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

  // --- Handlers ---

  // Eager file uploads — the composer's attachment chips (chat/useChatAttachments.ts).
  const { handleAddFiles, handleRemoveFile, handleRetryFile } = useChatAttachments({ agentName, draftKey })

  const handleSend = useCallback(
    (text: string, again?: { images?: PendingImage[]; files?: PendingFile[] }) => {
      if (!agentName) return
      // Live conversation: a typed send rides the duplex socket as a
      // SPOKEN-mode turn — the reply comes back as TTS, and none of the
      // overlay-closing below runs (an open app view survives the
      // send). Attachments can't ride the frame — fall through to a
      // normal typed send when any are pending, or if the socket is gone.
      // Send again (the turn-ended card) is a typed turn of its own.
      if (!again && duplexActiveRef.current && pendingImages.length === 0
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
      erroredRef.current = false  // a new turn: its own done refetches as usual
      // Draft persisted only as long as it's unsent. Clear immediately so a
      // tab crash mid-streaming doesn't leave the just-sent text dangling.
      // Send again leaves the composer as it is.
      if (draftKey && !again) useChatStore.getState().clearDraft(draftKey)
      // Drop the workspace/apps overlay when the user sends — they're done
      // browsing and should see the turn. The path/view memory persists so
      // re-opening returns to the same folder/tab.
      if (workspace.state.open) workspace.closeWorkspace()
      setAppsOpen(false)

      // Capture pending images and files before clearing. Files were uploaded eagerly
      // on pick, so each has uploadedPath set by now (the Send button is disabled
      // while any upload is still in-flight).
      // Send again (the turn-ended card) carries the last prompt's own
      // attachments by their saved paths and leaves the composer's alone.
      const images = again
        ? (again.images?.length ? again.images : undefined)
        : pendingImages.length > 0 ? [...pendingImages] : undefined
      const files = again
        ? (again.files?.length ? again.files : undefined)
        : pendingFiles.length > 0 ? [...pendingFiles] : undefined
      if (draftKey && !again) {
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
          // A fresh photo renders from its data URL; one handed back from a
          // cancelled queued message from its saved path (the files URL).
          blocks.push({
            type: 'image_attachments',
            images: images.map(i => i.base64 ?? i.name),
            paths: images.some(i => i.path) ? images.map(i => i.path ?? null) : undefined,
          })
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

      const wsImages = images?.map(i => ({ base64: i.base64, path: i.path, name: i.name }))
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
        warmupParams: {
          agentName, chatId: chatId || undefined, mode, model,
          layer: chatActiveLayer ?? selectedLayer ?? undefined,
        },
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

  // A chip is its author's to cancel (an admin's: any); a chip a 1.7.0
  // proxy minted names no author.
  const mayCancelQueued = useCallback(
    (item: QueuedMessage) => !item.authorSub || item.authorSub === user?.sub || isAdmin(user),
    [user],
  )

  // The turn-ended card's Send again: the person's messages of the turn
  // that ended (the one the card is in, an earlier turn's card included)
  // go out again as one new turn, verbatim (sendAgainPrompt).
  const handleSendAgain = useCallback((messageId: string) => {
    const again = sendAgainPrompt(messages, messageId)
    if (again) handleSend(again.text, { images: again.images, files: again.files })
  }, [handleSend, messages])

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

  // Wake word ARM (?wake=1 was consumed above into wakeRequested) — reuses
  // handleNewChat for the stale-state reset (chat/useChatDuplexVoice.ts).
  useDuplexWakeArm({
    wakeRequested, ws, warming, audioCapability, urlChatId, chatId, setWakeRequested, duplexVoice,
    wakeInitiatedRef, handleNewChat, agentName, keepAppsOnChatEntryRef, duplexPendingRef,
    mode, model, chatActiveLayer, selectedLayer, interactive,
  })
  // ChatInput's ready-made duplex voice prop (chat/useChatDuplexVoice.ts).
  const voice = duplexVoiceProp({
    audioCapability, duplexVoice, chatId, agentName, keepAppsOnChatEntryRef, duplexPendingRef,
    ws, mode, model, chatActiveLayer, selectedLayer, interactive,
  })

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

  // App send_prompt router (see chat/useOverlayPanels.ts).
  const { handleAppSendPrompt } = useAppSendPrompt({
    interactive, chatId, ws, sessionId, warmingUp, mode, model, chatActiveLayer, selectedLayer,
    setAppsOpen, setWarmingUp, serverKickPendingRef, sendAppAction, agentName, keepAppsOnChatEntryRef, handleSelectChat,
  })
  usePendingAppAction({ chatId, agentName, connected: ws.connected, handleAppSendPrompt })

  const handleModeChange = useCallback((m: string) => {
    ws.changeMode(m, chatId)
    setMode(m)
    // Sticky for the same agent's next new chat.
    if (agentName) useAgentPrefsStore.getState().setLastMode(agentName, m)
  }, [ws, agentName, chatId])

  // Codex plan card "Implement": leave plan mode (→ the next turn clears codex's
  // plan collaboration mode) and kick the build turn. Codex has no plan file, so
  // this replaces the Claude implement_plan (session-recreate) path.
  const handleImplementPlanCodex = useCallback((m: string) => {
    handleModeChange(m)
    handleSendMessage('Implement the plan you proposed above.')
  }, [handleModeChange, handleSendMessage])

  // handleModelChange + the cross-engine switch confirm/cancel (chat/useModelEngineSelection.ts).
  const { handleModelChange, handleEngineSwitchConfirm, handleEngineSwitchCancel } = useModelEngineHandlers({
    ws, parseModelValue, agentName, chatId, chatActiveLayer, viewedStreaming, warming, isTaskChat,
    setEngineSwitchBusy, setEngineSwitchError, setPendingEngineSwitch, setModel, setSelectedLayer, pendingEngineSwitch,
  })

  // Interactive-CLI + queue handlers (chat/useInteractiveHandlers.ts).
  const {
    handleInteractiveToggle, handleTerminalExit, handleToggleRichView, handleCancelQueued, handleEditQueued,
  } = useInteractiveHandlers({
    agentName, sessionId, chatId, interactive, viewedStreaming, ws, seedDbHistory, draftKey, queuedMessages, setEditText,
  })

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
        dialogsInTerminal={interactive.sessionInteractive}
        onSendMessage={handleSendMessage}
        onArtifactInteraction={sendArtifactInteraction}
        onPlanFetched={handlePlanFetched}
        streaming={viewedStreaming}
        queuedMessages={queuedMessages}
        pendingSteers={pendingSteers}
        onCancelQueued={handleCancelQueued}
        mayCancelQueued={mayCancelQueued}
        onSendAgain={handleSendAgain}
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
      <ForwardingBanner />
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

        {/* The chat column and, docked beside it, the document pane: the
            column narrows as the pane opens. The TopBar spans both. */}
        <div className="flex-1 flex min-h-0 min-w-0">
        <div className="flex-1 flex flex-col min-w-0 min-h-0 relative overflow-clip">

        {/* Find bar */}
        {findBarOpen && (
          <FindBar value={findInput} onChange={setFindInput} onClose={closeFindBar} />
        )}

        <ChatSidePanels
          workspace={workspace} isTaskChat={isTaskChat} taskRun={taskRun} costBilled={costBilled}
          sessionPlans={sessionPlans} currentGoal={currentGoal} meetingActive={meetingActive}
          meetingParticipants={meetingParticipants} meetingSpeaker={meetingSpeaker}
          meetingLeftParticipants={meetingLeftParticipants} currentTodos={currentTodos} workflows={workflows}
          artifacts={artifacts} documentChips={documentChips}
        />

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
            The workspace overlay swaps this slot in place when toggled. The
            floating document pane sits over this slot only, above the
            banners and the composer. */}
        <div className="relative flex-1 min-h-0 flex flex-col">
        {workspace.state.open && agentName ? (
          <ChatWorkspaceSlot
            isTaskChat={isTaskChat} currentAgent={currentAgent} taskRun={taskRun} agentName={agentName}
            canManageThisAgent={canManageThisAgent} canWriteThisWorkspace={canWriteThisWorkspace} workspace={workspace}
            recoverRequested={recoverRequested} setRecoverRequested={setRecoverRequested}
          />
        ) : appsActive && agentName ? (
          <div className="flex-1 min-h-0 flex flex-col">
            <AppsOverlay
              agent={agentName} onSendPrompt={handleAppSendPrompt} topPadding={!showHomeActive}
              activeId={appsActiveId} onSelect={selectApp}
            />
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
        {chatId && documentPaneForm === 'floating' && (
          <ChatDocumentPane key={chatId} chatId={chatId} placement="floating" hidden={slotOverlay} userSub={user?.sub ?? ''} />
        )}
        </div>

        <ChatBanners
          sessionExecutionTarget={sessionExecutionTarget} sessionFallbackReason={sessionFallbackReason}
          offlineMachineName={offlineMachineName} zeroAccessible={zeroAccessible} chatId={chatId}
          pendingEngineSwitch={pendingEngineSwitch} targetMismatch={targetMismatch}
          viewedStreaming={viewedStreaming} warming={warming} ws={ws} agentName={agentName} mode={mode}
          model={model} chatActiveLayer={chatActiveLayer} selectedLayer={selectedLayer}
          interactive={interactive} preWarmedRef={preWarmedRef} preWarmPath={preWarmPath} layers={layers}
          agentLayerModels={agentLayerModels} engineSwitchBusy={engineSwitchBusy}
          engineSwitchError={engineSwitchError} handleEngineSwitchConfirm={handleEngineSwitchConfirm}
          handleEngineSwitchCancel={handleEngineSwitchCancel}
        />

        {/* Floating bottom bar — status + input */}
        <ChatComposerBar
          viewedStreaming={viewedStreaming} warming={warming} turnStartTime={turnStartTime}
          thinkingActive={thinkingActive} compressingActive={compressingActive} activeAgents={activeAgents}
          mode={mode} pendingEngineSwitch={pendingEngineSwitch} model={model} modelCompound={modelCompound}
          totalCost={totalCost} costBilled={costBilled} contextUsed={contextUsed} contextMax={contextMax}
          cacheStats={cacheStats} meetingActive={meetingActive} permissionModes={permissionModes}
          agentLayerModels={agentLayerModels} modelGroups={modelGroups}
          interactiveAvailable={interactiveAvailable} interactive={interactive}
          interactiveLocked={interactiveLocked} pickersLockReason={pickersLockReason}
          handleInteractiveToggle={handleInteractiveToggle}
          handleToggleRichView={handleToggleRichView} isTaskChat={isTaskChat} chatId={chatId} agentName={agentName} ws={ws}
          handleModeChange={handleModeChange} handleModelChange={handleModelChange}
          chatActiveLayer={chatActiveLayer} effectiveLayer={effectiveLayer}
          supportsCompact={supportsCompact(layers, effectiveLayer)} limitReached={limitReached}
          limitReachedInfo={limitReachedInfo} engineLabels={engineLabels(layers)}
          limitWarning={limitWarning} setLimitWarning={setLimitWarning} draftInput={draftInput}
          setDraftInput={setDraftInput} handleSend={handleSend} handleAbort={handleAbort}
          handleEditQueued={handleEditQueued} handleEngage={handleEngage} permissionPending={permissionPending}
          warmingUp={warmingUp} aborting={aborting} queuedMessages={queuedMessages} editText={editText}
          setEditText={setEditText} pendingImages={pendingImages} draftKey={draftKey}
          pendingFiles={pendingFiles} handleAddFiles={handleAddFiles} handleRemoveFile={handleRemoveFile}
          handleRetryFile={handleRetryFile} workspace={workspace} appsActive={appsActive}
          toggleApps={toggleApps} setAppsOpen={setAppsOpen}
          projectsActive={projectsActive} isProjectChat={isProjectChat} dockAvailable={dockAvailable}
          setProjectsOpen={setProjectsOpen}
          voice={voice} composerAnimates={composerAnimates}
        />
        </div>
        {chatId && documentPaneForm === 'docked' && (
          <ChatDocumentPane key={chatId} chatId={chatId} placement="docked" hidden={slotOverlay} userSub={user?.sub ?? ''} />
        )}
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
