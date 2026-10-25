// The history and the resume of the chat stream (split from useChatStream):
// a chat_history frame rebuilt from the rows (the optimistic bubbles a slow
// load would erase carried over), a delta merged into the rows the view
// holds, a history held for the live_state that follows it so a generating
// chat paints once, and the live_state rebuild of a turn joined mid-way.
import { useCallback, useRef } from 'react'
import { useChatStore } from '@/store/chatStore'
import { continuesAbove, dbMessagesToDisplay, finishedBubble, latestCostBilled, toolFeesOf, withPlanRows } from '../../lib/messageBlocks'
import { HOOK_ITEM, SYSTEM_SUBTYPE, type DbMessageRow, type LiveStateFrame, type MeetingRestore } from '../../api/wireEvents'
import type { WsCallbacks } from '../useDashboardWs.types'
import type { DisplayMessage } from '../../components/chat/types'
import { buildLiveStateMessages } from './liveStateBuilder'
import type { StreamCore } from './core'

/**
 * The rebuilt (DB-authoritative) message list plus the trailing OPTIMISTIC
 * user bubbles of the previous list (send-minted `user-<ts>` ids; history
 * rows are `db-*`) that the rebuilt tail does not already hold, text-deduped
 * against it: a send can race a slow history load, and a wholesale replace
 * would erase the prompt until the next reload. Empty `stream-<ts>`
 * assistant placeholders are never carried (streaming re-mints its bubble).
 */
function withCarriedUserBubbles(prev: DisplayMessage[], displayMsgs: DisplayMessage[]): DisplayMessage[] {
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
}

// How long a history whose live attach was promised waits for its
// live_state before it paints alone.
const HELD_HISTORY_MS = 1500

export function useChatHistory(core: StreamCore, deps: {
  /** The queue hook's: a history load drops the steers held for the left view. */
  resetSteers: () => void
  /** The meeting hook's three. */
  applyMeetingRestore: (mt: MeetingRestore | null | undefined) => void
  noteLiveSpeaker: (lastMsg: DisplayMessage | undefined) => void
  applyLiveStateMeeting: (data: LiveStateFrame) => void
}) {
  const {
    options, agents, seedDbRows, chatIdRef, discardingRef, abortedRef, erroredRef, liveStateSeededRef,
    thinkingBufRef,
    setMessages, currentMsgRef, reviewNudgeRef, historyHeldRef, rawRowsRef, planRowsRef, oldestLoadedIdRef, loadingOlderRef, resetWindow,
    setHasMoreOlder, setLoadingOlder,
    ensureAssistantMsg, appendBlock,
    setTurnStartTime, setThinkingActive, setCompressingActive, setPermissionPending, setAborting, setWorkflows,
    setTotalCost, setBilledCost, setToolCost, setCostBilled, setContextUsed, setContextMax, setCacheStats, setSessionPlans, setCurrentTodos,
    setCurrentGoal,
  } = core
  const { resetSteers, applyMeetingRestore, noteLiveSpeaker, applyLiveStateMeeting } = deps

  // Every DB history seed (the interactive rich-view toggle, the persisted-
  // rows nudge) re-derives the flag from the newest row, like onChatHistory.
  const seedDbHistory = useCallback((rows: DbMessageRow[], hasMore: boolean) => {
    seedDbRows(rows, hasMore)
    const billedNewest = latestCostBilled(rows)
    setCostBilled(billedNewest)
    // No chat total rides a seed: a metered chat keeps the money it showed,
    // a subscription chat shows the fees its rows carry.
    const fees = toolFeesOf(rows)
    setToolCost(fees)
    setBilledCost((prev) => (billedNewest ? prev : fees))
  }, [seedDbRows, setBilledCost, setCostBilled, setToolCost])

  // A history frame whose live attach follows (`live_pending`): its rebuilt
  // rows wait here for the turn's live_state, so opening a generating chat
  // paints once (the rows and the live answer together, the timer already
  // running). Painted as they are by the next history frame, done, error,
  // abort or the timer; dropped on a chat switch.
  const heldHistoryRef = useRef<{ chatId: string; msgs: DisplayMessage[];
                                  timer: ReturnType<typeof setTimeout> } | null>(null)

  const dropHeldHistory = () => {
    const held = heldHistoryRef.current
    if (held) {
      clearTimeout(held.timer)
      heldHistoryRef.current = null
    }
    historyHeldRef.current = false
  }
  /** The held history's rows taken for a paint (once), null when none is
   *  held for the chat this view shows. */
  const takeHeldHistory = (): DisplayMessage[] | null => {
    const held = heldHistoryRef.current
    if (!held) return null
    dropHeldHistory()
    return held.chatId === chatIdRef.current ? held.msgs : null
  }
  /** Paint the held rows as they are: no live_state came (the turn ended
   *  first, the attach failed). The timer and the chat's state as an
   *  ordinary history leaves them. */
  const paintHeldHistory = () => {
    const msgs = takeHeldHistory()
    if (!msgs) return
    setMessages((prev) => withCarriedUserBubbles(prev, msgs))
    setTurnStartTime(null)
    if (chatIdRef.current) useChatStore.getState().setReady(chatIdRef.current)
  }
  const holdHistory = (cid: string, msgs: DisplayMessage[]) => {
    const timer = setTimeout(() => {
      if (heldHistoryRef.current?.timer === timer) paintHeldHistory()
    }, HELD_HISTORY_MS)
    heldHistoryRef.current = { chatId: cid, msgs, timer }
    historyHeldRef.current = true
  }

  const callbacks: Pick<WsCallbacks, 'onChatHistory' | 'onChatHistoryDelta' | 'onLiveState'> = {
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
      reviewNudgeRef.current = null
      thinkingBufRef.current = ''
      abortedRef.current = false
      erroredRef.current = false  // the left chat's error card must not skip this chat's reconcile
      resetSteers()  // held steers belong to the previous view
      liveStateSeededRef.current = false  // view is DB-authoritative again
      if (!data.live_pending) setTurnStartTime(null)
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
      // A frame whose live attach follows holds its paint (below), the
      // timer and the chat's state with it: the live_state sets them.
      const livePending = !!data.live_pending
      if (!livePending) {
        const _cid = data.chat_id || chatIdRef.current
        if (_cid) useChatStore.getState().setReady(_cid)
      }
      const dbMessages = Array.isArray(data.messages) ? data.messages : []
      const dbPlans = Array.isArray(data.plans) ? data.plans : []
      // Reset pagination on EVERY chat_history fire (resume / warmup attach /
      // mode-switch / reattach / onDone refetch) — the frame is the newest page,
      // so a re-fire collapses the window back to it (any scrolled-in older pages
      // are dropped). Rows arrive ascending, so [0] is the oldest loaded id.
      resetWindow(data.chat_id || chatIdRef.current)
      rawRowsRef.current = dbMessages
      oldestLoadedIdRef.current = dbMessages.length ? dbMessages[0].id : null
      loadingOlderRef.current = false
      setLoadingOlder(false)
      setHasMoreOlder(!!data.has_more)
      planRowsRef.current = dbPlans
      const displayMsgs = withPlanRows(dbMessagesToDisplay(dbMessages, agents), dbPlans)
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
      dropHeldHistory()
      if (livePending) {
        holdHistory(data.chat_id || chatIdRef.current || '', displayMsgs)
      } else {
        setMessages((prev) => withCarriedUserBubbles(prev, displayMsgs))
      }
      // Restore total cost from DB; the gauge follows the newest turn's flag
      // (reset on every load — a chat switch must not inherit the last one's)
      if (data.total_cost) setTotalCost(data.total_cost)
      const billedNewest = latestCostBilled(dbMessages)
      setCostBilled(billedNewest)
      // The gauge's money: a metered chat's whole total (the row's), else
      // the tool fees the loaded rows carry.
      const fees = toolFeesOf(dbMessages)
      setToolCost(fees)
      setBilledCost(billedNewest ? data.total_cost || 0 : fees)
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
        setSessionPlans(dbPlans.map((p) => ({
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
        applyMeetingRestore(data.restore.meeting)
      }

      options.onChatHistoryLoaded?.(data)
    },

    onChatHistoryDelta: (data) => {
      // A watched task chat's turn end: the rows this view lacks, appended
      // to the raw rows it holds (deduped by id, kept in id order), and the
      // view re-derived from all of them (the converter has cross-row
      // state). Never the first frame of a view: the pagination cursor and
      // the discard guard are untouched, and a frame for another chat is
      // dropped.
      if (!data.chat_id || !chatIdRef.current || data.chat_id !== chatIdRef.current) return
      if (discardingRef.current) return
      options.onChatHistoryMeta?.(data)
      const incoming = Array.isArray(data.messages) ? data.messages : []
      const byId = new Map<number, DbMessageRow>()
      for (const r of rawRowsRef.current) byId.set(r.id, r)
      for (const r of incoming) byId.set(r.id, r)
      const rows = [...byId.values()].sort((a, b) => a.id - b.id)
      rawRowsRef.current = rows
      const displayMsgs = dbMessagesToDisplay(rows, agents)
      currentMsgRef.current = null
      reviewNudgeRef.current = null
      // The same carry rule as onChatHistory: trailing optimistic user
      // bubbles the rows do not hold yet stay; the finished turn's live
      // bubble is replaced by its rows. A held history is superseded (the
      // view re-derives from every row).
      dropHeldHistory()
      if (data.live_pending) {
        holdHistory(data.chat_id, displayMsgs)
      } else {
        setMessages((prev) => withCarriedUserBubbles(prev, displayMsgs))
      }
      if (data.total_cost) setTotalCost(data.total_cost)
      const billedNewest = latestCostBilled(rows)
      setCostBilled(billedNewest)
      const fees = toolFeesOf(rows)
      setToolCost(fees)
      setBilledCost(billedNewest ? data.total_cost || 0 : fees)
      if (data.context_used) setContextUsed(data.context_used)
      if (data.context_max) setContextMax(data.context_max)
      if (data.cache_write) setCacheStats({
        cacheRead: data.cache_read ?? 0,
        cacheWrite: data.cache_write ?? 0,
        inputTokens: 0,
        outputTokens: data.output_tokens ?? 0,
      })
      const dbPlans = Array.isArray(data.plans) ? data.plans : []
      if (dbPlans.length > 0) {
        setSessionPlans(dbPlans.map((p) => ({
          filename: p.filename,
          content: p.content,
          status: p.status as 'pending' | 'implemented' | 'rejected',
        })))
      }
      // The panels and the meeting indicator as the full history restores
      // them: a meeting that concluded during the turn ends here too.
      if (data.restore) {
        setCurrentTodos(Array.isArray(data.restore.todos) ? data.restore.todos : [])
        setCurrentGoal(data.restore.goal ?? null)
        applyMeetingRestore(data.restore.meeting)
      }
    },

    onLiveState: (data) => {
      if (discardingRef.current) return
      // The rows of a history that waited for this frame: painted below in
      // the same update as the live answer.
      const heldRows = takeHeldHistory()
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
      if (heldRows && !hasContent) setMessages((prev) => withCarriedUserBubbles(prev, heldRows))
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
          (lb) => lb.type === 'system' && lb.subtype === SYSTEM_SUBTYPE.MEETING_STARTED
        )

        if (newMsgs.length > 0 || hasMeetingStartedBlock || heldRows) {
          if (newMsgs.length > 0) {
            currentMsgRef.current = newMsgs[newMsgs.length - 1]
          }
          noteLiveSpeaker(newMsgs[newMsgs.length - 1])
          setMessages((prev) => {
            const updated = heldRows ? withCarriedUserBubbles(prev, heldRows) : [...prev]
            if (hasMeetingStartedBlock) {
              for (let i = updated.length - 1; i >= 0; i--) {
                if (updated[i].role === 'assistant') {
                  updated[i] = { ...updated[i], blocks: [...updated[i].blocks, { type: 'system' as const, subtype: SYSTEM_SUBTYPE.MEETING_STARTED }] }
                  break
                }
              }
            }
            // The turn's live part below the rows the history ends with: when
            // those end in the agent's unfinished bubble, the live part goes
            // on from it, drawn as the rows will draw it once the turn ends
            // (the saved part of this turn, or an earlier turn that ended
            // with no metadata, which the rows merge the same way).
            const above = updated[updated.length - 1]
            const [first, ...rest] = newMsgs
            const joined = first && above && !finishedBubble(above) && continuesAbove(above, first)
              ? [{ ...first, continuation: true }, ...rest]
              : newMsgs
            return [...updated, ...joined]
          })
        }
      }

      // (Status-bar badges DERIVE from the reconstructed subagent/delegate
      // blocks above — the backend live_blocks carry each agent's `active` flag,
      // so a reconnect mid-run shows exactly the still-running set and clears it
      // when the snapshot goes empty. No parallel array to sync.)

      // Restore live dynamic-workflow trees (workflows is a {tuid: {...}} map).
      const wfRaw = data.workflows
      const wfMap = wfRaw && typeof wfRaw === 'object' && !Array.isArray(wfRaw) ? wfRaw : {}
      const wfList = Object.values(wfMap).map((w) => ({
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
        if (ppType === HOOK_ITEM.PLAN_REVIEW) {
          appendBlock({
            type: 'plan_review',
            requestId: pp.request_id || '',
            plan: pp.plan || '',
            toolInput: pp.tool_input || {},
            filename: pp.filename || '',
          })
        } else if (ppType === HOOK_ITEM.QUESTION_PROMPT) {
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

      applyLiveStateMeeting(data)
    },

    // --- Todo list ---
  }

  return { callbacks, seedDbHistory, paintHeldHistory }
}
