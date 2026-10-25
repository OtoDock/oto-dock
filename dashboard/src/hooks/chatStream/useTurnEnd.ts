// The turn's end of the chat stream (split from useChatStream): done and
// the end-of-turn reconcile, the error that ends a turn with its card, the
// warm-up that failed and the card it shows, the server's own turn start,
// and the stop's confirmation.
import { isArtifactBlock } from '../../lib/kinds/artifact'
import { continuesAbove, nudgeBlock, reviewSpeaker } from '../../lib/messageBlocks'
import { SERVER_TURN_CAUSE, SYSTEM_SUBTYPE } from '../../api/wireEvents'
import type { DisplayMessage } from '../../components/chat/types'
import type { WsCallbacks } from '../useDashboardWs.types'
import type { StreamCore } from './core'

// Reasons the backend's NoSubscriptionError classifier emits — all of them are
// "connect/reconnect an account" prompts and render the amber setup card, not
// the red crash card. 'throttled' is deliberately absent (a transient rest is
// not a setup problem) and falls through to session_error with the backend's
// try-again message.
const SUBSCRIPTION_REASONS = new Set([
  'no_subscription', 'auth_off', 'admin_oauth_only', 'no_pool', 'none',
  'own_sub_expired',
])

export function warmupFailSubtype(
  reason: string | undefined,
): 'no_subscription' | 'pool_cap' | 'target_unavailable' | 'session_error' | 'below_editor' {
  if (reason && SUBSCRIPTION_REASONS.has(reason)) return 'no_subscription'
  // The pool's subscription cap refused the spawn: its own card, with the
  // cap's wording and a way to the Usage tab.
  if (reason === 'pool_cap') return 'pool_cap'
  if (reason === 'target_unavailable') return 'target_unavailable'
  // A chat that runs as the agent, below the editor role: a role card.
  if (reason === 'below_editor') return 'below_editor'
  return 'session_error'
}

export function useTurnEnd(core: StreamCore, deps: {
  /** The meeting hook's: no completion ping during a meeting. */
  meetingActive: boolean
  /** The history hook's: a held history paints as it is at a turn's end. */
  paintHeldHistory: () => void
  /** The queue hook's: an accepted steer still owes its bubble, and the
   * send's bubble marker ends with the turn. */
  flushPendingSteer: (withContinuation?: boolean) => void
  clearSentMarkers: () => void
  /** The entry's shared stop cleanup, run again at the server's confirmation. */
  finalizeAbortedTurn: () => void
  /** The entry's: the rows this view lacks, from the socket. */
  refetchHistory: (chatId: string) => void
}) {
  const {
    options, chatIdRef, discardingRef, abortedRef, erroredRef, liveStateSeededRef, thinkingBufRef, messagesRef,
    setMessages, currentMsgRef, reviewNudgeRef, ensureAssistantMsg, appendBlock, appendToLastTextBlock,
    setTurnStartTime, setThinkingActive, setCompressingActive, setAborting, setPermissionPending, setSessionId,
  } = core
  const { meetingActive, paintHeldHistory, flushPendingSteer, clearSentMarkers, finalizeAbortedTurn, refetchHistory } = deps

  const callbacks: Pick<WsCallbacks, 'onDone' | 'onError' | 'onWarmupFailed' | 'onServerTurnStart' | 'onAborted'> = {
    onDone: () => {
      // Clear the optimistic-bubble dedup marker on EVERY turn end, even a
      // discarded/non-viewed one — if it leaks (turn ended while
      // discarding, or via abort/error/warmup-failed below), onQueued/onQueueSent
      // suppress a same-text queued message.
      clearSentMarkers()
      reviewNudgeRef.current = null  // a nudge no review took is over with the turn
      if (discardingRef.current) return
      paintHeldHistory()
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
          (b) => (b.type === 'subagent' && b.isActive && b._background)
            // A live Commands badge gates the refetch too: history
            // rehydrates bg_command_spawn rows isActive:false, so a
            // refetch would clobber a still-running command's badge.
            || (b.type === 'bgcommand' && b.isActive),
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
      // Mark foreground subagent blocks as done (bg agents keep spinning).
      // Return `prev` UNCHANGED when no message carried one: the common
      // turn has no fg subagent, and a fresh array here forces a full
      // list re-render in the exact tick the turn-end burst competes with
      // TTS scheduling (per-message identity was already preserved; the
      // array identity is what lets React bail out entirely).
      setMessages((prev) => {
        let changed = false
        const next = prev.map((msg) => {
          if (msg.role !== 'assistant') return msg
          const hasFg = msg.blocks.some(
            (b) => b.type === 'subagent' && b.isActive && !b._background,
          )
          if (!hasFg) return msg
          const blocks = msg.blocks.map((b) =>
            b.type === 'subagent' && b.isActive && !b._background
              ? { ...b, isActive: false }
              : b,
          )
          const updatedMsg = { ...msg, blocks }
          changed = true
          return updatedMsg
        })
        return changed ? next : prev
      })
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
      const errored = erroredRef.current
      erroredRef.current = false
      if (options.enableDefensiveRefetch && !bgStillRunning && !errored) {
        // The bubble's id NOW (synchronously): the queueMicrotask above nulls
        // currentMsgRef BEFORE the 400ms timeout fires, so reading the ref
        // inside the timeout always saw null → "no content" → refetch EVERY
        // turn → rebuild messages (new ids) → remount the bubble → kill
        // playing media (voice-mode TTS, audio/video players). The bubble's
        // blocks are read in the timeout from the committed list, which by
        // then holds every chunk the batched updaters landed. The recovery
        // is only meant to rescue a genuinely empty bubble (text OR any rich
        // block = keep)…
        const targetId = currentMsgRef.current?.id ?? null
        // …or a view seeded from a live_state snapshot (mid-turn attach): the
        // snapshot races the turn's end, so a truncated tail LOOKS like
        // content and the empty-bubble heuristic never fires. DB is
        // authoritative at turn end — reconcile once. One-shot: chat_history
        // clears the flag, so this can't loop. (Snapshot-seeded views were
        // already fully rebuilt at attach — no playing media to interrupt.)
        const seededMidTurn = liveStateSeededRef.current
        liveStateSeededRef.current = false
        setTimeout(() => {
          const bubble = targetId ? messagesRef.current.find((m) => m.id === targetId) : undefined
          // A bubble a history frame replaced meanwhile (the post-turn delta
          // of a task or meeting chat, the held rows painted at done) is the
          // DB's own: nothing to rescue.
          if (targetId && !bubble && !seededMidTurn) return
          const hasContent = (bubble?.blocks ?? []).some((b) =>
            (b.type === 'text' && (b.content || '').trim().length > 0) || isArtifactBlock(b.type),
          )
          if (hasContent && !seededMidTurn) return
          const cid = chatIdRef.current
          if (!cid) return
          const refetch = () => refetchHistory(cid)
          // The refetch rebuilds every message id → full remount — a
          // hard cut for any playing audio. Route through the caller's
          // defer hook so it waits out live TTS (runs immediately when
          // nothing is speaking).
          if (options.deferHeavy) options.deferHeavy(refetch)
          else refetch()
        }, 400)
      }
    },

    onError: (message, ending, refused) => {
      if (refused) {
        // One queued or busy send refused, no turn ended: the warmup, the
        // bubble and the markers stay as they are. The socket hook handed
        // what was sent back to its chat's composer already; the card says
        // what was refused, on the chat the send was typed into.
        if (discardingRef.current) return
        if (refused.chatId && refused.chatId !== chatIdRef.current) return
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype: SYSTEM_SUBTYPE.QUEUE_REFUSED, message,
                      returned: refused.returned })
        return
      }
      clearSentMarkers()  // turn ended — clear the bubble-dedup marker
      reviewNudgeRef.current = null
      if (discardingRef.current) return
      paintHeldHistory()
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
      options.onErrorExtra?.(message)
      if (ending?.reason) {
        // A typed ending is the card the chat keeps after a reload (the
        // server persists the `turn_ended` row inside the turn), rendered
        // live from the frame in the open bubble, or a bubble of its own.
        erroredRef.current = true
        ensureAssistantMsg()
        appendBlock({ type: 'system', subtype: SYSTEM_SUBTYPE.TURN_ENDED,
                      reason: ending.reason, message })
      } else if (currentMsgRef.current) {
        appendToLastTextBlock(`\n\n**Error:** ${message}`)
      }
      // The error ends the turn (no done follows): an accepted steer still
      // owes its user bubble, like at done and abort.
      flushPendingSteer(false)
      // Deferred like at done: the append above is a batched updater that
      // checks this ref when it runs.
      queueMicrotask(() => { currentMsgRef.current = null })
      setTurnStartTime(null)
    },

    onWarmupFailed: (data) => {
      // Stale: warmup_failed for a chat we're no longer on.
      if (data.chat_id && chatIdRef.current && data.chat_id !== chatIdRef.current) {
        return
      }
      // A refusal before any chat was minted (a send below the editor role
      // on a Shared-only agent) belongs to the new-chat page: once the page
      // is on a chat it is another chat's.
      if (!data.chat_id && chatIdRef.current) return
      // Cold-start that set the optimistic-bubble marker just failed — clear it
      // so the next queued message isn't suppressed.
      clearSentMarkers()
      // Page tail: clear warmup state so input re-enables (chat); unblock the
      // loading spinner (task).
      options.onWarmupFailedReset?.(data)
      // The backend tags warmup_failed with a `reason`. Subscription-block
      // reasons (the NoSubscriptionError classifier: auth_off,
      // admin_oauth_only, no_pool, none, own_sub_expired) render the amber
      // setup card — they are "connect/reconnect an account" prompts, not
      // crashes. 'target_unavailable' means the remote machine is
      // offline/unreachable. Anything unrecognized (incl. a reason-less local
      // failure, and 'throttled' — a transient rest, not a setup problem)
      // falls back to 'session_error' rather than the misleading "Remote
      // machine unavailable". Convert the empty assistant placeholder into
      // that system block carrying the backend's human-readable message; the
      // user's bubble stays. If no placeholder is present (resume path /
      // task), append one.
      const reason = (data as { reason?: string }).reason
      const subtype = warmupFailSubtype(reason)
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
    onServerTurnStart: (data) => {
      // Only the review's own turn takes the noted nudge: one whose turn did
      // not start (another starter held the chat, it was kept for later)
      // labels no later wake, result or first turn.
      const nudge = data.cause === SERVER_TURN_CAUSE.BG_REVIEW ? reviewNudgeRef.current : null
      reviewNudgeRef.current = null
      if (discardingRef.current) return
      // A server-initiated turn (bg-command / bg-subagent review nudge, or a
      // delegate-result synthesis) is now streaming — no user-send set the live
      // timer, so start it here (mirrors the send path). done/aborted clear it.
      setTurnStartTime(Date.now())
      // A background review: its bubble starts with the nudge's line, speaks
      // as the meeting bubble above it in a meeting, and goes on from the
      // reply above, as its stored row draws it.
      if (nudge && !currentMsgRef.current) {
        const committed = messagesRef.current
        const host: DisplayMessage = {
          id: `stream-${Date.now()}`,
          role: 'assistant',
          blocks: [nudgeBlock(nudge.subtype, nudge.count)],
          createdAt: new Date().toISOString(),
          ...reviewSpeaker(committed[committed.length - 1]),
        }
        currentMsgRef.current = host
        setMessages((prev) => [...prev, continuesAbove(prev[prev.length - 1], host) ? { ...host, continuation: true } : host])
      }
    },

    onAborted: (data) => {
      clearSentMarkers()  // turn ended — clear the bubble-dedup marker
      reviewNudgeRef.current = null
      if (discardingRef.current) return
      paintHeldHistory()
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

  }

  return { callbacks }
}
