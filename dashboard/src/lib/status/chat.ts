/**
 * The chat's liveness — the sidebar live-dot, the stop button and the
 * Active-now widget — mirrored from the proxy's authorities
 * `proxy/ws/chat_phase.py` (the words the server speaks) and
 * `proxy/services/delegation/lane_status.py` (the lane words a project
 * board speaks); `tests/storage/test_status_vocabularies.py` binds this
 * file to both (edit both).
 *
 * ONE union. The `chat_status` frame carries `streaming` or `ready`; a
 * `GET /v1/chats/active` row carries `streaming`, `warming` or `finished`;
 * the client adds `idle` (a slice nothing touched yet) and `failed` (a
 * warmup that failed) and derives `finished` for a row that just left the
 * live set. The store's slice never holds `finished`; the widget's rows
 * never hold `idle`, `ready` or `failed`.
 */

export const CHAT_PHASE = {
  IDLE: 'idle',
  WARMING: 'warming',
  READY: 'ready',
  STREAMING: 'streaming',
  FINISHED: 'finished',
  FAILED: 'failed',
} as const
export type ChatPhase = (typeof CHAT_PHASE)[keyof typeof CHAT_PHASE]

/** The store's slice: everything but the widget's derived `finished`. */
export type ChatStreamPhase = Exclude<ChatPhase, typeof CHAT_PHASE.FINISHED>

/** A turn is open or a session is warming — the sidebar keeps the chat in
 * its resume set and the widget in its live rows. */
export type LiveChatPhase = Extract<ChatPhase, 'streaming' | 'warming'>

/** The Active-now widget's row phases (the REST row's words). */
export type ActiveRowPhase = Extract<ChatPhase, 'streaming' | 'warming' | 'finished'>

export function isLiveChatPhase(phase: string | null | undefined): phase is LiveChatPhase {
  return phase === CHAT_PHASE.STREAMING || phase === CHAT_PHASE.WARMING
}

/** The lane words a project board speaks (`lane_status.py`). */
export const LANE_STATUS = {
  GENERATING: 'generating',
  AWAITING_USER: 'awaiting_user',
  IDLE: 'idle',
} as const
export type LaneStatus = (typeof LANE_STATUS)[keyof typeof LANE_STATUS]

/**
 * A lane's live word from the store's phase and the polled lane status:
 * the store's WS truth beats the poll in both directions (a streaming
 * slice generates at once; a ready or failed slice retires a stale
 * `generating`), and `awaiting_user` only the poll knows, so anything else
 * passes the poll through.
 */
export function laneStatusOf(phase: ChatPhase | undefined, polled: string): string {
  if (phase === CHAT_PHASE.STREAMING) return LANE_STATUS.GENERATING
  if ((phase === CHAT_PHASE.READY || phase === CHAT_PHASE.FAILED) && polled === LANE_STATUS.GENERATING) {
    return LANE_STATUS.IDLE
  }
  return polled
}
