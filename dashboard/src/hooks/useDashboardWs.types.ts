import type {
  AbortedFrame, AudioFrame, BgAgentDoneFrame, BgAgentsCompleteFrame, BgCommandDoneFrame,
  BgCommandSpawnFrame, BgCommandsCompleteFrame, ChatHistoryFrame,
  ChatHistoryDeltaFrame, ChatMovedFrame, ChatRowsFrame,
  ContextCompactFrame, DelegateResultFrame, DelegateSpawnFrame, DocumentPreviewFrame,
  EngineSwitchedFrame, ExecutionModeChangedFrame, FileFrame, FileUpdatedFrame, GoalUpdateFrame,
  ImageGeneratingFrame,
  ImagesFrame, LimitReachedFrame, LimitWarningFrame, LivenessFrame, LiveStateFrame,
  LocationRequestFrame, McpCostFrame, MediaFailedFrame, MediaProcessingFrame, MetadataFrame,
  NotificationCountFrame, NotificationFrame, NotificationSilentFrame, PermissionPromptFrame,
  PromptRetiredFrame,
  PlanModeFrame, PlanReviewFrame, PlanStatusFrame, PreWarmupReadyFrame, QuestionFrame, ShareInboxFrame,
  QueueRemovedFrame, QueueSentFrame, QueuedFrame, SatelliteUpdateFailedFrame,
  SatelliteUpdatedFrame, SatelliteUpdatingFrame, SteeredFrame, SwitchEngineDeniedFrame,
  SystemFrame, TaskSpawnFrame, ThinkingFrame, TitleUpdatedFrame, TodoUpdateFrame,
  ToolEndFrame, ToolInfoFrame, ToolResultFrame, ToolStartFrame, TurnCompleteFrame, UrlFrame,
  VideoFrame, WarmupFailedFrame, WarmupReadyFrame, WarmupStartedFrame, WorkflowEndFrame,
  WorkflowProgressFrame, WorkflowStartFrame,
} from '../api/wireEvents'

// The payload shapes live in the wire catalogue (api/wireEvents.ts); this
// file is the callback object the socket hook dispatches into. A callback
// receives the frame it is named after, minus nothing: consumers read the
// fields they need.
export type { ThreadGoal } from '../api/wireEvents'

export interface WsCallbacks {
  /**
   * The chat this consumer is currently SHOWING (null = brand-new chat, no id
   * minted yet; undefined = consumer opts out of filtering). Stream frames
   * that positively identify a different chat are dropped before dispatch —
   * background chats keep generating server-side and their frames must not
   * render into this view.
   */
  viewedChatId?: string | null
  /** True while the viewed chat renders a LIVE interactive PTY. Gates the
   *  chat_status auto-attach: a legit mid-turn "streaming" broadcast for a
   *  PTY chat must not trigger resume_chat — the resume replays history +
   *  re-attaches the PTY viewer, visibly reloading the terminal. */
  isViewedChatPtyLive?: () => boolean
  onText?: (content: string) => void
  onThinking?: (data: ThinkingFrame) => void
  onToolStart?: (data: ToolStartFrame) => void
  onToolInfo?: (data: ToolInfoFrame) => void
  onToolEnd?: (data: ToolEndFrame) => void
  onTaskSpawn?: (data: TaskSpawnFrame) => void
  onDelegateSpawn?: (data: DelegateSpawnFrame) => void
  onDelegateResult?: (data: DelegateResultFrame) => void
  // Per-subagent completion, keyed by tool_use_id (CLI: SubagentStop hook /
  // task_notification). Order-independent — no FIFO.
  onBgAgentDone?: (data: BgAgentDoneFrame) => void
  onBgCommandSpawn?: (data: BgCommandSpawnFrame) => void
  onBgCommandDone?: (data: BgCommandDoneFrame) => void
  onBgAgentsComplete?: (data: BgAgentsCompleteFrame) => void
  onBgCommandsComplete?: (data: BgCommandsCompleteFrame) => void
  onServerTurnStart?: () => void
  onFgAgentsComplete?: () => void
  // Dynamic workflows (Workflow tool) — live phase/agent tree.
  onWorkflowStart?: (data: WorkflowStartFrame) => void
  onWorkflowProgress?: (data: WorkflowProgressFrame) => void
  onWorkflowEnd?: (data: WorkflowEndFrame) => void
  onPermissionPrompt?: (data: PermissionPromptFrame) => void
  // A prompt card whose wait ended with no answer: drop it by request_id.
  onPromptRetired?: (data: PromptRetiredFrame) => void
  onLocationRequest?: (data: LocationRequestFrame) => void
  onPlanMode?: (data: PlanModeFrame) => void
  onPlanReview?: (data: PlanReviewFrame) => void
  onSystem?: (data: SystemFrame) => void
  // cost_billed false = subscription / local-model turn (cost hidden); missing = shown
  onMetadata?: (data: MetadataFrame) => void
  onDone?: () => void
  /** `ending` is set when the engine stopped the turn (a decline, a usage limit). */
  onError?: (message: string, ending?: { reason: string; resets_at?: string }) => void
  onImages?: (data: ImagesFrame) => void
  onImageGenerating?: (data: ImageGeneratingFrame) => void
  onMcpCost?: (data: McpCostFrame) => void
  onImageGenFailed?: () => void
  onUrl?: (data: UrlFrame) => void
  onFile?: (data: FileFrame) => void
  onVideo?: (data: VideoFrame) => void
  onAudio?: (data: AudioFrame) => void
  onMediaProcessing?: (data: MediaProcessingFrame) => void
  onMediaFailed?: (data: MediaFailedFrame) => void
  onDocumentPreview?: (data: DocumentPreviewFrame) => void
  // A shared workspace file changed on the server (a Collabora
  // save or an agent/disk write). Also fanned out to the lib/fileUpdates bus for
  // any open Collabora preview; this callback lets the host invalidate the
  // workspace file-tree query.
  onFileUpdated?: (data: FileUpdatedFrame) => void
  onWarmupReady?: (data: WarmupReadyFrame) => void
  // Warmup lifecycle events — chat-level. All carry chat_id so the
  // dispatcher routes into the per-chat store regardless of which chat is
  // on screen. The store survives chat navigation so the sidebar badge
  // stays amber while the user reads another chat. See chatStore.ts.
  // (MCP install progress lives in installStore — see installStore.ts.)
  onWarmupStarted?: (data: WarmupStartedFrame) => void
  onWarmupFailed?: (data: WarmupFailedFrame) => void
  // move_chat ack — the connection's OPEN chat was rebound to the agent's
  // current target (session cleared server-side). The consumer re-resumes
  // the chat so the fresh warmup runs there and the "moved" history card
  // arrives. Failures come as standard error frames (toasts), not this.
  onChatMoved?: (data: ChatMovedFrame) => void
  // switch_engine ack — the chat now runs on a different execution engine
  // (cross-engine resume). Arrives on the acting socket; the per-user
  // broadcast for sibling tabs is not delivered today (a carried defect,
  // see ws/dashboard_server_events.py) — handlers stay idempotent.
  onEngineSwitched?: (data: EngineSwitchedFrame) => void
  // switch_engine refusal — dedicated frame (the generic error rail only
  // renders into an open stream bubble, invisible on idle dead chats).
  // process_alive, when present, resyncs the client's liveness belief.
  onSwitchEngineDenied?: (data: SwitchEngineDeniedFrame) => void
  // probe_liveness answer — lazy process_alive refresh for the bound chat.
  onLiveness?: (data: LivenessFrame) => void
  // Auto-update: satellite lifecycle events broadcast to every
  // dashboard with access to the affected machine. Dispatched into
  // machineUpdateStore so the banner survives chat navigation.
  onSatelliteUpdating?: (data: SatelliteUpdatingFrame) => void
  onSatelliteUpdated?: (data: SatelliteUpdatedFrame) => void
  onSatelliteUpdateFailed?: (data: SatelliteUpdateFailedFrame) => void
  onPreWarmupReady?: (data: PreWarmupReadyFrame) => void
  onModeChanged?: (mode: string) => void
  onExecutionModeChanged?: (data: ExecutionModeChangedFrame) => void
  onModelChanged?: (model: string) => void
  onChatHistory?: (data: ChatHistoryFrame) => void
  onChatHistoryDelta?: (data: ChatHistoryDeltaFrame) => void
  onQueued?: (data: QueuedFrame) => void
  onQueueRemoved?: (data: QueueRemovedFrame) => void
  onQueueSent?: (data: QueueSentFrame) => void
  onSteered?: (data: SteeredFrame) => void
  onQuestion?: (data: QuestionFrame) => void
  onToolResult?: (data: ToolResultFrame) => void
  onTitleUpdated?: (data: TitleUpdatedFrame) => void
  /** New history rows persisted for an interactive chat (transcript tail
   *  batch) — refresh an open rich-history view. */
  onChatRows?: (data: ChatRowsFrame) => void
  onAborted?: (data: AbortedFrame) => void
  onQueueEditReturn?: (data: QueueRemovedFrame) => void
  onLimitWarning?: (data: LimitWarningFrame) => void
  onLimitReached?: (data: LimitReachedFrame) => void
  onPlanStatus?: (data: PlanStatusFrame) => void
  onLiveState?: (data: LiveStateFrame) => void
  onTodoUpdate?: (data: TodoUpdateFrame) => void
  onGoalUpdate?: (data: GoalUpdateFrame) => void
  onContextCompact?: (data: ContextCompactFrame) => void
  onNotification?: (data: NotificationFrame) => void
  // Silent inbox/badge update — fires on connected-but-inactive WS so the inbox stays in
  // sync while the user is on another tab or in the background. No toast, no sound.
  onNotificationSilent?: (data: NotificationSilentFrame) => void
  onNotificationCount?: (data: NotificationCountFrame) => void
  // "Shared with you" changed for this person (SHARING.md): the pending
  // count and the agents whose Apps panel gained or lost a placed app.
  onShareInbox?: (data: ShareInboxFrame) => void
  onTurnComplete?: (data: TurnCompleteFrame) => void
}
