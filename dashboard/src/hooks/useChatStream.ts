import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useDashboardWs } from './useDashboardWs'
import type { ThreadGoal } from './useDashboardWs.types'
import { useChatStore } from '@/store/chatStore'
import { getDeviceLocation } from '../lib/geolocation'
import type { DisplayMessage, MessageBlock } from '../components/chat/types'
import { dbMessagesToDisplay, eventToBlock } from '../lib/messageBlocks'
import type { ActiveAgent } from '../components/chat/ChatStatusBar'
import type { WorkflowLive } from '../components/chat/plan/WorkflowPanel'
import type { SessionPlan } from '../components/chat/plan/PlanPanel'
import { useChatMessages } from './useChatMessages'
import { buildLiveStateMessages } from './chatStream/liveStateBuilder'
import type { UseChatStreamOptions } from './chatStream/types'

// Tools that should NOT generate tool_start/tool_end blocks (they have dedicated display events).
// mcp__delegation-mcp__delegate renders as the (expandable) delegate pill from the proxy's
// delegate_spawn — without this skip it ALSO got a bare generic tool pill (live only; the
// backend never persisted it, so history and live disagreed).
const SKIP_TOOL_EVENTS = new Set(['EnterPlanMode', 'ExitPlanMode', 'mcp__delegation-mcp__delegate'])
// Tools that have dedicated subagent blocks — skip tool block but keep tracking logic
const AGENT_TOOL_NAMES = new Set(['Agent', 'Task'])

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
  const [chatId, setChatId] = useState<string | null>(options.initialChatId ?? null)
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
    messages, setMessages, currentMsgRef,
    rawRowsRef, oldestLoadedIdRef, loadingOlderRef,
    hasMoreOlder, loadingOlder, setHasMoreOlder, setLoadingOlder,
    appendBlock, seedDbHistory, loadOlder, removeMediaProcessing,
    appendToLastTextBlock, updateToolBlock, updateToolBlockByName,
    resolvePermission, updateSubagentActive, updateCommandActive,
    ensureAssistantMsg, removePreviewBlocks,
  } = useChatMessages({ agents, meetingSpeakerRef, chatIdRef })
  const [mode, setMode] = useState(options.defaultMode ?? 'default')
  const [model, setModel] = useState('')

  // Dismiss (user X'd a preview block): drop the matching local blocks
  // ref-safely. `key` scopes to one instance (a frozen "previous version");
  // without it the file's whole preview trail goes (the live block's close).
  // The live/frozen/chip states themselves are a render-time derivation
  // (previewChainModes) — there is no collapse bookkeeping to update here.
  const dismissPreview = useCallback((
    fileId: string, key?: { snapshotId?: string; dbMessageId?: number },
  ) => {
    removePreviewBlocks(fileId, key)
  }, [removePreviewBlocks])

  // Execution target for the active session (set by warmup_ready). When the
  // session falls back to local, fallbackReason is set so the header can render
  // an amber badge; offlineMachineName names the user's offline override.
  const [sessionExecutionTarget, setSessionExecutionTarget] = useState<string>('local')
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
        } else if (b.type === 'delegate' && b.status === 'running') {
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
  const [contextUsed, setContextUsed] = useState(0)
  const [contextMax, setContextMax] = useState(0)
  const [cacheStats, setCacheStats] = useState({ cacheRead: 0, cacheWrite: 0, inputTokens: 0, outputTokens: 0 })
  const [permissionPending, setPermissionPending] = useState(false)
  const [aborting, setAborting] = useState(false)
  const [limitReached, setLimitReached] = useState(false)
  const [limitWarning, setLimitWarning] = useState<{ monthly?: any; weekly?: any } | null>(null)

  // Plans panel state
  const [sessionPlans, setSessionPlans] = useState<SessionPlan[]>([])
  // Track mode before plan mode so we can restore
  const prePlanModeRef = useRef<string>('default')

  // Todo panel state (from TodoWrite tool events)
  const [currentTodos, setCurrentTodos] = useState<Array<{content: string; status: string; activeForm?: string}>>([])
  // Codex thread goal — null = no goal (the GoalPanel renders nothing).
  const [currentGoal, setCurrentGoal] = useState<ThreadGoal | null>(null)
  // Live dynamic-workflow (Workflow tool) trees, keyed by tool_use_id.
  const [workflows, setWorkflows] = useState<WorkflowLive[]>([])

  // Meeting state
  const [meetingActive, setMeetingActive] = useState(false)
  const [meetingParticipants, setMeetingParticipants] = useState<Array<{slug: string; display_name: string; color: string}>>([])
  const [meetingSpeaker, setMeetingSpeaker] = useState<string | null>(null)
  const [meetingRound, setMeetingRound] = useState(0)
  const [meetingMaxRounds, setMeetingMaxRounds] = useState(10)
  const [meetingLeftParticipants, setMeetingLeftParticipants] = useState<Set<string>>(new Set())

  // Queue editing state
  const [editText, setEditText] = useState<string | null>(null)

  // Guard against stale chunks after abort creating phantom bubbles. Armed by
  // the page's handleAbort, disarmed only at a terminal frame (aborted/done),
  // on the next send, or on a chat (re)load — a graceful abort keeps the
  // engine draining for a beat, and those straggler chunks must not reopen a
  // fresh assistant header after finalizeAbortedTurn sealed the message.
  const abortedRef = useRef(false)
  // Steers accepted mid-turn, held until the next block boundary. Codex
  // consumes an accepted steer at the next sampling-round boundary — while
  // the current text block is still streaming, the model has NOT read it
  // yet. Splitting immediately cut sentences in half; instead the user
  // bubble + fresh assistant header render at the boundary where the steer
  // actually lands: the next tool/thinking/subagent block, or turn end.
  const pendingSteerRef = useRef<string[]>([])
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
  // The TEXT of the last send that already added a user bubble. If the
  // backend unexpectedly queues THAT SAME message (stale pump from WS
  // reconnect), onQueued/onQueueSent skip the duplicate bubble/queue entry —
  // matched by text, because a boolean here suppressed every queued chip for
  // the rest of the turn: a mid-turn send (claude's queue fallback) arrived
  // while the flag was still armed from the turn-opening send and its chip
  // silently vanished until the post-turn drain.
  const sentWithBubbleRef = useRef<string | null>(null)

  /** Render any held steers: user bubble(s) in accept order, then (while the
   * turn keeps streaming) a fresh assistant continuation that subsequent
   * blocks land in. `withContinuation=false` at turn end/abort — nothing
   * streams after, so only the bubbles are owed. No-op when nothing is held. */
  const flushPendingSteer = (withContinuation = true) => {
    if (!pendingSteerRef.current.length) return
    const held = pendingSteerRef.current
    pendingSteerRef.current = []
    const now = Date.now()
    const userMsgs: DisplayMessage[] = held.map((text, i) => ({
      id: `user-${now}-${i}`,
      role: 'user',
      blocks: [{ type: 'text', content: text }],
      createdAt: new Date().toISOString(),
    }))
    if (!withContinuation) {
      setMessages((prev) => [...prev, ...userMsgs])
      return
    }
    const continuation: DisplayMessage = {
      id: `stream-${now}`,
      role: 'assistant',
      blocks: [],
      createdAt: new Date().toISOString(),
    }
    currentMsgRef.current = continuation
    setMessages((prev) => [...prev, ...userMsgs, continuation])
  }

  // --- WebSocket callbacks ---

  const ws = useDashboardWs({
    // The chat this view is showing — useDashboardWs drops stream frames
    // tagged for any OTHER chat (background generation must not render here).
    // null until warmup_started mints the id for a brand-new chat.
    viewedChatId: chatId,
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
      if (data.chat_id) {
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
      setSessionExecutionTarget(data.execution_target || 'local')
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

    onChatHistory: (data) => {
      // Discard stale chat_history events from a previously-resumed chat/run.
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return  // stale — belongs to a previous chat
      }
      discardingRef.current = false  // Chat loaded — accept events for this chat
      // Page-owned restore (chat: chatActiveLayer + model from execution_path).
      options.onChatHistoryMeta?.(data)
      // Reset all streaming refs — critical for app-background-return where React
      // state survives but refs point to stale objects from the previous stream.
      currentMsgRef.current = null
      thinkingBufRef.current = ''
      abortedRef.current = false
      pendingSteerRef.current = []  // held steers belong to the previous view
      liveStateSeededRef.current = false  // view is DB-authoritative again
      setTurnStartTime(null)
      setThinkingActive(false)
      setCompressingActive(false)
      setPermissionPending(false)
      setAborting(false)
      setWorkflows([])
      // Reconcile the per-chat streaming status to the DB-authoritative resume.
      // The send/stop button reads byChat[chatId].status (per-chat), which is
      // only cleared by the `done`/`aborted` WS frames — and those are LOST when
      // the socket is suspended (Android background) while a turn finishes. So a
      // turn that ended (often on an AskUserQuestion) while backgrounded left the
      // status stuck at 'streaming' → the button stayed red even though the timer
      // (turnStartTime, reset above) was gone. resume_chat always sends
      // chat_history BEFORE any live_state, so reset to 'ready' here; a genuinely
      // live turn's trailing live_state re-arms it to 'streaming' (same pattern as
      // turnStartTime). Only the VIEWED chat (the staleness guard above) — a
      // non-viewed streaming chat keeps its sidebar dot (its pump isn't attached
      // on this socket, so no live_state would re-arm it).
      {
        const _cid = data.chat_id || chatIdRef.current
        if (_cid) useChatStore.getState().setReady(_cid)
      }
      const dbMessages = Array.isArray(data.messages) ? data.messages : []
      const dbPlans = Array.isArray(data.plans) ? data.plans : []
      // Reset pagination on EVERY chat_history fire (resume / warmup attach /
      // mode-switch / reattach / onDone refetch) — the frame is the newest page,
      // so a re-fire collapses the window back to it (any scrolled-in older pages
      // are dropped). Rows arrive ascending, so [0] is the oldest loaded id.
      rawRowsRef.current = dbMessages
      oldestLoadedIdRef.current = dbMessages.length ? dbMessages[0].id : null
      loadingOlderRef.current = false
      setLoadingOlder(false)
      setHasMoreOlder(!!data.has_more)
      const displayMsgs = dbMessagesToDisplay(dbMessages, agents)
      // History is authoritative for everything PERSISTED — but a send can
      // race a slow history load (long chat): the optimistic user bubble that
      // handleSend appended would be erased by a wholesale replace, because
      // this history JSON was read server-side before the prompt persisted
      // ("my message vanished until the next reload"). Carry forward trailing
      // OPTIMISTIC user bubbles (send-minted `user-<ts>` ids; history rows are
      // `db-*`) that the rebuilt list doesn't already contain — text-deduped
      // against the rebuilt tail in case the prompt DID make it into the read.
      // Empty `stream-<ts>` assistant placeholders are NOT carried:
      // currentMsgRef was just reset above, so streaming re-mints its bubble.
      setMessages((prev) => {
        const carried: DisplayMessage[] = []
        for (let i = prev.length - 1; i >= 0; i--) {
          const m = prev[i]
          if (!/^(user|stream)-\d+$/.test(m.id)) break
          if (m.role === 'user') carried.unshift(m)
        }
        if (carried.length === 0) return displayMsgs
        const textOf = (m: DisplayMessage) => {
          const t = m.blocks.find((b) => b.type === 'text')
          return t && t.type === 'text' ? t.content : ''
        }
        const dbUserTail = new Set(
          displayMsgs.filter((m) => m.role === 'user').slice(-6).map(textOf),
        )
        const fresh = carried.filter((m) => {
          const t = textOf(m)
          return !(t && dbUserTail.has(t))
        })
        return fresh.length > 0 ? [...displayMsgs, ...fresh] : displayMsgs
      })
      // Restore total cost from DB
      if (data.total_cost) setTotalCost(data.total_cost)
      if (data.context_used) setContextUsed(data.context_used)
      if (data.context_max) setContextMax(data.context_max)
      if (data.cache_write) setCacheStats({
        cacheRead: data.cache_read ?? 0,
        cacheWrite: data.cache_write ?? 0,
        inputTokens: 0,
        outputTokens: data.output_tokens ?? 0,
      })
      // Restore pinned plans from DB
      if (dbPlans.length > 0) {
        setSessionPlans(dbPlans.map((p: any) => ({
          filename: p.filename,
          content: p.content,
          status: p.status as 'pending' | 'implemented' | 'rejected',
        })))
      }
      // Panel restore comes from the server-computed `restore` object (full-history,
      // window-independent — the lazy-load window may not contain the last snapshot),
      // NOT from scanning the loaded messages. A frame without `restore` (older/
      // partial paths) leaves the panels as-is rather than wiping them. For an active
      // session the trailing live_state overrides this (sent after chat_history).
      if (data.restore) {
        setCurrentTodos(Array.isArray(data.restore.todos) ? data.restore.todos : [])
        setCurrentGoal(data.restore.goal ?? null)
        const mt = data.restore.meeting
        if (mt && mt.active) {
          setMeetingActive(true)
          // Normalize: older proxies sent the DB's slug STRINGS here while the
          // live meeting_started event sends {slug, display_name, color}
          // objects — the string form crashed MeetingIndicator (undefined
          // .charAt) and blanked the app pre-ErrorBoundary.
          const parts = Array.isArray(mt.participants) ? mt.participants : []
          setMeetingParticipants(parts.map((p: any) =>
            typeof p === 'string'
              ? { slug: p, display_name: p, color: '' }
              : { slug: p?.slug || '', display_name: p?.display_name || p?.slug || '', color: p?.color || '' }
          ))
          setMeetingMaxRounds(mt.max_turns || 30)
        } else {
          setMeetingActive(false)
        }
      }

      options.onChatHistoryLoaded?.(data)
    },

    onText: (content) => {
      if (discardingRef.current || abortedRef.current) return
      if (!currentMsgRef.current) {
        const speaker = meetingSpeakerRef.current
        const msg: DisplayMessage = {
          id: `stream-${Date.now()}`,
          role: 'assistant',
          blocks: [{ type: 'text', content }],
          createdAt: new Date().toISOString(),
          ...(speaker ? {
            agentSlug: speaker.slug,
            agentDisplayName: speaker.displayName,
            agentColor: speaker.color,
            badge: 'meeting',
          } : {}),
        }
        currentMsgRef.current = msg
        setMessages((prev) => [...prev, msg])
      } else {
        appendToLastTextBlock(content)
      }
    },

    onThinking: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      if (data.phase === 'start') {
        thinkingBufRef.current = ''
        setThinkingActive(true)
        appendBlock({ type: 'thinking', content: '', collapsed: true })
      } else if (data.phase === 'progress') {
        // Live ~token gauge for content-less thinking (when adaptive effort
        // hides the thinking text): update the open thinking block's count;
        // create the block
        // if a CLI shape ever sends progress without a preceding start.
        setThinkingActive(true)
        const tokens = data.estimated_tokens || 0
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (!last || last.role !== 'assistant') return prev
          const blocks = [...last.blocks]
          let found = false
          for (let i = blocks.length - 1; i >= 0; i--) {
            const b = blocks[i] as any
            if (b.type === 'thinking' && !b.done) {
              blocks[i] = { ...b, tokens } as MessageBlock
              found = true
              break
            }
          }
          if (!found) blocks.push({ type: 'thinking', content: '', collapsed: true, tokens } as MessageBlock)
          const updated = { ...last, blocks }
          if (last === currentMsgRef.current) currentMsgRef.current = updated
          return [...prev.slice(0, -1), updated]
        })
      } else if (data.text) {
        thinkingBufRef.current += data.text
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant') {
            const blocks = [...last.blocks]
            for (let i = blocks.length - 1; i >= 0; i--) {
              if (blocks[i].type === 'thinking') {
                blocks[i] = { ...blocks[i], content: thinkingBufRef.current } as MessageBlock
                break
              }
            }
            const updated = { ...last, blocks }
            if (last === currentMsgRef.current) currentMsgRef.current = updated
            return [...prev.slice(0, -1), updated]
          }
          return prev
        })
      } else if (data.phase === 'end') {
        setThinkingActive(false)
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last && last.role === 'assistant') {
            const blocks = last.blocks.map((b) =>
              b.type === 'thinking' ? { ...b, collapsed: true, done: true } : b,
            )
            const updated = { ...last, blocks }
            if (last === currentMsgRef.current) currentMsgRef.current = updated
            return [...prev.slice(0, -1), updated]
          }
          return prev
        })
      }
    },

    onToolStart: (data) => {
      if (discardingRef.current || abortedRef.current) return
      // Filter out plan mode tools (they have dedicated plan_mode events)
      if (SKIP_TOOL_EVENTS.has(data.name)) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at

      const toolId = data.tool_id || data.name

      // Agent/Task tools get a dedicated subagent block + status-bar entry —
      // both created by onTaskSpawn (the authoritative spawn event, which
      // upserts the badge keyed by tool_use_id). We do NOT pre-add the badge
      // here: a premature entry keyed by tool_id created an orphan / double-
      // count whenever task_spawn's id or ordering differed (e.g. missing/
      // empty tool_use_id, Codex mixed paths). Skip the tool block entirely;
      // the badge + block now appear together at task_spawn and clear together
      // by the same key. (No badge before task_spawn = a ~sub-second delay
      // while the tool input streams — a worthwhile trade for no desync.)
      if (AGENT_TOOL_NAMES.has(data.name)) {
        return
      }

      ensureAssistantMsg()
      appendBlock({
        type: 'tool',
        name: data.name,
        toolId,
        summary: '',
        status: 'running',
      })
    },

    onToolInfo: (data) => {
      if (discardingRef.current) return
      // tool_info has name but not tool_id, so match by name
      updateToolBlockByName(data.name, { summary: data.summary || '', toolInput: data.tool_input })
    },

    onToolResult: (data) => {
      if (discardingRef.current) return
      // A FOREGROUND subagent's final report — Agent tools have no generic
      // tool block; attach the result to the subagent block (by tool_use_id)
      // so the pill expands to the report. Background spawns keep their
      // "launched" ack out of the pill (the real report arrives next turn).
      if (AGENT_TOOL_NAMES.has(data.tool_name)) {
        const toolId = data.tool_use_id || ''
        if (!toolId || !data.result_content) return
        setMessages((prev) =>
          prev.map((msg) => ({
            ...msg,
            blocks: msg.blocks.map((b): MessageBlock =>
              b.type === 'subagent' && b._toolId === toolId && !b._background
                ? { ...b, toolResult: data.result_content }
                : b,
            ),
          })),
        )
        return
      }
      // Attach result content to the most recent matching tool block
      updateToolBlockByName(data.tool_name, { toolResult: data.result_content, resultSummary: data.summary })
    },

    onToolEnd: (data) => {
      if (discardingRef.current) return
      // Filter out plan mode tools
      if (SKIP_TOOL_EVENTS.has(data.name)) return

      const toolId = data.tool_id || data.name

      // Agent/Task tool_end means "content block streamed", NOT "agent finished".
      // Agents haven't even started executing yet. Ignore entirely.
      if (AGENT_TOOL_NAMES.has(data.name)) return

      updateToolBlock(toolId, { status: 'done' })
    },

    onTaskSpawn: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      // task_spawn is the AUTHORITATIVE spawn event — it creates the subagent
      // block, and the status-bar badge DERIVES from that block (keyed by
      // _toolId). The CLI binds task_spawn → tool_use_id (== the Agent tool_use
      // id from onToolStart) so bg_agent_done can clear the right block; Codex's
      // spawn_agent has no tool_use_id (_toolId null) and clears via
      // fg/bg_agents_complete.
      appendBlock({
        type: 'subagent',
        description: data.description,
        subagentType: data.subagent_type || '',
        isActive: true,  // Always start spinning — completion comes later
        _toolId: data.tool_use_id || null,
        _background: data.run_in_background || false,
        toolInput: data.tool_input,  // full Agent input — the pill expands to it
      })
    },

    onDelegateSpawn: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      // Proxy-emitted on actual task-create; the badge derives from this block
      // (keyed by task_id). onDelegateResult completes it by the same task_id.
      appendBlock({
        type: 'delegate',
        taskName: data.task_name,
        agent: data.agent,
        promptPreview: data.prompt_preview,
        status: 'running',
        _taskId: data.task_id,
        prompt: data.prompt || '',  // full prompt — the pill expands to it
        workerChatId: data.chat_id || undefined,
      })
    },

    onDelegateResult: (data) => {
      if (discardingRef.current) return
      // Mark the matching delegate block completed — by task_id when present
      // (stable key), else by task_name. The badge derives from the block.
      setMessages((prev) =>
        prev.map((msg) => ({
          ...msg,
          blocks: msg.blocks.map((b): MessageBlock =>
            b.type === 'delegate'
              && ((data.task_id && b._taskId === data.task_id)
                  || (!data.task_id && b.taskName === data.task_name))
              ? { ...b, status: ((data.status || 'completed') as 'completed' | 'failed' | 'cancelled' | 'user_interrupted') }
              : b,
          ),
        })),
      )

      // Insert delegate result as inline agent message
      if (data.output_text) {
        const delegateAgent = agents?.find(a => a.name === data.agent)
        setMessages((prev) => [
          ...prev,
          {
            id: `delegate-result-${data.task_id || data.task_name}-${Date.now()}`,
            role: 'assistant' as const,
            blocks: [{ type: 'text' as const, content: data.output_text || '' }],
            createdAt: new Date().toISOString(),
            agentSlug: data.agent || '',
            agentDisplayName: delegateAgent?.display_name,
            agentColor: delegateAgent?.color || '',
            badge: data.status === 'cancelled' ? 'delegate canceled'
              : data.status === 'failed' ? 'delegate failed'
              : data.status === 'user_interrupted' ? 'delegate interrupted'
              : 'delegate response',
          },
        ])
      }
    },

    onBgAgentDone: (data) => {
      if (discardingRef.current) return
      // One subagent finished (CLI: SubagentStop hook / task_notification).
      // Deterministic + order-independent: clear the block for THIS tool_use_id
      // (the badge derives from it), so parallel agents that finish out of order
      // each clear their own widget (no FIFO guessing).
      const toolId = data?.tool_use_id || ''
      if (!toolId) return
      updateSubagentActive(toolId, false)
    },

    onBgCommandSpawn: (data) => {
      if (discardingRef.current || abortedRef.current) return
      flushPendingSteer()  // a new block = the boundary a held steer lands at
      ensureAssistantMsg()
      // A backgrounded Bash — create a live bg-command block (the badge derives
      // from it, keyed by _toolId). bg_command_done clears it by the same id.
      // Rendered on top of the command's normal tool card.
      appendBlock({
        type: 'bgcommand',
        command: data.command || '',
        description: data.description || '',
        isActive: true,
        _toolId: data.tool_use_id || null,
      })
    },

    onBgCommandDone: (data) => {
      if (discardingRef.current) return
      // One background command finished (CLI task_updated — no completion hook
      // exists for bg bash). Order-independent clear by tool_use_id so
      // concurrent commands each clear their own widget.
      const toolId = data?.tool_use_id || ''
      if (!toolId) return
      updateCommandActive(toolId, false)
    },

    onBgAgentsComplete: (_msg) => {
      if (discardingRef.current) return
      // All background agents completed (sent by bg_nudge before auto-response).
      // Mark ALL remaining bg subagent blocks as done.
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasBg = msg.blocks.some(
            (b) => b.type === 'subagent' && b.isActive && (b as any)._background,
          )
          if (!hasBg) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'subagent' && b.isActive && (b as any)._background
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
          return updatedMsg
        }),
      )
    },

    onBgCommandsComplete: (_msg) => {
      if (discardingRef.current) return
      // All background commands finished (sent by bg_command_nudge before the
      // review turn). The per-command bg_command_done can't deliver post-turn
      // (the turn's pump is gone), so this is the reliable clear — mark ALL
      // active bgcommand blocks done. Mirror of onBgAgentsComplete.
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasCmd = msg.blocks.some(
            (b) => b.type === 'bgcommand' && b.isActive,
          )
          if (!hasCmd) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'bgcommand' && b.isActive
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
          return updatedMsg
        }),
      )
    },

    onServerTurnStart: () => {
      if (discardingRef.current) return
      // A server-initiated turn (bg-command / bg-subagent review nudge, or a
      // delegate-result synthesis) is now streaming — no user-send set the live
      // timer, so start it here (mirrors the send path). done/aborted clear it.
      setTurnStartTime(Date.now())
    },

    onWorkflowStart: (data) => {
      if (discardingRef.current) return
      setWorkflows((prev) => {
        if (prev.some((w) => w.toolUseId === data.tool_use_id)) return prev
        return [...prev, { toolUseId: data.tool_use_id, workflowName: data.workflow_name || '', progress: [], active: true }]
      })
    },

    onWorkflowProgress: (data) => {
      if (discardingRef.current) return
      setWorkflows((prev) => prev.map((w) =>
        w.toolUseId === data.tool_use_id ? { ...w, progress: Array.isArray(data.workflow_progress) ? data.workflow_progress : [] } : w,
      ))
    },

    onWorkflowEnd: (data) => {
      if (discardingRef.current) return
      setWorkflows((prev) => prev.map((w) =>
        w.toolUseId === data.tool_use_id ? { ...w, active: false } : w,
      ))
    },

    onFgAgentsComplete: () => {
      if (discardingRef.current) return
      // Codex path: the collab `wait` tool emits fg_agents_complete (no
      // per-agent id). Claude CLI clears each subagent individually via
      // bg_agent_done(tool_use_id). Mark ALL foreground subagent blocks done.
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasFg = msg.blocks.some(
            (b) => b.type === 'subagent' && b.isActive && !(b as any)._background,
          )
          if (!hasFg) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'subagent' && b.isActive && !(b as any)._background
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
          return updatedMsg
        }),
      )
    },

    onLocationRequest: async (data) => {
      try {
        const loc = await getDeviceLocation()
        ws.sendLocationResponse(data.request_id, loc)
      } catch (err: any) {
        ws.sendLocationResponse(data.request_id, {
          error: err.message || 'Could not determine location',
        })
      }
    },

    onPermissionPrompt: (data) => {
      if (discardingRef.current) return
      setPermissionPending(true)
      setTurnStartTime(null)  // Pause timer — LLM is blocked waiting
      ensureAssistantMsg()
      // Resolve meeting agent identity for the permission dialog
      const meetingAgentSlug = data.meeting_agent as string | undefined
      const meetingAgent = meetingAgentSlug
        ? (() => {
            const p = meetingParticipants.find(pp => pp.slug === meetingAgentSlug)
            return p ? { slug: p.slug, displayName: p.display_name, color: p.color } : { slug: meetingAgentSlug, displayName: meetingAgentSlug, color: '' }
          })()
        : undefined
      appendBlock({
        type: 'permission',
        requestId: data.request_id,
        toolName: data.tool_name,
        toolInput: data.tool_input,
        description: data.description,
        meetingAgent,
      })
    },

    onPlanMode: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      if (data.action === 'enter') {
        appendBlock({ type: 'plan', action: 'enter', toolInput: data.tool_input })
        prePlanModeRef.current = mode
        setMode('plan')
      } else if (data.action === 'exit' && (data as any).synthetic) {
        // Codex has no ExitPlanMode / held plan_review — the plan is the turn's
        // final message, so the backend synthesizes this exit at turn-end. Render
        // the implement card (PlanView). Supersede any earlier plan card so a
        // multi-turn refinement replaces rather than stacks actionable cards, then
        // append the new card — both in ONE setMessages so currentMsgRef stays in
        // sync (a separate supersede-map would replace the streaming message object
        // out from under appendBlock's identity check, dropping the card live).
        setMessages((prev) => {
          const superseded = prev.map((msg) => ({
            ...msg,
            blocks: msg.blocks.map((b) =>
              b.type === 'plan' && b.action === 'exit' && !b.superseded
                ? { ...b, superseded: true }
                : b,
            ),
          }))
          const last = superseded[superseded.length - 1]
          if (last && last.role === 'assistant') {
            const updated = {
              ...last,
              blocks: [...last.blocks, {
                type: 'plan' as const, action: 'exit' as const,
                toolInput: data.tool_input,
              }],
            }
            currentMsgRef.current = updated
            return [...superseded.slice(0, -1), updated]
          }
          return superseded
        })
      }
      // For Claude, plan_mode exit is a no-op — the actionable card is the held
      // plan_review; the mode change comes from plan_review_response / auto-approve.
    },

    onPlanReview: (data) => {
      if (discardingRef.current) return
      setPermissionPending(true)
      setTurnStartTime(null)  // Pause timer — LLM is blocked waiting
      ensureAssistantMsg()
      appendBlock({
        type: 'plan_review',
        requestId: data.request_id,
        plan: data.plan,
        toolInput: data.tool_input,
        filename: (data as any).filename || '',
      })
    },

    onSystem: (data) => {
      if (discardingRef.current) return
      const subtype = data.subtype

      // Meeting events: update state, selectively create blocks
      if (subtype === 'meeting_started') {
        setMeetingActive(true)
        setMeetingParticipants(Array.isArray(data.participants) ? data.participants : [])
        setMeetingMaxRounds(data.max_turns || data.max_rounds || 30)
        setMeetingRound(0)
        setMeetingLeftParticipants(new Set())
        // Append banner to the last assistant message — but only if it
        // doesn't already have one (onLiveState may have added it).
        setMessages((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            if (prev[i].role === 'assistant') {
              const alreadyHas = prev[i].blocks.some(
                (b) => b.type === 'system' && (b as any).subtype === 'meeting_started'
              )
              if (alreadyHas) return prev
              const updated = { ...prev[i], blocks: [...prev[i].blocks, { type: 'system' as const, subtype: 'meeting_started' }] }
              return [...prev.slice(0, i), updated, ...prev.slice(i + 1)]
            }
          }
          return prev
        })
        return
      }
      if (subtype === 'meeting_turn_start') {
        const agentSlug = data.agent || ''
        setMeetingSpeaker(agentSlug || null)
        setMeetingRound(data.round || 0)
        meetingSpeakerRef.current = {
          slug: agentSlug,
          displayName: data.agent_display_name || agentSlug,
          color: data.agent_color || '',
        }
        // Force new assistant message — unless the current message is
        // already for this agent (live_state reconstruction created it,
        // and this is a duplicate event from the queue).
        if (currentMsgRef.current?.agentSlug === agentSlug) {
          // Keep the existing message (it has blocks from live_state like thinking)
        } else {
          currentMsgRef.current = null
        }
        return  // no block — agent header on the message is enough
      }
      if (subtype === 'meeting_turn_end') {
        setMeetingSpeaker(null)
        meetingSpeakerRef.current = null
        return  // no block
      }
      if (subtype === 'meeting_agent_left') {
        if (data.agent) setMeetingLeftParticipants(prev => new Set([...prev, data.agent!]))
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype, agentName: data.agent_display_name || data.agent, agentColor: data.agent_color })
        return
      }
      if (subtype === 'meeting_concluded') {
        setMeetingActive(false)
        setMeetingSpeaker(null)
        setMeetingParticipants([])
        setMeetingLeftParticipants(new Set())
        meetingSpeakerRef.current = null
        // Show conclusion banner
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype: 'meeting_concluded' })
        return
      }
      if (subtype === 'meeting_agent_failed') {
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype, agentName: data.agent_display_name || data.agent, agentColor: data.agent_color })
        return
      }
      if (subtype === 'meeting_failed') {
        // The meeting never started (admission denial, spawn failure…) —
        // clear the pill state and show the reason where the "meeting is
        // set up" ack was left hanging.
        setMeetingActive(false)
        setMeetingSpeaker(null)
        setMeetingParticipants([])
        setMeetingLeftParticipants(new Set())
        meetingSpeakerRef.current = null
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype: 'meeting_failed', message: data.message })
        return
      }

      // Non-meeting system events: default behavior. `message` carries the
      // body for informational cards (e.g. session_reseeded).
      ensureAssistantMsg()
      appendBlock({ type: 'system', subtype, message: data.message })
    },

    onImages: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      // Remove image_generating placeholder (if any) before appending the gallery
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (!last || last.role !== 'assistant') return prev
        const genIdx = last.blocks.findIndex(b => b.type === 'image_generating')
        if (genIdx === -1) return prev
        const blocks = [...last.blocks]
        blocks.splice(genIdx, 1)
        const updated = { ...last, blocks }
        currentMsgRef.current = updated
        return [...prev.slice(0, -1), updated]
      })
      // Normalize wire field names (snake_case from backend → camelCase for the renderer).
      const galleryImages = (data.images || []).map(it => ({
        url: it.url || undefined,
        imageData: it.image_data || undefined,
        mimeType: it.mime_type || undefined,
        caption: it.caption || undefined,
        attribution: it.attribution || undefined,
        linkUrl: it.link_url || undefined,
        downloadUrl: it.download_url || undefined,
      }))
      if (galleryImages.length > 0) {
        appendBlock({ type: 'images', images: galleryImages })
      }
    },
    onImageGenerating: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({ type: 'image_generating', promptPreview: data.prompt_preview, model: data.model })
    },
    onMcpCost: (data) => {
      if (discardingRef.current) return
      const cost = data.cost_usd ?? 0
      if (cost > 0) setTotalCost(prev => prev + cost)
    },
    onImageGenFailed: () => {
      if (discardingRef.current) return
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (!last || last.role !== 'assistant') return prev
        const blocks = last.blocks.filter(b => b.type !== 'image_generating')
        if (blocks.length === last.blocks.length) return prev
        const updated = { ...last, blocks }
        currentMsgRef.current = updated
        return [...prev.slice(0, -1), updated]
      })
    },
    onLimitWarning: (data) => {
      if (discardingRef.current) return
      setLimitWarning(data)
      setTimeout(() => setLimitWarning(null), 10000)
    },
    onLimitReached: (_msg) => {
      if (discardingRef.current) return
      setLimitReached(true)
    },
    onUrl: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({ type: 'url', url: data.url, title: data.title, description: data.description })
    },
    onFile: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({ type: 'file', filename: data.filename, downloadUrl: data.download_url, description: data.description })
    },
    onVideo: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      removeMediaProcessing()
      appendBlock({
        type: 'video',
        srcKind: data.src_kind === 'token' ? 'token' : 'url',
        url: data.url || undefined,
        mediaUrl: data.media_url || undefined,
        token: data.token || undefined,
        mime: data.mime || undefined,
        caption: data.caption || undefined,
        title: data.title || undefined,
        poster: data.poster || undefined,
      })
    },
    onAudio: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      removeMediaProcessing()
      appendBlock({
        type: 'audio',
        srcKind: data.src_kind === 'token' ? 'token' : 'url',
        url: data.url || undefined,
        mediaUrl: data.media_url || undefined,
        token: data.token || undefined,
        mime: data.mime || undefined,
        caption: data.caption || undefined,
        title: data.title || undefined,
      })
    },
    onMediaProcessing: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      appendBlock({
        type: 'media_processing',
        mediaKind: data.media_kind === 'audio' ? 'audio' : 'video',
        caption: data.caption || undefined,
      })
    },
    onMediaFailed: (_msg) => {
      if (discardingRef.current) return
      removeMediaProcessing()
    },
    onDocumentPreview: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      // Append the new preview to the current assistant message (same pattern
      // as onImage/onFile). The old block for this file transitions to a
      // view-only "previous version" (and older ones to chips) at RENDER time
      // via previewChainModes — each block defers its own transition while
      // the user is engaged with it, so nothing here needs to collapse state.
      appendBlock({
        type: 'document_preview',
        wopiUrl: data.wopi_url,
        filename: data.filename,
        fileId: data.file_id,
        downloadUrl: data.download_url,
        snapshotId: data.snapshot_id || undefined,
        generation: data.generation || undefined,
      })
    },

    onQuestion: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      // A codex request_user_input carries a request_id → the held turn resumes
      // only on question_response. Claude's AskUserQuestion has none (answer =
      // fresh chat turn). A codex question keeps the composer gated + timer paused.
      if (data.request_id) setPermissionPending(true)
      appendBlock({
        type: 'question', toolName: data.tool_name, toolInput: data.tool_input,
        requestId: data.request_id,
      })
    },

    onMetadata: (data) => {
      if (discardingRef.current) return
      ensureAssistantMsg()
      const cost = data.cost_usd ?? 0
      appendBlock({ type: 'metadata', costUsd: cost, durationMs: data.duration_ms ?? data.duration_api_ms ?? 0 })
      if (cost > 0) setTotalCost((prev) => prev + cost)
      if (data.context_used && data.context_max) {
        setContextUsed(data.context_used)
        setContextMax(data.context_max)
      }
      setCacheStats({
        cacheRead: data.cache_read ?? 0,
        cacheWrite: data.cache_write ?? 0,
        inputTokens: data.input_tokens ?? 0,
        outputTokens: data.output_tokens ?? 0,
      })
    },

    onContextCompact: (data) => {
      if (discardingRef.current) return
      if (data.phase === 'usage') {
        // Post-compaction gauge state (Codex reports the compacted prompt
        // size on its next tokenUsage) — counter only, no chip: the
        // 'completed' event already rendered the visible separator.
        if (data.post_tokens != null) setContextUsed(data.post_tokens)
        if (data.context_max) setContextMax(data.context_max)
      } else if (data.phase === 'started') {
        setCompressingActive(true)
      } else if (data.phase === 'completed') {
        setCompressingActive(false)
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype: 'context_compressed' })
        // A between-turns manual compaction has no follow-up metadata frame —
        // the completed event carries the post-compaction size itself.
        if (data.post_tokens != null) setContextUsed(data.post_tokens)
      } else if (data.phase === 'failed') {
        // Manual compaction failed/unsupported — clear the badge, no separator
        // (the server sends the error toast separately).
        setCompressingActive(false)
      }
    },

    onDone: () => {
      // Clear the optimistic-bubble dedup marker on EVERY turn end, even a
      // discarded/non-viewed one — if it leaks (turn ended while
      // discarding, or via abort/error/warmup-failed below), onQueued/onQueueSent
      // suppress a same-text queued message.
      sentWithBubbleRef.current = null
      if (discardingRef.current) return
      abortedRef.current = false  // terminal frame — straggler window over
      // A steer the turn consumed at its very last round boundary (no block
      // followed) still owes its user bubble — render it before the cleanup.
      flushPendingSteer(false)
      // Turn-done tail (chat: ping when not in a meeting and no bg subagent is
      // running, then refetchChats). The ping gate inputs are computed here so
      // the page doesn't need stale-closure-free reads of meeting/bg state.
      // A bg subagent block still active (in any assistant message) gates the
      // turn-complete ping. Derived from the blocks (the badge's source).
      const bgStillRunning = messagesRef.current.some((m) =>
        m.role === 'assistant' && m.blocks.some(
          (b) => b.type === 'subagent' && b.isActive && b._background,
        ),
      )
      options.onTurnDone?.({ meetingActive, bgStillRunning })
      // Defer clearing currentMsgRef so that pending state updaters from
      // onText/onMetadata (which check this ref) settle first.  When multiple
      // WS frames arrive in the same event-loop tick (common for Codex where
      // text/metadata/done fire in rapid succession), React batches the
      // setMessages calls — the updaters run after all onmessage handlers, at
      // which point the ref is already null and the appendToLastTextBlock
      // identity check fails, losing text.
      queueMicrotask(() => { currentMsgRef.current = null })
      thinkingBufRef.current = ''
      setThinkingActive(false)
      setCompressingActive(false)
      setAborting(false)
      // Mark foreground subagent blocks as done (bg agents keep spinning)
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasFg = msg.blocks.some(
            (b) => b.type === 'subagent' && b.isActive && !(b as any)._background,
          )
          if (!hasFg) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'subagent' && b.isActive && !(b as any)._background
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          if (msg === currentMsgRef.current) currentMsgRef.current = updatedMsg
          return updatedMsg
        }),
      )
      // Background subagents + running delegates stay in the badge because onDone
      // leaves their blocks active/running (only fg is marked done above).
      setTurnStartTime(null)
      setPermissionPending(false)

      // Defensive: if the streaming bubble never received a text block (e.g.
      // tool-only turn that DID produce text the user didn't see, or text
      // events dropped between WS frames and React render), auto-refetch this
      // chat's history from DB. The backend pump persists every event before
      // signaling done, so a clean refetch gives the canonical end-of-turn
      // state. No-op when the bubble has content.
      // Skip the refetch while a background sub-agent is still running: the
      // refetch rebuilds from DB history, where a task_spawn is always isActive:
      // false ("Historical = completed"), so it would clobber the still-running
      // bg badge (Codex: the main turn ends while the sub keeps going). Once the
      // sub finishes, the next turn's onDone (bgStillRunning false) runs the
      // refetch and history then correctly shows it done.
      if (options.enableDefensiveRefetch && !bgStillRunning) {
        // Capture the bubble NOW (synchronously). The queueMicrotask above nulls
        // currentMsgRef BEFORE this 400ms timeout fires, so reading the ref inside
        // the timeout always saw null → "no content" → refetch EVERY turn →
        // rebuild messages (new ids) → remount the bubble → kill playing media
        // (voice-mode TTS, audio/video players). The recovery is only meant to
        // rescue a genuinely empty bubble (text OR any rich block = keep)…
        const bubble = currentMsgRef.current
        const hasContent = (bubble?.blocks ?? []).some((b: any) =>
          (b.type === 'text' && (b.content || '').trim().length > 0) ||
          ['audio', 'video', 'images', 'image_generating', 'file', 'url', 'document_preview', 'media_processing'].includes(b.type),
        )
        // …or a view seeded from a live_state snapshot (mid-turn attach): the
        // snapshot races the turn's end, so a truncated tail LOOKS like
        // content and the empty-bubble heuristic never fires. DB is
        // authoritative at turn end — reconcile once. One-shot: chat_history
        // clears the flag, so this can't loop. (Snapshot-seeded views were
        // already fully rebuilt at attach — no playing media to interrupt.)
        const seededMidTurn = liveStateSeededRef.current
        liveStateSeededRef.current = false
        if (!hasContent || seededMidTurn) {
          setTimeout(() => {
            const cid = chatIdRef.current
            if (cid) { try { ws.resumeChat(cid) } catch { /* ignore */ } }
          }, 400)
        }
      }
    },

    onError: (message) => {
      sentWithBubbleRef.current = null  // turn ended — clear the bubble-dedup marker
      if (discardingRef.current) return
      // If we were warming up, the session failed to start — reset state so the
      // user sees the error and can retry instead of being stuck on "Starting
      // session..." forever. The optimistic user bubble + placeholder (if any)
      // are removed here; the page tail resets its own warmup flags + refs.
      if (options.isWarmingUp) {
        setMessages((prev) => {
          if (prev.length >= 2 && prev[prev.length - 1].role === 'assistant' && prev[prev.length - 2].role === 'user') {
            return prev.slice(0, -2)
          }
          return prev
        })
        options.onWarmupReset?.()
      }
      options.onErrorExtra?.()
      if (currentMsgRef.current) {
        appendToLastTextBlock(`\n\n**Error:** ${message}`)
      }
      currentMsgRef.current = null
      setTurnStartTime(null)
    },

    onWarmupFailed: (data) => {
      // Stale: warmup_failed for a chat we're no longer on.
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      // Cold-start that set the optimistic-bubble marker just failed — clear it
      // so the next queued message isn't suppressed.
      sentWithBubbleRef.current = null
      // Page tail: clear warmup state so input re-enables (chat); unblock the
      // loading spinner (task).
      options.onWarmupFailedReset?.()
      // The backend tags warmup_failed with a `reason`: 'no_subscription' (setup
      // card), 'target_unavailable' (the remote machine is offline/unreachable),
      // or 'session_error' (the session failed to START on a reachable machine —
      // a config/spawn error, NOT an availability problem). Anything unrecognized
      // (incl. a reason-less local failure) falls back to 'session_error' rather
      // than the misleading "Remote machine unavailable". Convert the empty
      // assistant placeholder into that system block carrying the backend's
      // human-readable message; the user's bubble stays. If no placeholder is
      // present (resume path / task), append one.
      const reason = (data as { reason?: string }).reason
      const subtype: 'no_subscription' | 'target_unavailable' | 'session_error' =
        reason === 'no_subscription'
          ? 'no_subscription'
          : reason === 'target_unavailable'
            ? 'target_unavailable'
            : 'session_error'
      setMessages((prev) => {
        if (prev.length === 0) {
          // chat: leave the empty chat untouched. task: append the error inline.
          return options.appendErrorOnEmptyWarmupFail
            ? [{
                id: `warmup-fail-${Date.now()}`,
                role: 'assistant',
                blocks: [{ type: 'system', subtype, message: data.error }],
                createdAt: new Date().toISOString(),
              }]
            : prev
        }
        const last = prev[prev.length - 1]
        if (last.role === 'assistant' && last.blocks.length === 0) {
          return [
            ...prev.slice(0, -1),
            { ...last, blocks: [{ type: 'system', subtype, message: data.error }] },
          ]
        }
        return [
          ...prev,
          {
            id: `warmup-fail-${Date.now()}`,
            role: 'assistant',
            blocks: [{ type: 'system', subtype, message: data.error }],
            createdAt: new Date().toISOString(),
          },
        ]
      })
      currentMsgRef.current = null
      setTurnStartTime(null)
    },

    // Guard against a stale mode/model frame from the PREVIOUS chat landing
    // mid-switch (discardingRef set, before the new chat_history) and
    // contaminating the new chat's mode/model.
    onModeChanged: (m) => { if (discardingRef.current) return; setMode(m) },
    onModelChanged: (m) => { if (discardingRef.current) return; setModel(m) },

    onQueued: (data) => {
      if (discardingRef.current) return
      // Skip the queue display ONLY when this exact message already has a
      // bubble (the reconnect/stale-pump dedup) — any OTHER text is a real
      // mid-turn queue (claude's steer fallback) and must show its chip.
      if (sentWithBubbleRef.current === data.text) return
      options.queue.addQueued(data.index, data.text)
    },
    onQueueRemoved: (_msg) => {
      // No-op for queuedMessages — the page's cancel/edit handlers already
      // removed from state before sending cancel_queued to the backend.
      // onQueueEditReturn handles pulling text back to input.
    },
    onQueueSent: (data) => {
      if (discardingRef.current) return
      options.queue.clearQueued()
      // If the page already added a user bubble + placeholder for THIS message,
      // don't duplicate — just refresh the timer for the actual streaming start.
      if (sentWithBubbleRef.current === data.text) {
        sentWithBubbleRef.current = null
        setTurnStartTime(Date.now())
        return
      }
      // Queue processed — add user bubble + assistant placeholder.
      const userMsg: DisplayMessage = {
        id: `user-${Date.now()}`,
        role: 'user',
        blocks: [{ type: 'text', content: data.text }],
        createdAt: new Date().toISOString(),
      }
      // During meetings, skip the assistant placeholder — meeting agents create
      // their own messages. An empty placeholder would persist with loading dots
      // since no agent fills it.
      if (meetingActive) {
        setMessages((prev) => [...prev, userMsg])
        return
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
    },

    onSteered: (data) => {
      if (discardingRef.current) return
      // Mid-turn steer accepted: the message went INTO the running turn (so
      // no queue entry and no timer reset). The engine consumes it at the
      // NEXT sampling-round boundary — while an assistant message is still
      // open and streaming, splitting here cut its text mid-sentence, so
      // hold the bubble and let the next block boundary render it (the
      // flushPendingSteer calls in the block handlers + turn end).
      if (currentMsgRef.current) {
        pendingSteerRef.current.push(data.text)
        return
      }
      // Idle position (between blocks): render at the live position and
      // continue the assistant's response in a NEW message below it.
      const userMsg: DisplayMessage = {
        id: `user-${Date.now()}`,
        role: 'user',
        blocks: [{ type: 'text', content: data.text }],
        createdAt: new Date().toISOString(),
      }
      const continuation: DisplayMessage = {
        id: `stream-${Date.now()}`,
        role: 'assistant',
        blocks: [],
        createdAt: new Date().toISOString(),
      }
      currentMsgRef.current = continuation
      setMessages((prev) => [...prev, userMsg, continuation])
    },

    onUserMessage: (content) => {
      if (discardingRef.current) return
      // Backend-injected user message (e.g. auto "implement plan" prompt)
      const userMsg: DisplayMessage = {
        id: `user-${Date.now()}`,
        role: 'user',
        blocks: [{ type: 'text', content }],
        createdAt: new Date().toISOString(),
      }
      // During meetings, skip placeholder (meeting agents create own messages)
      if (meetingActive) {
        setMessages((prev) => [...prev, userMsg])
        return
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
    },

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

    onAborted: (data) => {
      sentWithBubbleRef.current = null  // turn ended — clear the bubble-dedup marker
      if (discardingRef.current) return
      // The actual cleanup ran in handleAbort already (proactive — so the UI
      // clears even if the server's confirmation event never arrives). Re-running
      // it here is cheap (idempotent) and covers any blocks that arrived between
      // the local handleAbort call and this event.
      finalizeAbortedTurn()
      // Only THIS terminal frame (not the proactive finalize) disarms the
      // straggler guard: a graceful abort keeps the engine draining briefly,
      // and its late chunks must not reopen a fresh assistant header.
      abortedRef.current = false
      if (data.session_id) {
        setSessionId(data.session_id)
      }
    },

    onQueueEditReturn: (data) => {
      // Pull the text back into input for editing
      setEditText(data.text)
    },

    onLiveState: (data) => {
      if (discardingRef.current) return
      // Reconnected to an actively streaming chat — restore live state.
      // A residual snapshot (streaming === false: turn ended, bg subagents
      // still running) must not start a turn timer.
      if (data.streaming !== false) {
        setTurnStartTime(data.started_at ? data.started_at * 1000 : Date.now())
      }

      // Restore thinking state
      if (data.thinking_active || data.thinking_text) {
        setThinkingActive(!!data.thinking_active)
        thinkingBufRef.current = data.thinking_text || ''
      }

      // Reconstruct messages from ordered live_blocks.
      // For meetings: split at meeting_turn_start to create separate messages per agent.
      const liveBlocks = Array.isArray(data.live_blocks) ? data.live_blocks : []
      const hasContent = liveBlocks.length > 0 || data.thinking_active || data.thinking_text
      // Mid-turn attach with reconstructed content: mark the view snapshot-
      // seeded so onDone reconciles it against DB once the turn ends. A
      // residual snapshot (streaming === false) is already post-persist.
      if (hasContent && data.streaming !== false) {
        liveStateSeededRef.current = true
      }
      if (hasContent) {
        const newMsgs = buildLiveStateMessages(liveBlocks, {
          active: data.thinking_active,
          text: data.thinking_text,
          tokens: data.thinking_tokens,
        })
        // Check if live_blocks contain meeting_started — append banner to last
        // assistant message from DB (prev), not as a new message.
        const hasMeetingStartedBlock = liveBlocks.some(
          (lb: any) => lb.type === 'system' && lb.subtype === 'meeting_started'
        )

        if (newMsgs.length > 0 || hasMeetingStartedBlock) {
          if (newMsgs.length > 0) {
            currentMsgRef.current = newMsgs[newMsgs.length - 1]
          }
          // Set meetingSpeakerRef if last message has meeting agent identity
          const lastMsg = newMsgs[newMsgs.length - 1]
          if (lastMsg?.badge === 'meeting' && lastMsg.agentSlug) {
            meetingSpeakerRef.current = {
              slug: lastMsg.agentSlug,
              displayName: lastMsg.agentDisplayName || lastMsg.agentSlug,
              color: lastMsg.agentColor || '',
            }
          }
          setMessages((prev) => {
            let updated = [...prev]
            if (hasMeetingStartedBlock) {
              for (let i = updated.length - 1; i >= 0; i--) {
                if (updated[i].role === 'assistant') {
                  updated[i] = { ...updated[i], blocks: [...updated[i].blocks, { type: 'system' as const, subtype: 'meeting_started' }] }
                  break
                }
              }
            }
            return [...updated, ...newMsgs]
          })
        }
      }

      // (Status-bar badges DERIVE from the reconstructed subagent/delegate
      // blocks above — the backend live_blocks carry each agent's `active` flag,
      // so a reconnect mid-run shows exactly the still-running set and clears it
      // when the snapshot goes empty. No parallel array to sync.)

      // Restore live dynamic-workflow trees (workflows is a {tuid: {...}} map).
      const wfRaw = (data as any).workflows
      const wfMap = wfRaw && typeof wfRaw === 'object' && !Array.isArray(wfRaw) ? wfRaw : {}
      const wfList = Object.values(wfMap).map((w: any) => ({
        toolUseId: w.tool_use_id,
        workflowName: w.workflow_name || '',
        progress: Array.isArray(w.progress) ? w.progress : [],
        active: w.active !== false,
      }))
      setWorkflows(wfList)

      // Re-present pending permission or plan_review if any
      if (data.pending_permission) {
        const pp = data.pending_permission
        const ppType = pp.event_type || 'permission_prompt'
        setPermissionPending(true)
        setTurnStartTime(null)  // Pause timer — LLM is blocked
        ensureAssistantMsg()
        if (ppType === 'plan_review') {
          appendBlock({
            type: 'plan_review',
            requestId: pp.request_id || '',
            plan: pp.plan || '',
            toolInput: pp.tool_input || {},
            filename: pp.filename || '',
          })
        } else if (ppType === 'question_prompt') {
          // Reconnect mid-question (codex held turn) — re-render the card with
          // its request_id so the answer still resolves the held request.
          appendBlock({
            type: 'question',
            toolName: pp.tool_name || 'request_user_input',
            toolInput: pp.tool_input || {},
            requestId: pp.request_id || '',
          })
        } else {
          appendBlock({
            type: 'permission',
            requestId: pp.request_id || '',
            toolName: pp.tool_name || '',
            toolInput: pp.tool_input || {},
            description: pp.description,
          })
        }
      }

      // Restore todo state from live streaming
      if (Array.isArray(data.todos) && data.todos.length > 0) setCurrentTodos(data.todos)

      // Restore the thread goal — only when non-null: the goal is chat-durable
      // and already seeded from restore.goal (chat_history), so a fresh pump's
      // null must not wipe it. A genuine clear is consistent either way (the
      // cleared event NULLs the DB, so restore.goal agrees).
      if (data.goal) setCurrentGoal(data.goal)

      // Restore meeting state from live streaming
      if (data.meeting_agent || (data.meeting_participants && data.meeting_participants.length > 0)) {
        setMeetingActive(true)
        if (data.meeting_agent) setMeetingSpeaker(data.meeting_agent)
        if (Array.isArray(data.meeting_participants)) setMeetingParticipants(data.meeting_participants)
      }
    },

    // --- Todo list ---
    onTodoUpdate: (data) => {
      if (discardingRef.current) return
      setCurrentTodos(Array.isArray(data.todos) ? data.todos : [])
    },

    // --- Thread goal (live frame applies unconditionally — null clears) ---
    onGoalUpdate: (data) => {
      if (discardingRef.current) return
      setCurrentGoal(data.goal ?? null)
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

  // display_ui artifacts ride the generic frame registry rather than a
  // useDashboardWs switch case — the registry fires for EVERY frame type, so
  // a self-contained feature needs no dispatcher growth. `ui` is not in
  // PER_CHAT_FRAMES, so the handler self-gates by chat_id (background chats'
  // frames must not render here). Ref-indirected so the once-registered
  // subscriber always sees the latest closure state.
  const onUiFrameRef = useRef<(msg: any) => void>(() => {})
  onUiFrameRef.current = (msg: any) => {
    if (discardingRef.current) return
    if (msg.chat_id && chatIdRef.current && msg.chat_id !== chatIdRef.current) return
    const block = eventToBlock(msg)
    if (!block) return
    ensureAssistantMsg()
    appendBlock(block)
  }
  const wsSubscribe = ws.subscribe
  useEffect(() => wsSubscribe('ui', (msg: any) => onUiFrameRef.current(msg)), [wsSubscribe])

  // display_ui backchannel. Two disjoint chip-append sources: the
  // `artifact_interaction` frame renders QUEUED interactions at boundary
  // delivery (pump drain), the `sent` ack below renders the idle path — the
  // server only ever emits one of the two per send, so no dedupe is needed.
  const appendArtifactChipTurn = (token: string, title: string | undefined, payload: unknown) => {
    const chipMsg: DisplayMessage = {
      id: `artifact-int-${Date.now()}`,
      role: 'assistant',
      blocks: [{ type: 'artifact_interaction', token, title, payload }],
      createdAt: new Date().toISOString(),
    }
    const placeholder: DisplayMessage = {
      id: `stream-${Date.now()}`,
      role: 'assistant',
      blocks: [],
      createdAt: new Date().toISOString(),
    }
    currentMsgRef.current = placeholder
    setMessages((prev) => [...prev, chipMsg, placeholder])
    setTurnStartTime(Date.now())
  }
  const onArtifactFrameRef = useRef<(msg: any) => void>(() => {})
  onArtifactFrameRef.current = (msg: any) => {
    if (discardingRef.current) return
    if (msg.chat_id && chatIdRef.current && msg.chat_id !== chatIdRef.current) return
    appendArtifactChipTurn(msg.token || '', msg.title || undefined, msg.payload)
  }
  useEffect(() => wsSubscribe('artifact_interaction', (msg: any) => onArtifactFrameRef.current(msg)), [wsSubscribe])

  // In-flight send acks, keyed by token (the server's ≥1s per-token
  // min-interval guarantees one in-flight send per token).
  const artifactAckWaiters = useRef<Map<string, (ack: { status: string; reason?: string }) => void>>(new Map())
  useEffect(() => wsSubscribe('artifact_ack', (msg: any) => {
    const waiter = artifactAckWaiters.current.get(msg.token || '')
    if (waiter) {
      artifactAckWaiters.current.delete(msg.token || '')
      waiter({ status: msg.status || 'denied', reason: msg.reason })
    }
  }), [wsSubscribe])

  const sendArtifactInteraction = useCallback(
    (token: string, title: string, payload: unknown): Promise<{ status: string; reason?: string }> => {
      const cid = chatIdRef.current
      if (!cid || !ws.connected) {
        return Promise.resolve({ status: 'unavailable', reason: 'no live chat' })
      }
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          artifactAckWaiters.current.delete(token)
          resolve({ status: 'denied', reason: 'timeout' })
        }, 10000)
        artifactAckWaiters.current.set(token, (ack) => {
          clearTimeout(timer)
          if (ack.status === 'sent') {
            // The interaction started a real turn — render its chip + a fresh
            // placeholder and flip the same streaming state a user send flips.
            abortedRef.current = false
            appendArtifactChipTurn(token, title || undefined, payload)
            ws.setStreaming(true)
            useChatStore.getState().setStreaming(cid)
          }
          resolve(ack)
        })
        ws.sendArtifactInteraction(cid, token, title, payload)
      })
    },
    [ws],
  )

  // Pinned mini-app send_prompt actions — the app_action twin of the artifact
  // backchannel above: same chip-turn semantics (queued interactions render
  // via the `app_action` frame at boundary drain; the `sent` ack renders the
  // idle path), acks keyed by app+action.
  const appendAppActionChipTurn = (msg: { app_id?: string; slug?: string; title?: string; action_id?: string; label?: string; prompt?: string }) => {
    const chipMsg: DisplayMessage = {
      id: `app-action-${Date.now()}`,
      role: 'assistant',
      blocks: [{
        type: 'app_action', appId: msg.app_id || '', slug: msg.slug || undefined,
        title: msg.title || undefined, actionId: msg.action_id || '',
        label: msg.label || undefined, prompt: msg.prompt || undefined,
      }],
      createdAt: new Date().toISOString(),
    }
    const placeholder: DisplayMessage = {
      id: `stream-${Date.now()}`,
      role: 'assistant',
      blocks: [],
      createdAt: new Date().toISOString(),
    }
    currentMsgRef.current = placeholder
    setMessages((prev) => [...prev, chipMsg, placeholder])
    setTurnStartTime(Date.now())
  }
  const onAppActionFrameRef = useRef<(msg: any) => void>(() => {})
  onAppActionFrameRef.current = (msg: any) => {
    if (discardingRef.current) return
    if (msg.chat_id && chatIdRef.current && msg.chat_id !== chatIdRef.current) return
    appendAppActionChipTurn(msg)
  }
  useEffect(() => wsSubscribe('app_action', (msg: any) => onAppActionFrameRef.current(msg)), [wsSubscribe])

  const appActionAckWaiters = useRef<Map<string, (ack: { status: string; reason?: string }) => void>>(new Map())
  useEffect(() => wsSubscribe('app_action_ack', (msg: any) => {
    const key = `${msg.app_id || ''}:${msg.action_id || ''}`
    const waiter = appActionAckWaiters.current.get(key)
    if (waiter) {
      appActionAckWaiters.current.delete(key)
      waiter({ status: msg.status || 'denied', reason: msg.reason })
    }
  }), [wsSubscribe])

  const sendAppAction = useCallback(
    (app: { id: string; slug?: string; title?: string }, actionId: string, label: string, prompt: string, args: unknown): Promise<{ status: string; reason?: string }> => {
      const cid = chatIdRef.current
      if (!cid || !ws.connected) {
        return Promise.resolve({ status: 'unavailable', reason: 'no live chat' })
      }
      const key = `${app.id}:${actionId}`
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          appActionAckWaiters.current.delete(key)
          resolve({ status: 'denied', reason: 'timeout' })
        }, 10000)
        appActionAckWaiters.current.set(key, (ack) => {
          clearTimeout(timer)
          if (ack.status === 'sent') {
            abortedRef.current = false
            appendAppActionChipTurn({
              app_id: app.id, slug: app.slug, title: app.title,
              action_id: actionId, label, prompt,
            })
            ws.setStreaming(true)
            useChatStore.getState().setStreaming(cid)
          }
          resolve(ack)
        })
        ws.sendAppAction(cid, app.id, actionId, args)
      })
    },
    [ws],
  )

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
    [chatId, ws],
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
    [chatId, ws],
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
    [chatId, ws],
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
    setTurnStartTime(Date.now())  // Restart timer — LLM resumes
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
        if (msg === currentMsgRef.current) currentMsgRef.current = updated
        return updated
      }),
    )
    // Mode change is handled by the backend (sends mode_changed event)
  }, [ws, messages])

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
            || (b.type === 'delegate' && b.status === 'running')
            || (b.type === 'bgcommand' && b.isActive),
        )
        if (!hasRunning) return msg
        return {
          ...msg,
          blocks: msg.blocks.map((b) => {
            if (b.type === 'tool' && b.status === 'running') return { ...b, status: 'failed' as const }
            if (b.type === 'subagent' && b.isActive) return { ...b, isActive: false, failed: true }
            if (b.type === 'bgcommand' && b.isActive) return { ...b, isActive: false, failed: true }
            if (b.type === 'delegate' && b.status === 'running') return { ...b, status: 'failed' as const }
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
    if (meetingActive) {
      setMeetingActive(false)
      setMeetingSpeaker(null)
      setMeetingParticipants([])
      meetingSpeakerRef.current = null
    }
    if (options.clearQueueOnAbort) options.queue.clearQueued()
  }, [meetingActive, options])

  return {
    ws,
    // state
    messages, setMessages,
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
    meetingRound, setMeetingRound,
    meetingMaxRounds,
    meetingLeftParticipants,
    editText, setEditText,
    dismissPreview,
    // refs
    currentMsgRef,
    thinkingBufRef,
    abortedRef,
    discardingRef,
    sentWithBubbleRef,
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
