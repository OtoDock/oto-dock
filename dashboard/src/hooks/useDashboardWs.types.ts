// Codex per-thread long-running goal (GOAL_UPDATE / restore.goal / live_state.goal).
export interface ThreadGoal {
  objective: string
  // "active" | "complete" | "paused" | "usageLimited" | "budgetLimited";
  // absent on rows persisted before the field existed (treated as active).
  status?: string
  token_budget: number | null
  tokens_used: number
  time_used_seconds: number
}

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
  onThinking?: (data: { phase?: string; text?: string; estimated_tokens?: number }) => void
  onToolStart?: (data: { name: string; tool_id?: string }) => void
  onToolInfo?: (data: { name: string; summary?: string; tool_input?: any }) => void
  onToolEnd?: (data: { name: string; tool_id?: string }) => void
  onTaskSpawn?: (data: { description: string; subagent_type?: string; run_in_background?: boolean; tool_use_id?: string; tool_input?: any }) => void
  onDelegateSpawn?: (data: { task_id?: string; task_name: string; agent: string; prompt_preview: string; prompt?: string; chat_id?: string }) => void
  onDelegateResult?: (data: { task_id?: string; task_name: string; agent?: string; output_text?: string; status?: string }) => void
  // Per-subagent completion, keyed by tool_use_id (CLI: SubagentStop hook /
  // task_notification). Order-independent — no FIFO.
  onBgAgentDone?: (data: { tool_use_id?: string }) => void
  onBgCommandSpawn?: (data: { tool_use_id?: string; command?: string; description?: string }) => void
  onBgCommandDone?: (data: { tool_use_id?: string; status?: string }) => void
  onBgAgentsComplete?: (data: { count: number }) => void
  onBgCommandsComplete?: (data: { count: number }) => void
  onServerTurnStart?: () => void
  onFgAgentsComplete?: () => void
  // Dynamic workflows (Workflow tool) — live phase/agent tree.
  onWorkflowStart?: (data: { tool_use_id: string; workflow_name?: string }) => void
  onWorkflowProgress?: (data: { tool_use_id: string; workflow_progress: any[] }) => void
  onWorkflowEnd?: (data: { tool_use_id: string }) => void
  onPermissionPrompt?: (data: {
    request_id: string
    tool_name: string
    tool_input: any
    description?: string
    meeting_agent?: string
  }) => void
  onLocationRequest?: (data: { request_id: string }) => void
  onPlanMode?: (data: { action: string; tool_input?: any }) => void
  onPlanReview?: (data: { request_id: string; plan: string; tool_input: any; filename?: string }) => void
  onSystem?: (data: { subtype: string; message?: string; agent?: string; agent_display_name?: string; agent_color?: string; round?: number; participants?: any[]; max_rounds?: number; max_turns?: number; meeting_id?: string }) => void
  onMetadata?: (data: { cost_usd?: number; duration_ms?: number; duration_api_ms?: number; context_used?: number; context_max?: number; cache_read?: number; cache_write?: number; input_tokens?: number; output_tokens?: number }) => void
  onDone?: () => void
  onError?: (message: string) => void
  onImages?: (data: { images: Array<{ url?: string; image_data?: string; mime_type?: string; caption?: string; attribution?: string; link_url?: string; download_url?: string }> }) => void
  onImageGenerating?: (data: { prompt_preview: string; model: string }) => void
  onMcpCost?: (data: { cost_usd: number; provider: string; model: string; tool: string; mcp: string }) => void
  onImageGenFailed?: () => void
  onUrl?: (data: { url: string; title: string; description: string }) => void
  onFile?: (data: { filename: string; download_url: string; description: string }) => void
  onVideo?: (data: { src_kind?: string; url?: string; media_url?: string; token?: string; mime?: string; caption?: string; title?: string; poster?: string }) => void
  onAudio?: (data: { src_kind?: string; url?: string; media_url?: string; token?: string; mime?: string; caption?: string; title?: string }) => void
  onMediaProcessing?: (data: { media_kind?: string; caption?: string }) => void
  onMediaFailed?: (data: { error?: string }) => void
  onDocumentPreview?: (data: { wopi_url: string; filename: string; file_id: string; download_url: string; snapshot_id?: string; generation?: number }) => void
  // A shared workspace file changed on the server (a Collabora
  // save or an agent/disk write). Also fanned out to the lib/fileUpdates bus for
  // any open Collabora preview; this callback lets the host invalidate the
  // workspace file-tree query.
  onFileUpdated?: (data: { agent_slug: string; rel_path: string; file_id?: string; source?: string }) => void
  onWarmupReady?: (data: {
    session_id: string | null
    chat_id: string
    mode: string
    model?: string
    needs_warmup?: boolean
    execution_path?: string
    execution_target?: string
    // Interactive CLI: true when a live PTY-backed TUI
    // session exists → mount the terminal. execution_mode is the chat's stored
    // override ('interactive' | '-p' | '') for the toggle state.
    interactive?: boolean
    execution_mode?: string
    fallback_reason?: string | null
    // Populated only when fallback_reason === 'user-override-offline' —
    // the human-readable name of the user's offline target machine so
    // the dashboard can render the brief soft-fallback banner.
    offline_machine_name?: string
    // Pin-vs-current-target mismatch. Present ONLY when the open chat is
    // pinned to an execution target different from the agent's currently-
    // resolved one AND the viewer is the chat owner/admin. Absent = no
    // mismatch — any previously stored mismatch for the chat is cleared
    // (the fresh warmup after a successful move carries no fields).
    // Drives the ChatTargetBanner + the sidebar kebab's move row.
    pinned_target?: string
    pinned_label?: string
    resolved_target?: string
    resolved_label?: string
  }) => void
  // Warmup lifecycle events — chat-level. All carry chat_id so the
  // dispatcher routes into the per-chat store regardless of which chat is
  // on screen. The store survives chat navigation so the sidebar badge
  // stays amber while the user reads another chat. See chatStore.ts.
  // (MCP install progress lives in installStore — see installStore.ts.)
  onWarmupStarted?: (data: {
    chat_id: string
    agent: string
    execution_path?: string
    execution_target?: string
  }) => void
  onWarmupFailed?: (data: { chat_id: string; error: string }) => void
  // move_chat ack — the connection's OPEN chat was rebound to the agent's
  // current target (session cleared server-side). The consumer re-resumes
  // the chat so the fresh warmup runs there and the "moved" history card
  // arrives. Failures come as standard error frames (toasts), not this.
  onChatMoved?: (data: { chat_id: string; new_target: string; resolved_label?: string }) => void
  // Auto-update: satellite lifecycle events broadcast to every
  // dashboard with access to the affected machine. Dispatched into
  // machineUpdateStore so the banner survives chat navigation.
  onSatelliteUpdating?: (data: {
    machine_id: string
    machine_name?: string
    from_version?: string
    to_version: string
    started_at?: string
  }) => void
  onSatelliteUpdated?: (data: {
    machine_id: string
    machine_name?: string
    version: string
  }) => void
  onSatelliteUpdateFailed?: (data: {
    machine_id: string
    machine_name?: string
    error: string
    rolled_back_to?: string
  }) => void
  onPreWarmupReady?: (data: { session_id: string }) => void
  onModeChanged?: (mode: string) => void
  onModelChanged?: (model: string) => void
  onThinkingChanged?: (max_tokens: number | null) => void
  onChatHistory?: (data: { chat_id?: string; messages: any[]; has_more?: boolean; restore?: { todos?: any[]; meeting?: { active?: boolean; participants?: any[]; max_turns?: number } | null; goal?: ThreadGoal | null }; plans?: any[]; total_cost?: number; context_used?: number; context_max?: number; cache_read?: number; cache_write?: number; output_tokens?: number; execution_path?: string; execution_mode?: string; model?: string }) => void
  onQueued?: (data: { index: number; text: string }) => void
  onQueueRemoved?: (data: { index: number }) => void
  onQueueSent?: (data: { text: string }) => void
  onSteered?: (data: { text: string }) => void
  onQuestion?: (data: { tool_name: string; tool_input: any; request_id?: string }) => void
  onToolResult?: (data: { tool_name: string; tool_use_id?: string; summary: string; result_content?: string }) => void
  onTitleUpdated?: (data: { chat_id: string; title: string }) => void
  /** New history rows persisted for an interactive chat (transcript tail
   *  batch) — refresh an open rich-history view. */
  onChatRows?: (data: { chat_id: string; agent?: string }) => void
  onAborted?: (data: { session_id?: string }) => void
  onQueueEditReturn?: (data: { index: number; text: string }) => void
  onUserMessage?: (content: string) => void
  onLimitWarning?: (data: { monthly?: any; weekly?: any }) => void
  onLimitReached?: (data: { monthly?: any; weekly?: any }) => void
  onPlanStatus?: (data: { filename: string; status: string }) => void
  onLiveState?: (data: {
    streaming: boolean
    session_id: string
    started_at: number
    live_blocks: any[]
    active_tools: any[]
    active_agents: any[]
    active_delegates: any[]
    pending_permission: any | null
    thinking_active?: boolean
    thinking_text?: string
    thinking_tokens?: number
    todos?: Array<{ content: string; status: string; activeForm?: string }>
    goal?: ThreadGoal | null
    workflows?: Record<string, { tool_use_id: string; workflow_name?: string; progress?: any[]; active?: boolean }>
    meeting_agent?: string | null
    meeting_participants?: Array<{ slug: string; display_name: string; color: string }>
  }) => void
  onTodoUpdate?: (data: { todos: Array<{ content: string; status: string; activeForm?: string }> }) => void
  onGoalUpdate?: (data: { goal: ThreadGoal | null }) => void
  onContextCompact?: (data: { phase: string; trigger?: string; pre_tokens?: number; post_tokens?: number; context_max?: number; messages_summarized?: number }) => void
  onNotification?: (data: { delivery: any }) => void
  // Silent inbox/badge update — fires on connected-but-inactive WS so the inbox stays in
  // sync while the user is on another tab or in the background. No toast, no sound.
  onNotificationSilent?: (data: { delivery: any }) => void
  onNotificationCount?: (data: { count: number }) => void
  onTurnComplete?: (data: { chat_id?: string; title?: string; body?: string }) => void
}
