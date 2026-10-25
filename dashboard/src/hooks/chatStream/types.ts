import type { AttachedFileMeta, AttachedImageMeta, QueuedMessage } from '../../store/types'
import type {
  ChatHistoryDeltaFrame, ChatHistoryFrame, ChatMovedFrame, NotificationCountFrame, NotificationFrame, NotificationSilentFrame,
  PreWarmupReadyFrame, WarmupFailedFrame, WarmupReadyFrame, WarmupStartedFrame,
} from '../../api/wireEvents'

/** Per-page adapter for the queued-messages store (chatStore for AgentChat,
 * local useState elsewhere). The hook only writes the queue; the page
 * owns the read model + the cancel/edit handlers. */
export interface ChatStreamQueueAdapter {
  addQueued: (index: number, item: QueuedMessage) => void
  clearQueued: () => void
  /** A cancelled queued message hands its attachments back to the composer
   * (chat page only; a task view has no composer attachments). */
  restoreAttachments?: (images: AttachedImageMeta[], files: AttachedFileMeta[]) => void
}

export interface UseChatStreamOptions {
  /** The place picked for the new chat on `agent` (sent with its first frames). */
  newChatTarget?: (agent: string) => string | undefined
  /** useAgents() data — delegate identity lookup (agent display_name + color). */
  agents: Array<{ name: string; display_name?: string; color?: string }> | undefined
  /** Initial permission mode: Auto (a chat) | 'auto' (a task, shown as Don't Ask). */
  defaultMode?: string
  /** Seed for the owned chatId state. chat: urlChatId; task: run.chat_id. */
  initialChatId?: string | null
  /** Model to fall back to in onWarmupReady when the payload has none.
   * chat: agent default model; task: undefined (only set when present). */
  fallbackModel?: string
  /** Queue write adapter (see ChatStreamQueueAdapter). */
  queue: ChatStreamQueueAdapter
  /** Clear the local queue inside finalizeAbortedTurn. task: true (local
   * useState needs an explicit reset); chat: false (chatStore is reconciled
   * by the backend's queue deltas). */
  clearQueueOnAbort?: boolean
  /** Route heavy non-visible work (currently the defensive end-of-turn
   * history refetch — a full message-list remount) through the page's
   * speech-activity deferral so it can't hard-cut live TTS. Must run the
   * callback synchronously when nothing is speaking. Omitted → immediate. */
  deferHeavy?: (fn: () => void) => void

  // --- Warmup / error tails (reference page-owned state only) ---
  /** Fires on warmup_ready BEFORE the stale-chat guard (chat: refetchChats). */
  onWarmupRefetch?: () => void
  /** Page tail after the common warmup_ready apply (chat: lock layer +
   * setWarmingUp(false) + navigate; task: setNeedsWarmup). */
  onWarmupReadyExtra?: (data: WarmupReadyFrame) => void
  /** Page tail inside warmup_started, after setChatId (chat: refetchChats). */
  onWarmupStartedExtra?: (data: WarmupStartedFrame) => void
  /** pre_warmup_ready tail (chat: stash preWarmedRef). */
  onPreWarmupReady?: (data: PreWarmupReadyFrame) => void
  /** Whether a warmup is currently in flight — drives onError's optimistic
   * bubble removal (chat: warmingUp state; task: false). */
  isWarmingUp?: boolean
  /** onError page tail when isWarmingUp (chat: reset warmup state + pending refs). */
  onWarmupReset?: () => void
  /** onError page tail, always (task: setChatReady(true)). */
  onErrorExtra?: (message: string) => void
  /** onWarmupFailed page tail (chat: reset warmup state; task: setChatReady(true)). */
  onWarmupFailedReset?: (data: WarmupFailedFrame) => void
  /** On warmup_failed with an empty message list, append a standalone error
   * message (task: true — the run may have no optimistic bubble). chat leaves
   * the empty chat untouched (false/omitted). */
  appendErrorOnEmptyWarmupFail?: boolean
  /** title_updated tail (chat: refetchChats). */
  onTitleUpdated?: () => void
  /** chat_moved for the VIEWED chat (the hook drops frames for any other):
   * a move's outcome, broadcast to every tab that sees the chat. The page
   * re-resumes it so the next warmup runs where it went. */
  onChatMoved?: (data: ChatMovedFrame) => void
  /** chat_view_only for the VIEWED chat: a phone call drives it (read-only
   * history until the call ends); null when the chat is its own again. */
  onChatViewOnly?: (data: { chat_id: string; kind: string } | null) => void
  /** switch_engine ack for the VIEWED chat (stale-guarded like onChatMoved).
   * Arrives on the acting socket AND via per-user broadcast — idempotent. */
  onEngineSwitched?: (data: { chat_id: string; execution_path: string; model: string }) => void
  /** switch_engine refusal for the VIEWED chat — rendered inside the switch
   * dialog (the generic error rail is invisible on idle dead chats). */
  onSwitchEngineDenied?: (data: { chat_id: string; message: string; process_alive?: boolean }) => void
  /** probe_liveness answer for the VIEWED chat — lazy process_alive refresh. */
  onLiveness?: (data: { chat_id: string; process_alive: boolean }) => void
  /** New interactive-history rows persisted (transcript tail batch) — pages
   *  use it to live-refresh an open rich-history (transcript) view. */
  onChatRows?: (data: { chat_id: string; agent?: string }) => void

  // --- Chat-history tails ---
  /** Early restore from chat_history that the hook doesn't own (chat: restore
   * chatActiveLayer + model from execution_path). */
  onChatHistoryMeta?: (data: ChatHistoryFrame | ChatHistoryDeltaFrame) => void
  /** The viewed chat's stored execution mode: the ack of a terminal pick or
   *  the echo of a refused one. */
  onExecutionModeChanged?: (executionMode: string) => void
  /** Fires after chat_history is applied (chat: deferred find-bar; task:
   * setChatReady(true)). */
  onChatHistoryLoaded?: (data: ChatHistoryFrame) => void

  // --- Turn-done tails ---
  /** Fires at turn end with the ping gate inputs (chat: play ping when not in
   * a meeting and no bg subagent is running, then refetchChats). */
  onTurnDone?: (info: { meetingActive: boolean; bgStillRunning: boolean }) => void
  /** Origin-routed end-of-turn ping for a hidden tab or a background chat.
   *  The visible viewed chat is onTurnDone's job — never both (the hook drops
   *  the frame when this view is showing the chat in a visible tab). */
  onTurnComplete?: (data: { chat_id?: string; title?: string; body?: string }) => void
  /** Schedule the 400ms defensive resume_chat when the bubble has no text
   * (chat: true). */
  enableDefensiveRefetch?: boolean
  /** True while the viewed chat renders a live interactive PTY — forwarded to
   *  useDashboardWs to suppress the chat_status auto-attach (a resume would
   *  reload the terminal view). */
  isViewedChatPtyLive?: () => boolean

  // --- Notifications (page-owned; AgentChat / useChatNotifications) ---
  onNotification?: (data: NotificationFrame) => void
  onNotificationSilent?: (data: NotificationSilentFrame) => void
  onNotificationCount?: (data: NotificationCountFrame) => void
}
