import { useState, useRef, useEffect, useCallback, useMemo, type SetStateAction } from 'react'
import { useDashboardWs } from './useDashboardWs'
import type { ThreadGoal } from './useDashboardWs.types'
import type { LimitPayload } from '../api/usage'
import { getDeviceLocation } from '../lib/geolocation'
import { TARGET_LOCAL } from '../lib/placement'
import type { DisplayMessage } from '../components/chat/types'

import type { ActiveAgent } from '../components/chat/ChatStatusBar'
import type { WorkflowLive } from '../components/chat/plan/WorkflowPanel'
import type { SessionPlan } from '../components/chat/plan/PlanPanel'
import { useChatMessages } from './useChatMessages'
import type { UseChatStreamOptions } from './chatStream/types'
import type { StreamCore } from './chatStream/core'
import { useMeetingIdentity } from './chatStream/useMeetingIdentity'
import { useQueueAndSteer } from './chatStream/useQueueAndSteer'
import { useChatHistory } from './chatStream/useChatHistory'
import { useTurnEnd } from './chatStream/useTurnEnd'
import { useStreamBlocks } from './chatStream/useStreamBlocks'
import { useArtifactChannels } from './chatStream/useArtifactChannels'
import { DELEGATE_RESULT, RUN_STATUS } from '../lib/status/run'
import { errorText } from '../lib/http/apiJson'
import { ASK_MODE, AUTO_REVIEW_MODE } from '../lib/engines/modes'

export { warmupFailSubtype } from './chatStream/useTurnEnd'

/**
 * The shared `/ws/dashboard` streaming state machine — the single source of
 * truth for AgentChat (live chats and task chats alike). Owns the
 * message/block reconstruction, the streaming status state, the meeting state
 * machine, and the entire `useDashboardWs` callback object. Page-specific
 * lifecycle (warmup vs run-loading, chat sidebar, pre-warmup, notification UI,
 * model dropdown, workspace, draft/queue storage, send/abort entry points)
 * stays in the page and is wired in through the options above.
 */

export function useChatStream(options: UseChatStreamOptions) {
  const { agents } = options

  // --- Core state ---
  const [chatId, setChatIdState] = useState<string | null>(options.initialChatId ?? null)
  // Mirror chatId in a ref so the WS callbacks see the current value without
  // re-binding, and can discard stale events from a previously-resumed chat.
  const chatIdRef = useRef<string | null>(chatId)
  useEffect(() => { chatIdRef.current = chatId }, [chatId])
  const [sessionId, setSessionId] = useState<string | null>(null)
  // Track current meeting speaker for live message identity. Created here (not in
  // useChatMessages) because handlers that stay in this hook also read/write it.
  const meetingSpeakerRef = useRef<{slug: string; displayName: string; color: string} | null>(null)
  // Message list + lazy chat-history pagination (scroll-back) + the message-
  // assembly helpers live in useChatMessages. currentMsgRef is created there and
  // shared back here. The pagination internals (rawRowsRef / oldestLoadedIdRef /
  // loadingOlderRef / setHasMoreOlder / setLoadingOlder) are returned because
  // onChatHistory — which stays in this hook — resets them.
  const {
    messages, setMessages, currentMsgRef, reviewNudgeRef, historyHeldRef,
    rawRowsRef, planRowsRef, oldestLoadedIdRef, loadingOlderRef, resetWindow,
    hasMoreOlder, loadingOlder, setHasMoreOlder, setLoadingOlder,
    appendBlock, seedDbHistory: seedDbRows, loadOlder, removePlaceholders,
    appendToLastTextBlock, updateToolBlock, updateToolBlockByName,
    resolvePermission, updateSubagentActive, updateCommandActive,
    ensureAssistantMsg, upsertPreviewBlock,
  } = useChatMessages({ agents, meetingSpeakerRef, chatIdRef })
  const [mode, setMode] = useState(options.defaultMode ?? AUTO_REVIEW_MODE)
  const [model, setModel] = useState('')

  // Execution target for the active session (set by warmup_ready). When the
  // session falls back to local, fallbackReason is set so the header can render
  // an amber badge; offlineMachineName names the user's offline override.
  const [sessionExecutionTarget, setSessionExecutionTarget] = useState<string>(TARGET_LOCAL)
  const [sessionFallbackReason, setSessionFallbackReason] = useState<string | null>(null)
  const [offlineMachineName, setOfflineMachineName] = useState<string>('')

  // Status bar state
  const [turnStartTime, setTurnStartTime] = useState<number | null>(null)
  const [thinkingActive, setThinkingActive] = useState(false)
  const [compressingActive, setCompressingActive] = useState(false)
  // The live status-bar badges are a PURE DERIVATION of the message blocks —
  // the single source of truth that already tracks fg/bg subagents + delegates
  // with their type + start/finish. No parallel activeAgents array to hand-sync;
  // every handler mutates ONLY the blocks (isActive / status) and the badges
  // follow. ChatStatusBar still consumes an ActiveAgent[].
  const activeAgents = useMemo<ActiveAgent[]>(() => {
    const out: ActiveAgent[] = []
    for (const msg of messages) {
      if (msg.role !== 'assistant') continue
      for (const b of msg.blocks) {
        if (b.type === 'subagent' && b.isActive) {
          out.push({
            id: b._toolId || `sub-${out.length}`,
            description: b.description || 'Subagent',
            type: b.subagentType || 'general-purpose',
            startTime: 0,
            background: !!b._background,
          })
        } else if (b.type === 'delegate' && b.status === RUN_STATUS.RUNNING) {
          out.push({
            id: `delegate-${b._taskId || b.taskName}`,
            description: `${b.taskName} → ${b.agent}`,
            type: 'delegate',
            startTime: 0,
          })
        } else if (b.type === 'bgcommand' && b.isActive) {
          out.push({
            id: b._toolId || `cmd-${out.length}`,
            description: b.description || b.command || 'Background command',
            type: 'bgcommand',
            startTime: 0,
            background: true,
          })
        }
      }
    }
    return out
  }, [messages])
  // Mirror of messages for stale-closure-free reads inside WS callbacks
  // (onDone derives "is a bg subagent still running" to gate the turn ping).
  const messagesRef = useRef<DisplayMessage[]>([])
  useEffect(() => { messagesRef.current = messages }, [messages])
  const [totalCost, setTotalCost] = useState(0)
  // What the gauge shows: the money (an API key's or the relay's turns and
  // every tool fee), never a subscription's or a local model's activity
  // estimate, which totalCost still carries for Usage. toolCost is the fees'
  // share of it.
  const [billedCost, setBilledCost] = useState(0)
  const [toolCost, setToolCost] = useState(0)
  // Whether the cost gauge shows: the newest turn's credential kind (an API
  // key / the relay bill real money; a subscription or a local model only
  // estimates activity). totalCost keeps accumulating either way.
  const [costBilled, setCostBilled] = useState(true)
  const [contextUsed, setContextUsed] = useState(0)
  const [contextMax, setContextMax] = useState(0)
  const [cacheStats, setCacheStats] = useState({ cacheRead: 0, cacheWrite: 0, inputTokens: 0, outputTokens: 0 })
  const [permissionPending, setPermissionPending] = useState(false)
  const [aborting, setAborting] = useState(false)
  const [limitReached, setLimitReached] = useState(false)
  // Which budget blocked (the platform periods, the own API-key `self`
  // periods, or the pool cap) — what the composer's banner names.
  const [limitReachedInfo, setLimitReachedInfo] = useState<LimitPayload | null>(null)
  const [limitWarning, setLimitWarning] = useState<LimitPayload | null>(null)

  // Plans panel state
  const [sessionPlans, setSessionPlans] = useState<SessionPlan[]>([])
  // A scroll-back rebuild reads the plan rows as the panel shows them now.
  useEffect(() => { planRowsRef.current = sessionPlans }, [sessionPlans, planRowsRef])
  // Track mode before plan mode so we can restore
  const prePlanModeRef = useRef<string>(ASK_MODE)

  // Todo panel state (from TodoWrite tool events)
  const [currentTodos, setCurrentTodos] = useState<Array<{content: string; status: string; activeForm?: string}>>([])
  // Codex thread goal — null = no goal (the GoalPanel renders nothing).
  const [currentGoal, setCurrentGoal] = useState<ThreadGoal | null>(null)
  // Live dynamic-workflow (Workflow tool) trees, keyed by tool_use_id.
  const [workflows, setWorkflows] = useState<WorkflowLive[]>([])

  // Guard against stale chunks after abort creating phantom bubbles. Armed by
  // the page's handleAbort, disarmed only at a terminal frame (aborted/done),
  // on the next send, or on a chat (re)load — a graceful abort keeps the
  // engine draining for a beat, and those straggler chunks must not reopen a
  // fresh assistant header after finalizeAbortedTurn sealed the message.
  const abortedRef = useRef(false)
  // Guard against stale WS events during chat switch / run switch. Set true by
  // the page on navigation, cleared in onChatHistory / onWarmupReady. While
  // true, all streaming callbacks are no-ops (prevents ghost messages, stale
  // warmups hijacking state, wrong cost/context contamination).
  const discardingRef = useRef(false)
  const thinkingBufRef = useRef('')
  // True when the current view's streaming message was (re)built from a
  // live_state snapshot (mid-turn attach). The snapshot + post-attach frames
  // SHOULD add up to the full turn, but the attach can race the turn's end —
  // any tail lost in that window is invisible and permanent without a
  // reconcile. Consumed in onDone: a seeded view refetches history once at
  // turn end (DB is authoritative — the pump persists before signaling).
  const liveStateSeededRef = useRef(false)

  // The turn ended on an error (a pump's `error` frame, which names the
  // chat and its ending): the card renders live from the frame, so the
  // `done` that follows must not refetch the history (a full remount of the
  // list for nothing). Cleared at the next send and on a chat switch.
  const erroredRef = useRef(false)
  // A chat switch (the page's, or a minted chat's) leaves the error card
  // with the chat it belongs to.
  const setChatId = useCallback((next: SetStateAction<string | null>) => {
    erroredRef.current = false
    setChatIdState(next)
  }, [])
  // What the concern hooks under hooks/chatStream/ share (core.ts): one
  // plain object per render, so every handler closes over this render.
  const core: StreamCore = {
    options, agents, seedDbRows, chatId,
    chatIdRef, discardingRef, abortedRef, erroredRef, liveStateSeededRef, thinkingBufRef, meetingSpeakerRef,
    messagesRef,
    prePlanModeRef, mode, setMode, setSessionId,
    setTurnStartTime, setThinkingActive, setCompressingActive, setPermissionPending, setAborting, setWorkflows,
    setTotalCost, setBilledCost, setToolCost, setCostBilled, setContextUsed, setContextMax, setCacheStats, setSessionPlans, setCurrentTodos,
    setCurrentGoal, setLimitWarning, setLimitReached, setLimitReachedInfo,
    messages, setMessages, currentMsgRef, reviewNudgeRef, historyHeldRef, rawRowsRef, planRowsRef, oldestLoadedIdRef,
    loadingOlderRef, resetWindow, hasMoreOlder, loadingOlder, setHasMoreOlder, setLoadingOlder, loadOlder,
    appendBlock, removePlaceholders, appendToLastTextBlock, updateToolBlock, updateToolBlockByName,
    resolvePermission, updateSubagentActive, updateCommandActive, ensureAssistantMsg, upsertPreviewBlock,
  }
  const meeting = useMeetingIdentity(core)
  const {
    meetingActive, setMeetingActive, meetingParticipants, setMeetingParticipants,
    meetingSpeaker, setMeetingSpeaker, meetingRound, setMeetingRound, meetingMaxRounds, meetingLeftParticipants,
  } = meeting
  const queue = useQueueAndSteer(core, meetingActive)
  const {
    pendingSteers, editText, setEditText, sentWithBubbleRef, sentWithBubbleIdRef, flushPendingSteer,
  } = queue
  const history = useChatHistory(core, {
    resetSteers: queue.resetSteers,
    applyMeetingRestore: meeting.applyMeetingRestore,
    noteLiveSpeaker: meeting.noteLiveSpeaker,
    applyLiveStateMeeting: meeting.applyLiveStateMeeting,
  })
  const { seedDbHistory } = history

  /** Shared cleanup for a stopped turn. Marks any still-running tool /
   * subagent / delegate blocks as `failed` (red X) across all assistant
   * messages, clears thinking/compressing/meeting state, and empties the
   * activity sidebars. Idempotent — safe to call multiple times.
   *
   * Called proactively from the page's handleAbort so the UI clears
   * immediately even if the server's `aborted` event is delayed or dropped,
   * and again from the `onAborted` handler as a safety net.
   */
  const finalizeAbortedTurn = useCallback(() => {
    // NOTE: abortedRef stays ARMED here — the graceful abort keeps the engine
    // draining for a beat, and its straggler chunks must not reopen a fresh
    // assistant header after this seals the message. The guard disarms at a
    // terminal frame (aborted/done), the next send, or a chat (re)load.
    flushPendingSteer(false)  // an accepted steer still owes its user bubble
    setAborting(false)
    setMessages((prev) => prev
      .map((msg) => {
        if (msg.role !== 'assistant') return msg
        const hasRunning = msg.blocks.some(
          (b) =>
            (b.type === 'tool' && b.status === 'running')
            || (b.type === 'subagent' && b.isActive)
            || (b.type === 'delegate' && b.status === RUN_STATUS.RUNNING)
            || (b.type === 'bgcommand' && b.isActive),
        )
        if (!hasRunning) return msg
        return {
          ...msg,
          blocks: msg.blocks.map((b) => {
            if (b.type === 'tool' && b.status === 'running') return { ...b, status: 'failed' as const }
            if (b.type === 'subagent' && b.isActive) return { ...b, isActive: false, failed: true }
            if (b.type === 'bgcommand' && b.isActive) return { ...b, isActive: false, failed: true }
            if (b.type === 'delegate' && b.status === RUN_STATUS.RUNNING) return { ...b, status: DELEGATE_RESULT.FAILED }
            return b
          }),
        }
      })
      .filter((msg) => !(msg.role === 'assistant' && msg.blocks.length === 0))
    )
    currentMsgRef.current = null
    thinkingBufRef.current = ''
    setThinkingActive(false)
    setCompressingActive(false)
    setWorkflows([])
    setTurnStartTime(null)
    setPermissionPending(false)
    meeting.resetMeeting()
    if (options.clearQueueOnAbort) options.queue.clearQueued()
  }, [meeting, options, flushPendingSteer, setMessages, currentMsgRef])

  const turnEnd = useTurnEnd(core, {
    meetingActive,
    paintHeldHistory: history.paintHeldHistory,
    flushPendingSteer,
    clearSentMarkers: queue.clearSentMarkers,
    finalizeAbortedTurn,
    // The rows this view lacks are enough; the server answers in full when
    // it holds no base for the chat.
    refetchHistory: (cid) => { try { ws.resumeChat(cid, { delta: true }) } catch { /* ignore */ } },
  })
  const blocks = useStreamBlocks(core, {
    flushPendingSteer,
    onMeetingSystem: meeting.onMeetingSystem,
    meetingParticipants,
  })

  // --- WebSocket callbacks ---

  const ws = useDashboardWs({
    newChatTarget: options.newChatTarget,
    // The chat this view is showing — useDashboardWs drops stream frames
    // tagged for any OTHER chat (background generation must not render here).
    // null until warmup_started mints the id for a brand-new chat.
    viewedChatId: chatId,
    isViewedChatPtyLive: options.isViewedChatPtyLive,
    onPreWarmupReady: (data) => {
      if (discardingRef.current) return
      options.onPreWarmupReady?.(data)
    },

    onTurnComplete: (data) => {
      // Origin-routed end-of-turn alert — the backend sends it to the device
      // that STARTED the turn. The visible viewed chat already pings via
      // onTurnDone, so drop it here; a hidden tab or a background chat pings.
      // NOT gated by discardingRef: it may target a non-viewed chat.
      if (data.chat_id === chatIdRef.current && document.visibilityState === 'visible') return
      options.onTurnComplete?.(data)
    },

    onWarmupStarted: (data) => {
      // The backend has minted the chat_id and persisted the row. Adopt the
      // chat_id locally AND navigate (via onWarmupStartedExtra) so the URL
      // carries it during the whole spawn — a refresh/back then re-resumes the
      // in-flight warmup instead of losing the chat.
      // Only a chat THIS warmup minted (`new_chat`) is ours to adopt: the
      // same frame fires for an existing chat re-warmed by a send, and a
      // reconnect on the new-chat page replays every warming chat's frame —
      // adopting one still spawning elsewhere would walk the user into it.
      if (data.chat_id && data.new_chat === true) {
        if (!chatIdRef.current) setChatId(data.chat_id)
        options.onWarmupStartedExtra?.(data)
      }
    },

    onWarmupReady: (data) => {
      // Even on a discarded (stale) warmup, the chat row IS persisted in DB by
      // the time this fires — refetch the sidebar so it picks up the new chat.
      if (data.chat_id) options.onWarmupRefetch?.()

      // Stale: this warmup is for a different chat than the one we're on.
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }

      // First event for this chat — clear the new-chat / switch transition flag.
      discardingRef.current = false

      setSessionId(data.session_id)
      if (data.chat_id) setChatId(data.chat_id)
      if (data.mode) setMode(data.mode)
      if (data.model) setModel(data.model)
      else if (options.fallbackModel !== undefined) setModel(options.fallbackModel)
      setSessionExecutionTarget(data.execution_target || TARGET_LOCAL)
      setSessionFallbackReason(data.fallback_reason ?? null)
      setOfflineMachineName(data.offline_machine_name || '')

      options.onWarmupReadyExtra?.(data)
    },

    onChatMoved: (data) => {
      // The move op acts on the connection's open chat — drop the ack if the
      // user already navigated elsewhere (that chat re-warms on next open).
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      options.onChatMoved?.(data)
    },

    onChatViewOnly: (data) => {
      // The frame names the viewed chat (per-chat routed); a stale one for a
      // chat the user left is dropped like a moved ack.
      if (data && data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      options.onChatViewOnly?.(data)
    },

    onEngineSwitched: (data) => {
      // Same staleness rule as onChatMoved — the frame also arrives via a
      // per-user broadcast, so a sibling tab viewing another chat drops it.
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      options.onEngineSwitched?.(data)
    },

    onSwitchEngineDenied: (data) => {
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      options.onSwitchEngineDenied?.(data)
    },

    onLiveness: (data) => {
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      options.onLiveness?.(data)
    },

    ...history.callbacks,
    ...blocks.callbacks,
    onLocationRequest: async (data) => {
      try {
        const loc = await getDeviceLocation()
        ws.sendLocationResponse(data.request_id, loc)
      } catch (err) {
        ws.sendLocationResponse(data.request_id, {
          error: errorText(err) || 'Could not determine location',
        })
      }
    },

    ...turnEnd.callbacks,
    onModeChanged: (m) => { if (discardingRef.current) return; setMode(m) },
    onModelChanged: (m) => { if (discardingRef.current) return; setModel(m) },
    // Not a per-chat frame on the wire (a new chat's pick carries no id), so
    // the chat is matched here: only the viewed chat's stored mode applies.
    onExecutionModeChanged: (f) => {
      if (discardingRef.current || !f.chat_id || f.chat_id !== chatIdRef.current) return
      options.onExecutionModeChanged?.(f.execution_mode)
    },

    ...queue.callbacks,
    onPlanStatus: (data) => {
      if (discardingRef.current) return
      setSessionPlans((prev) =>
        prev.map((p) =>
          p.filename === data.filename
            ? { ...p, status: data.status as 'pending' | 'implemented' | 'rejected' }
            : p
        )
      )
    },

    onTitleUpdated: (_msg) => {
      // Refresh the sidebar — the chat row's title col is the live source.
      options.onTitleUpdated?.()
    },

    // New interactive-history rows persisted (transcript tail batch) —
    // pass-through so pages can live-refresh an open rich-history view.
    onChatRows: (data) => {
      options.onChatRows?.(data)
    },

    // --- Notifications (page-owned) ---
    onNotification: (data) => {
      options.onNotification?.(data)
    },
    onNotificationSilent: (data) => {
      options.onNotificationSilent?.(data)
    },
    onNotificationCount: (data) => {
      options.onNotificationCount?.(data)
    },
  })

  // The artifact channels (chatStream/useArtifactChannels.ts): subscribed
  // through the socket's registry, so they come after it.
  const { sendArtifactInteraction, sendAppAction } = useArtifactChannels(core, ws)

  // --- Shared handlers ---

  const handlePermissionRespond = useCallback(
    (requestId: string, approved: boolean) => {
      setPermissionPending(false)
      setTurnStartTime(Date.now())  // Restart timer — LLM resumes
      ws.sendPermission(requestId, approved)
      resolvePermission(requestId, approved)
    },
    [ws, resolvePermission],
  )

  const handleQuestionAnswer = useCallback(
    (response: string) => {
      if (!chatId) return
      setMessages((prev) =>
        prev.map((msg) => ({
          ...msg,
          blocks: msg.blocks.map((b) =>
            b.type === 'question' && !b.answered ? { ...b, answered: true } : b,
          ),
        })),
      )
      // Restart timer — the answer resumes (or starts) generation. Without
      // this the send turns into Stop (sendMessage flips the chat slice to
      // streaming) but the timer never renders: a mid-turn answer is consumed
      // by the question hook, so no queue_sent/live_state ever re-arms it.
      setTurnStartTime(Date.now())
      if (ws.streaming) {
        // During streaming (e.g. permission pending), let backend queue it.
        // The queue_sent event will add the user bubble when processed.
        ws.sendMessage(response, chatId)
      } else {
        const userMsg: DisplayMessage = {
          id: `user-${Date.now()}`,
          role: 'user',
          blocks: [{ type: 'text', content: response }],
          createdAt: new Date().toISOString(),
        }
        setMessages((prev) => [...prev, userMsg])
        ws.sendMessage(response, chatId)
      }
    },
    [chatId, ws, setMessages],
  )

  // Codex request_user_input: answer the HELD turn over question_response (no
  // fresh chat turn — the turn resumes in place). Mark the block answered, clear
  // the composer gate, and restart the timer so the resumed generation renders.
  const handleQuestionAnswerStructured = useCallback(
    (requestId: string, answers: Record<string, { answers: string[] }>) => {
      if (!chatId) return
      setMessages((prev) =>
        prev.map((msg) => ({
          ...msg,
          blocks: msg.blocks.map((b) =>
            b.type === 'question' && b.requestId === requestId && !b.answered
              ? { ...b, answered: true }
              : b,
          ),
        })),
      )
      setPermissionPending(false)
      setTurnStartTime(Date.now())
      ws.sendQuestionResponse(requestId, answers)
    },
    [chatId, ws, setMessages],
  )

  const handleSendMessage = useCallback(
    (text: string) => {
      if (!chatId) return
      abortedRef.current = false  // a new send always re-opens the stream
      if (ws.streaming) {
        // During streaming, let backend queue it — onQueueSent will add the user bubble
        ws.sendMessage(text, chatId)
      } else {
        const userMsg: DisplayMessage = {
          id: `user-${Date.now()}`,
          role: 'user',
          blocks: [{ type: 'text', content: text }],
          createdAt: new Date().toISOString(),
        }
        const placeholder: DisplayMessage = {
          id: `stream-${Date.now()}`,
          role: 'assistant',
          blocks: [],
          createdAt: new Date().toISOString(),
        }
        currentMsgRef.current = placeholder
        setMessages((prev) => [...prev, userMsg, placeholder])
        setTurnStartTime(Date.now())
        ws.sendMessage(text, chatId)
      }
    },
    [chatId, ws, setMessages, currentMsgRef],
  )

  const handleImplementPlan = useCallback(
    (planPath: string, planMode: string) => {
      ws.implementPlan(planPath, planMode)
      setSessionPlans((prev) =>
        prev.map((p) =>
          p.filename === planPath ? { ...p, status: 'implemented' as const } : p,
        ),
      )
    },
    [ws],
  )

  const handlePlanFetched = useCallback((filename: string, content: string) => {
    setSessionPlans((prev) => {
      // Update existing plan if content changed (same file re-reviewed after edit)
      const existing = prev.find((p) => p.filename === filename)
      if (existing) {
        if (existing.content === content && existing.status === 'pending') return prev
        // Reset status to pending when content changes (plan re-reviewed after implementation)
        return prev.map((p) => p.filename === filename ? { ...p, content, status: 'pending' as const } : p)
      }
      // If there's already a pending plan, update it instead of adding a duplicate
      // (handles the case where plan-{requestId} changes between reviews)
      const pendingIdx = prev.findIndex((p) => p.status === 'pending')
      if (pendingIdx >= 0) {
        const updated = [...prev]
        updated[pendingIdx] = { filename, content, status: 'pending' as const }
        return updated
      }
      return [...prev, { filename, content, status: 'pending' as const }]
    })
  }, [])

  const resolvePlanReview = useCallback((requestId: string, action: string) => {
    // Find the filename from the plan_review block
    let planFilename = ''
    for (const msg of messages) {
      for (const b of msg.blocks) {
        if (b.type === 'plan_review' && b.requestId === requestId) {
          planFilename = b.filename || ''
        }
      }
    }
    setPermissionPending(false)
    // The held turn resumes; a card answered between turns (a Codex plan)
    // starts its turn's timer with the turn's own frames.
    if (ws.streaming) setTurnStartTime(Date.now())
    ws.sendPlanReviewResponse(requestId, action, planFilename)
    // Update plan status in local state (only for reject — implementation
    // status is tracked by the backend via plan_status event on done)
    if (planFilename && action === 'reject') {
      setSessionPlans((prev) =>
        prev.map((p) => p.filename === planFilename ? { ...p, status: 'rejected' as const } : p)
      )
    }
    setMessages((prev) =>
      prev.map((msg) => {
        const hasMatch = msg.blocks.some(
          (b) => b.type === 'plan_review' && b.requestId === requestId
        )
        if (!hasMatch) return msg
        const updated = {
          ...msg,
          blocks: msg.blocks.map((b) =>
            b.type === 'plan_review' && b.requestId === requestId
              ? { ...b, resolved: true, action }
              : b,
          ),
        }
        return updated
      }),
    )
    // Mode change is handled by the backend (sends mode_changed event)
  }, [ws, messages, setMessages])

  return {
    ws,
    // state
    messages, setMessages,
    pendingSteers,
    // lazy chat-history pagination (scroll-back)
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
    billedCost, setBilledCost,
    toolCost, setToolCost,
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
    meetingRound, setMeetingRound,
    meetingMaxRounds,
    meetingLeftParticipants,
    editText, setEditText,
    // refs
    currentMsgRef,
    thinkingBufRef,
    abortedRef,
    discardingRef,
    sentWithBubbleRef,
    sentWithBubbleIdRef,
    erroredRef,
    meetingSpeakerRef,
    // handlers
    handlePermissionRespond,
    handleQuestionAnswer,
    handleQuestionAnswerStructured,
    handleSendMessage,
    sendArtifactInteraction,
    sendAppAction,
    handleImplementPlan,
    handlePlanFetched,
    resolvePlanReview,
    finalizeAbortedTurn,
  }
}
