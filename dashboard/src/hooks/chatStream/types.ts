/** Per-page adapter for the queued-messages store (chatStore for AgentChat,
 * local useState elsewhere). The hook only writes the queue; the page
 * owns the read model + the cancel/edit handlers. */
export interface ChatStreamQueueAdapter {
  addQueued: (index: number, text: string) => void
  clearQueued: () => void
}

export interface UseChatStreamOptions {
  /** useAgents() data — delegate identity lookup (agent display_name + color). */
  agents: Array<{ name: string; display_name?: string; color?: string }> | undefined
  /** Initial permission mode: 'default' (chat) | 'auto' (task). */
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

  // --- Warmup / error tails (reference page-owned state only) ---
  /** Fires on warmup_ready BEFORE the stale-chat guard (chat: refetchChats). */
  onWarmupRefetch?: () => void
  /** Page tail after the common warmup_ready apply (chat: lock layer +
   * setWarmingUp(false) + navigate; task: setNeedsWarmup). */
  onWarmupReadyExtra?: (data: any) => void
  /** Page tail inside warmup_started, after setChatId (chat: refetchChats). */
  onWarmupStartedExtra?: (data: any) => void
  /** pre_warmup_ready tail (chat: stash preWarmedRef). */
  onPreWarmupReady?: (data: any) => void
  /** Whether a warmup is currently in flight — drives onError's optimistic
   * bubble removal (chat: warmingUp state; task: false). */
  isWarmingUp?: boolean
  /** onError page tail when isWarmingUp (chat: reset warmup state + pending refs). */
  onWarmupReset?: () => void
  /** onError page tail, always (task: setChatReady(true)). */
  onErrorExtra?: () => void
  /** onWarmupFailed page tail (chat: reset warmup state; task: setChatReady(true)). */
  onWarmupFailedReset?: () => void
  /** On warmup_failed with an empty message list, append a standalone error
   * message (task: true — the run may have no optimistic bubble). chat leaves
   * the empty chat untouched (false/omitted). */
  appendErrorOnEmptyWarmupFail?: boolean
  /** title_updated tail (chat: refetchChats). */
  onTitleUpdated?: () => void
  /** move_chat ack for the VIEWED chat (the hook drops acks for any other) —
   * the page re-resumes it so the fresh warmup runs on the new target and
   * the "moved" history card arrives. */
  onChatMoved?: (data: { chat_id: string; new_target: string; resolved_label?: string }) => void
  /** New interactive-history rows persisted (transcript tail batch) — pages
   *  use it to live-refresh an open rich-history (transcript) view. */
  onChatRows?: (data: { chat_id: string; agent?: string }) => void

  // --- Chat-history tails ---
  /** Early restore from chat_history that the hook doesn't own (chat: restore
   * chatActiveLayer + model from execution_path). */
  onChatHistoryMeta?: (data: any) => void
  /** Fires after chat_history is applied (chat: deferred find-bar; task:
   * setChatReady(true)). */
  onChatHistoryLoaded?: (data: any) => void

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
  onNotification?: (data: any) => void
  onNotificationSilent?: (data: any) => void
  onNotificationCount?: (data: any) => void
}
