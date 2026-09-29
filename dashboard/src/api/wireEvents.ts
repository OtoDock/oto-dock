/**
 * The wire catalogue — the TypeScript mirror of the proxy's
 * `ws/wire_events.py`: every frame the dashboard socket carries, named
 * once. The proxy-side test `tests/session/test_wire_events.py` reads this
 * file and keeps `WIRE`, `FRAMES`, `PERSISTED`, `LIVE_BLOCK` and `OUT`
 * equal to the Python; the acceptance test refuses a frame name spelled as
 * a literal anywhere else in the dashboard. The payload interfaces are
 * hand-kept on both sides. The artifact kinds' behaviour (what a frame
 * evicts, replaces, persists, replays) lives in `lib/kinds/artifact.ts`.
 *
 * Every string here is FROZEN — a frame name, a payload key and a
 * persisted `event_type` are read by every bundle ever served, by the phone
 * daemon, by the share host and by every `chat_messages` row. A new frame
 * is one `WIRE` entry, one `FRAMES` row, one payload interface, one union
 * member and one receiver case; a renamed one is a wire change.
 */

import type { LimitPayload } from './usage'

/** The outbound frames (proxy → dashboard). */
export const WIRE = {
  // The turn stream.
  TEXT: 'text',
  THINKING: 'thinking',
  TOOL_START: 'tool_start',
  TOOL_INFO: 'tool_info',
  TOOL_END: 'tool_end',
  TOOL_RESULT: 'tool_result',
  TASK_SPAWN: 'task_spawn',
  BG_AGENT_DONE: 'bg_agent_done',
  BG_COMMAND_SPAWN: 'bg_command_spawn',
  BG_COMMAND_DONE: 'bg_command_done',
  BG_AGENTS_COMPLETE: 'bg_agents_complete',
  BG_COMMANDS_COMPLETE: 'bg_commands_complete',
  FG_AGENTS_COMPLETE: 'fg_agents_complete',
  WORKFLOW_START: 'workflow_start',
  WORKFLOW_PROGRESS: 'workflow_progress',
  WORKFLOW_END: 'workflow_end',
  DELEGATE_SPAWN: 'delegate_spawn',
  DELEGATE_RESULT: 'delegate_result',
  CHECK_VERDICT: 'check_verdict',
  PERMISSION_PROMPT: 'permission_prompt',
  PLAN_REVIEW: 'plan_review',
  QUESTION: 'question',
  LOCATION_REQUEST: 'location_request',
  PLAN_MODE: 'plan_mode',
  SYSTEM: 'system',
  METADATA: 'metadata',
  MCP_COST: 'mcp_cost',
  TODO_UPDATE: 'todo_update',
  GOAL_UPDATE: 'goal_update',
  CONTEXT_COMPACT: 'context_compact',
  DONE: 'done',
  ERROR: 'error',
  ABORTED: 'aborted',
  LIVE_STATE: 'live_state',
  LIMIT_WARNING: 'limit_warning',
  LIMIT_REACHED: 'limit_reached',
  // The artifacts.
  IMAGES: 'images',
  IMAGE_GENERATING: 'image_generating',
  IMAGE_GEN_FAILED: 'image_gen_failed',
  URL: 'url',
  FILE: 'file',
  VIDEO: 'video',
  AUDIO: 'audio',
  MEDIA_PROCESSING: 'media_processing',
  MEDIA_FAILED: 'media_failed',
  DOCUMENT_PREVIEW: 'document_preview',
  UI: 'ui',
  ARTIFACT_INTERACTION: 'artifact_interaction',
  ARTIFACT_ACK: 'artifact_ack',
  APP_ACTION: 'app_action',
  APP_ACTION_ACK: 'app_action_ack',
  // The queue and the turn's control frames.
  QUEUED: 'queued',
  QUEUE_REMOVED: 'queue_removed',
  QUEUE_SENT: 'queue_sent',
  QUEUE_CLEARED: 'queue_cleared',
  QUEUE_SNAPSHOT: 'queue_snapshot',
  STEERED: 'steered',
  MODE_CHANGED: 'mode_changed',
  MODEL_CHANGED: 'model_changed',
  EXECUTION_MODE_CHANGED: 'execution_mode_changed',
  PLAN_STATUS: 'plan_status',
  SERVER_TURN_START: 'server_turn_start',
  // The chat and session lifecycle.
  WARMUP_STARTED: 'warmup_started',
  WARMUP_HEARTBEAT: 'warmup_heartbeat',
  WARMUP_READY: 'warmup_ready',
  WARMUP_FAILED: 'warmup_failed',
  PRE_WARMUP_READY: 'pre_warmup_ready',
  CHAT_HISTORY: 'chat_history',
  CHAT_STATUS: 'chat_status',
  CHAT_STATUS_SNAPSHOT: 'chat_status_snapshot',
  CHAT_READ: 'chat_read',
  CHAT_ROWS: 'chat_rows',
  CHAT_META: 'chat_meta',
  CHAT_MOVED: 'chat_moved',
  ENGINE_SWITCHED: 'engine_switched',
  SWITCH_ENGINE_DENIED: 'switch_engine_denied',
  LIVENESS: 'liveness',
  TITLE_UPDATED: 'title_updated',
  TURN_COMPLETE: 'turn_complete',
  // The interactive terminal.
  PTY_OUTPUT: 'pty_output',
  PTY_EXIT: 'pty_exit',
  PTY_PERMISSION: 'pty_permission',
  PTY_ARTIFACT: 'pty_artifact',
  PTY_STATUS: 'pty_status',
  // The user's other screens.
  NOTIFICATION: 'notification',
  NOTIFICATION_SILENT: 'notification_silent',
  NOTIFICATION_COUNT: 'notification_count',
  FILE_UPDATED: 'file_updated',
  APP_PUSH: 'app_push',
  APP_STATE: 'app_state',
  OPEN_APP: 'open_app',
  APP_DEPLOYED: 'app_deployed',
  CATALOG: 'catalog',
  INSTALL_STARTED: 'install_started',
  INSTALL_MCP_PLAN: 'install_mcp_plan',
  INSTALL_PROGRESS: 'install_progress',
  INSTALL_HEARTBEAT: 'install_heartbeat',
  INSTALL_VERIFYING: 'install_verifying',
  INSTALL_DONE: 'install_done',
  INSTALL_FAILED: 'install_failed',
  MCP_INSTALL_FAILED: 'mcp_install_failed',
  TRANSFER_STARTED: 'transfer_started',
  TRANSFER_MACHINE_STATE: 'transfer_machine_state',
  TRANSFER_PROGRESS: 'transfer_progress',
  TRANSFER_DONE: 'transfer_done',
  TRANSFER_STATE: 'transfer_state',
  SATELLITE_UPDATING: 'satellite_updating',
  SATELLITE_UPDATED: 'satellite_updated',
  SATELLITE_UPDATE_FAILED: 'satellite_update_failed',
  SATELLITE_UPDATE_SYNC: 'satellite_update_sync',
  // The connection itself.
  SERVER_INFO: 'server_info',
  PONG: 'pong',
} as const

export type WireType = (typeof WIRE)[keyof typeof WIRE]

/** The persisted row kinds (`chat_messages.event_type`) that differ from a
 *  frame name, plus the rows the server writes without a frame. */
export const PERSISTED = {
  TOOL: 'tool',
  BG_NUDGE: 'bg_nudge',
  BG_COMMAND_NUDGE: 'bg_command_nudge',
  SCHEDULE_WAKE: 'schedule_wake',
} as const

/** The live-block kinds (`live_state.live_blocks[].type`). */
export const LIVE_BLOCK = {
  TEXT: WIRE.TEXT,
  THINKING: WIRE.THINKING,
  TOOL: PERSISTED.TOOL,
  AGENT: 'agent',
  COMMAND: 'command',
  DELEGATE: 'delegate',
} as const

export interface FrameMeta {
  /** The frame belongs to ONE chat's stream: a `chat_id` naming another
   *  chat drops it before dispatch (an untagged frame passes). */
  perChat: boolean
  /** The `event_type` the frame's block is stored under, or null when the
   *  frame is live-only (`text` is stored as an assistant row). */
  persisted: string | null
}

export const FRAMES: Record<WireType, FrameMeta> = {
  text: { perChat: true, persisted: null },
  thinking: { perChat: true, persisted: 'thinking' },
  tool_start: { perChat: true, persisted: 'tool' },
  tool_info: { perChat: true, persisted: 'tool' },
  tool_end: { perChat: true, persisted: 'tool' },
  tool_result: { perChat: true, persisted: 'tool' },
  task_spawn: { perChat: true, persisted: 'task_spawn' },
  bg_agent_done: { perChat: true, persisted: null },
  bg_command_spawn: { perChat: true, persisted: 'bg_command_spawn' },
  bg_command_done: { perChat: true, persisted: null },
  bg_agents_complete: { perChat: true, persisted: null },
  bg_commands_complete: { perChat: true, persisted: null },
  fg_agents_complete: { perChat: true, persisted: null },   // minted by the liveness clear alone
  workflow_start: { perChat: true, persisted: null },
  workflow_progress: { perChat: true, persisted: null },
  workflow_end: { perChat: true, persisted: null },
  delegate_spawn: { perChat: true, persisted: 'delegate_spawn' },
  delegate_result: { perChat: true, persisted: 'delegate_result' },
  check_verdict: { perChat: false, persisted: 'check_verdict' },
  permission_prompt: { perChat: true, persisted: 'permission_prompt' },
  plan_review: { perChat: true, persisted: 'plan_review' },
  question: { perChat: true, persisted: 'question' },
  location_request: { perChat: true, persisted: null },
  plan_mode: { perChat: true, persisted: 'plan_mode' },
  system: { perChat: true, persisted: 'system' },
  metadata: { perChat: true, persisted: 'metadata' },
  mcp_cost: { perChat: true, persisted: null },
  todo_update: { perChat: true, persisted: null },
  goal_update: { perChat: true, persisted: null },
  context_compact: { perChat: true, persisted: 'context_compact' },
  done: { perChat: true, persisted: null },
  error: { perChat: true, persisted: null },
  aborted: { perChat: true, persisted: null },
  live_state: { perChat: true, persisted: null },
  limit_warning: { perChat: true, persisted: null },
  limit_reached: { perChat: true, persisted: null },
  images: { perChat: true, persisted: 'images' },
  image_generating: { perChat: true, persisted: 'image_generating' },   // a turn ending mid-generation freezes the placeholder
  image_gen_failed: { perChat: true, persisted: null },
  url: { perChat: true, persisted: 'url' },
  file: { perChat: true, persisted: 'file' },
  video: { perChat: true, persisted: 'video' },
  audio: { perChat: true, persisted: 'audio' },
  media_processing: { perChat: true, persisted: null },
  media_failed: { perChat: true, persisted: null },
  document_preview: { perChat: true, persisted: 'document_preview' },
  ui: { perChat: false, persisted: 'ui' },
  artifact_interaction: { perChat: false, persisted: 'artifact_interaction' },
  artifact_ack: { perChat: false, persisted: null },
  app_action: { perChat: false, persisted: 'app_action' },
  app_action_ack: { perChat: false, persisted: null },
  queued: { perChat: true, persisted: null },
  queue_removed: { perChat: true, persisted: null },
  queue_sent: { perChat: true, persisted: null },
  queue_cleared: { perChat: true, persisted: null },
  queue_snapshot: { perChat: true, persisted: null },
  steered: { perChat: true, persisted: null },
  mode_changed: { perChat: true, persisted: null },
  model_changed: { perChat: true, persisted: null },
  execution_mode_changed: { perChat: false, persisted: null },
  plan_status: { perChat: false, persisted: null },
  server_turn_start: { perChat: false, persisted: null },
  warmup_started: { perChat: false, persisted: null },
  warmup_heartbeat: { perChat: false, persisted: null },
  warmup_ready: { perChat: false, persisted: null },
  warmup_failed: { perChat: false, persisted: null },
  pre_warmup_ready: { perChat: false, persisted: null },
  chat_history: { perChat: false, persisted: null },
  chat_status: { perChat: false, persisted: null },
  chat_status_snapshot: { perChat: false, persisted: null },
  chat_read: { perChat: false, persisted: null },
  chat_rows: { perChat: true, persisted: null },
  chat_meta: { perChat: true, persisted: null },
  chat_moved: { perChat: false, persisted: null },
  engine_switched: { perChat: false, persisted: null },
  switch_engine_denied: { perChat: false, persisted: null },
  liveness: { perChat: false, persisted: null },
  title_updated: { perChat: false, persisted: null },
  turn_complete: { perChat: false, persisted: null },
  pty_output: { perChat: true, persisted: null },
  pty_exit: { perChat: true, persisted: null },
  pty_permission: { perChat: true, persisted: null },
  pty_artifact: { perChat: true, persisted: null },
  pty_status: { perChat: true, persisted: null },
  notification: { perChat: false, persisted: null },
  notification_silent: { perChat: false, persisted: null },
  notification_count: { perChat: false, persisted: null },
  file_updated: { perChat: false, persisted: null },
  app_push: { perChat: false, persisted: null },
  app_state: { perChat: false, persisted: null },
  open_app: { perChat: false, persisted: null },
  app_deployed: { perChat: false, persisted: null },
  catalog: { perChat: false, persisted: null },
  install_started: { perChat: false, persisted: null },
  install_mcp_plan: { perChat: false, persisted: null },
  install_progress: { perChat: false, persisted: null },
  install_heartbeat: { perChat: false, persisted: null },
  install_verifying: { perChat: false, persisted: null },
  install_done: { perChat: false, persisted: null },
  install_failed: { perChat: false, persisted: null },
  mcp_install_failed: { perChat: false, persisted: null },
  transfer_started: { perChat: false, persisted: null },
  transfer_machine_state: { perChat: false, persisted: null },
  transfer_progress: { perChat: false, persisted: null },
  transfer_done: { perChat: false, persisted: null },
  transfer_state: { perChat: false, persisted: null },
  satellite_updating: { perChat: false, persisted: null },
  satellite_updated: { perChat: false, persisted: null },
  satellite_update_failed: { perChat: false, persisted: null },
  satellite_update_sync: { perChat: false, persisted: null },
  server_info: { perChat: false, persisted: null },
  pong: { perChat: false, persisted: null },
}

/** Frames that belong to a single chat's stream — derived, never listed. A
 *  frame carrying a `chat_id` different from the viewed chat is dropped
 *  before dispatch; untagged frames pass. `turn_complete` is per-chat by
 *  origin but its receiver decides by chat id itself. */
export const PER_CHAT_FRAMES: ReadonlySet<string> = new Set(
  (Object.keys(FRAMES) as WireType[]).filter((k) => FRAMES[k].perChat),
)

export function isWireType(t: unknown): t is WireType {
  return typeof t === 'string' && Object.prototype.hasOwnProperty.call(FRAMES, t)
}

/** `system.subtype`: the ones the proxy mints or persists (the meeting
 *  orchestrator's seven, the reseed card, the undelivered-input card, the
 *  self-wake marker, the install progress, the tailers' compaction
 *  separator) and, marked, the ones the DASHBOARD synthesises from other
 *  frames (`warmup_failed.reason`, the two nudge rows, the live compaction).
 *  The CLI passes raw subtypes through as well; the renderer ignores what
 *  it does not know. */
export const SYSTEM_SUBTYPE = {
  SESSION_RESEEDED: 'session_reseeded',
  UNDELIVERED_INPUT: 'undelivered_input',
  BG_WAKE: 'bg_wake',
  CONTEXT_COMPRESSED: 'context_compressed',
  MEETING_STARTED: 'meeting_started',
  MEETING_TURN_START: 'meeting_turn_start',
  MEETING_TURN_END: 'meeting_turn_end',
  MEETING_CONCLUDED: 'meeting_concluded',
  MEETING_AGENT_FAILED: 'meeting_agent_failed',
  MEETING_AGENT_LEFT: 'meeting_agent_left',
  MEETING_FAILED: 'meeting_failed',
  // Dashboard-synthesised (never sent by the proxy).
  NO_SUBSCRIPTION: 'no_subscription',
  POOL_CAP: 'pool_cap',
  TARGET_UNAVAILABLE: 'target_unavailable',
  SESSION_ERROR: 'session_error',
  BG_AGENTS_COMPLETED: 'bg_agents_completed',
  BG_COMMANDS_COMPLETED: 'bg_commands_completed',
} as const

/** The kind of a held prompt (`live_state.pending_permission.event_type`,
 *  the proxy's hook-queue `ITEM_*`): what a reconnect re-renders. */
export const HOOK_ITEM = {
  PERMISSION_PROMPT: 'permission_prompt',
  PLAN_REVIEW: 'plan_review',
  QUESTION_PROMPT: 'question_prompt',
} as const

/** `warmup_failed.reason` (the proxy's `WARMUP_FAILED_REASONS`). */
export const WARMUP_FAILED_REASON = {
  AUTH_OFF: 'auth_off',
  ADMIN_OAUTH_ONLY: 'admin_oauth_only',
  NO_POOL: 'no_pool',
  NONE: 'none',
  THROTTLED: 'throttled',
  OWN_SUB_EXPIRED: 'own_sub_expired',
  POOL_CAP: 'pool_cap',
  TARGET_UNAVAILABLE: 'target_unavailable',
  SESSION_ERROR: 'session_error',
} as const

/** The inbound messages (dashboard → proxy); the proxy's `INBOUND` table
 *  names the same words. */
export const OUT = {
  PRE_WARMUP: 'pre_warmup',
  WARMUP: 'warmup',
  CHAT: 'chat',
  ARTIFACT_INTERACTION: 'artifact_interaction',
  APP_ACTION: 'app_action',
  RESUME_CHAT: 'resume_chat',
  CHAT_READ: 'chat_read',
  PERMISSION_RESPONSE: 'permission_response',
  QUESTION_RESPONSE: 'question_response',
  LOCATION_RESPONSE: 'location_response',
  PLAN_REVIEW_RESPONSE: 'plan_review_response',
  PTY_ATTACH: 'pty_attach',
  PTY_TAKEOVER: 'pty_takeover',
  PTY_INPUT: 'pty_input',
  PTY_RESIZE: 'pty_resize',
  PTY_ATTACHMENTS: 'pty_attachments',
  MODE_CHANGE: 'mode_change',
  MODEL_CHANGE: 'model_change',
  EXECUTION_MODE_CHANGE: 'execution_mode_change',
  EXECUTION_MODE_SWITCH: 'execution_mode_switch',
  COMPACT_CONTEXT: 'compact_context',
  MOVE_CHAT: 'move_chat',
  SWITCH_ENGINE: 'switch_engine',
  PROBE_LIVENESS: 'probe_liveness',
  IMPLEMENT_PLAN: 'implement_plan',
  ABORT: 'abort',
  CANCEL_QUEUED: 'cancel_queued',
  CANCEL_ALL_QUEUED: 'cancel_all_queued',
  CLIENT_INFO: 'client_info',
  USER_ACTIVE: 'user_active',
  USER_IDLE: 'user_idle',
  FOCUS: 'focus',
  CATALOG_SUBSCRIBE: 'catalog_subscribe',
  CATALOG_UNSUBSCRIBE: 'catalog_unsubscribe',
  PING: 'ping',
  CLOSE: 'close',
} as const

// ---------------------------------------------------------------------------
// The payloads — one interface per frame, the keys frozen. `chat_id` is the
// stream loop's tag on per-chat frames.
// ---------------------------------------------------------------------------

interface PerChat { chat_id?: string }

export interface TextFrame extends PerChat { type: typeof WIRE.TEXT; content: string }
export interface ThinkingFrame extends PerChat { type: typeof WIRE.THINKING; phase?: string; text?: string; estimated_tokens?: number }
export interface ToolStartFrame extends PerChat { type: typeof WIRE.TOOL_START; name: string; tool_id?: string }
export interface ToolInfoFrame extends PerChat { type: typeof WIRE.TOOL_INFO; name: string; summary?: string; tool_input?: any; file_path?: string }
export interface ToolEndFrame extends PerChat { type: typeof WIRE.TOOL_END; name: string; tool_id?: string }
export interface ToolResultFrame extends PerChat { type: typeof WIRE.TOOL_RESULT; tool_name: string; tool_use_id?: string; summary: string; result_content?: string }
export interface TaskSpawnFrame extends PerChat { type: typeof WIRE.TASK_SPAWN; description: string; subagent_type?: string; run_in_background?: boolean; tool_use_id?: string; tool_input?: any }
export interface BgAgentDoneFrame extends PerChat { type: typeof WIRE.BG_AGENT_DONE; tool_use_id?: string }
export interface BgCommandSpawnFrame extends PerChat { type: typeof WIRE.BG_COMMAND_SPAWN; tool_use_id?: string; command?: string; description?: string }
export interface BgCommandDoneFrame extends PerChat { type: typeof WIRE.BG_COMMAND_DONE; tool_use_id?: string; status?: string }
export interface BgAgentsCompleteFrame extends PerChat { type: typeof WIRE.BG_AGENTS_COMPLETE; count: number }
export interface BgCommandsCompleteFrame extends PerChat { type: typeof WIRE.BG_COMMANDS_COMPLETE; count: number }
export interface FgAgentsCompleteFrame extends PerChat { type: typeof WIRE.FG_AGENTS_COMPLETE }
export interface WorkflowStartFrame extends PerChat { type: typeof WIRE.WORKFLOW_START; tool_use_id: string; workflow_name?: string }
export interface WorkflowProgressFrame extends PerChat { type: typeof WIRE.WORKFLOW_PROGRESS; tool_use_id: string; workflow_progress: any[] }
export interface WorkflowEndFrame extends PerChat { type: typeof WIRE.WORKFLOW_END; tool_use_id: string }
/** `chat_id` on the persisted row is the WORKER chat; on the live frame the
 *  stream loop's tag overwrites it (a payload collision, deferred). */
export interface DelegateSpawnFrame extends PerChat { type: typeof WIRE.DELEGATE_SPAWN; task_id?: string; task_name: string; agent: string; surface?: string; prompt_preview: string; prompt?: string }
export interface DelegateResultFrame extends PerChat { type: typeof WIRE.DELEGATE_RESULT; task_id?: string; task_name: string; agent?: string; output_text?: string; status?: string }
export interface CheckVerdictFrame extends PerChat { type: typeof WIRE.CHECK_VERDICT; check?: string; status?: string; summary?: string; findings?: any[]; round?: number; rounds?: number; ran_on?: string; [k: string]: unknown }
export interface PermissionPromptFrame extends PerChat { type: typeof WIRE.PERMISSION_PROMPT; request_id: string; tool_name: string; tool_input: any; description?: string; meeting_agent?: string }
export interface PlanReviewFrame extends PerChat { type: typeof WIRE.PLAN_REVIEW; request_id: string; plan: string; tool_input: any; filename?: string }
export interface QuestionFrame extends PerChat { type: typeof WIRE.QUESTION; tool_name: string; tool_input: any; request_id?: string }
export interface LocationRequestFrame extends PerChat { type: typeof WIRE.LOCATION_REQUEST; request_id: string }
export interface PlanModeFrame extends PerChat { type: typeof WIRE.PLAN_MODE; action: string; plan?: string; filename?: string; tool_input?: any }
export interface SystemFrame extends PerChat { type: typeof WIRE.SYSTEM; subtype: string; message?: string; agent?: string; agent_display_name?: string; agent_color?: string; round?: number; participants?: any[]; max_rounds?: number; max_turns?: number; meeting_id?: string; machine_name?: string; reason?: string; mcp?: string; pct?: number; phase?: string }
export interface MetadataFrame extends PerChat { type: typeof WIRE.METADATA; cost_usd?: number; cost_billed?: boolean; duration_ms?: number; duration_api_ms?: number; context_used?: number; context_max?: number; cache_read?: number; cache_write?: number; input_tokens?: number; output_tokens?: number }
export interface McpCostFrame extends PerChat { type: typeof WIRE.MCP_COST; cost_usd: number; cost_billed?: boolean; provider: string; model: string; tool: string; mcp: string }
export interface TodoUpdateFrame extends PerChat { type: typeof WIRE.TODO_UPDATE; todos: Array<{ content: string; status: string; activeForm?: string }> }
export interface ThreadGoal {
  objective: string
  /** active | complete | paused | usageLimited | budgetLimited; absent on
   *  rows persisted before the field existed (treated as active). */
  status?: string
  token_budget: number | null
  tokens_used: number
  time_used_seconds: number
}
export interface GoalUpdateFrame extends PerChat { type: typeof WIRE.GOAL_UPDATE; goal: ThreadGoal | null }
export interface ContextCompactFrame extends PerChat { type: typeof WIRE.CONTEXT_COMPACT; phase: string; trigger?: string; pre_tokens?: number; post_tokens?: number; context_max?: number; messages_summarized?: number }
export interface DoneFrame extends PerChat { type: typeof WIRE.DONE }
export interface ErrorFrame extends PerChat { type: typeof WIRE.ERROR; message: string }
export interface AbortedFrame extends PerChat { type: typeof WIRE.ABORTED; session_id?: string }
export interface LiveStateFrame extends PerChat {
  type: typeof WIRE.LIVE_STATE
  streaming: boolean
  session_id: string
  started_at: number
  live_blocks: any[]
  active_tools: any[]
  active_agents: any[]
  active_delegates: any[]
  active_commands?: any[]
  pending_permission: any | null
  thinking_active?: boolean
  thinking_text?: string
  thinking_tokens?: number
  todos?: Array<{ content: string; status: string; activeForm?: string }>
  goal?: ThreadGoal | null
  workflows?: Record<string, { tool_use_id: string; workflow_name?: string; progress?: any[]; active?: boolean }>
  meeting_agent?: string | null
  meeting_participants?: Array<{ slug: string; display_name: string; color: string }>
}
export interface LimitWarningFrame extends PerChat, LimitPayload { type: typeof WIRE.LIMIT_WARNING }
export interface LimitReachedFrame extends PerChat, LimitPayload { type: typeof WIRE.LIMIT_REACHED }
export interface GalleryImageWire { url?: string; image_data?: string; mime_type?: string; caption?: string; attribution?: string; link_url?: string; download_url?: string }
export interface ImagesFrame extends PerChat { type: typeof WIRE.IMAGES; images: GalleryImageWire[] }
export interface ImageGeneratingFrame extends PerChat { type: typeof WIRE.IMAGE_GENERATING; prompt_preview: string; model: string }
export interface ImageGenFailedFrame extends PerChat { type: typeof WIRE.IMAGE_GEN_FAILED }
export interface UrlFrame extends PerChat { type: typeof WIRE.URL; url: string; title: string; description: string }
export interface FileFrame extends PerChat { type: typeof WIRE.FILE; filename: string; download_url: string; description: string }
export interface MediaFields { src_kind?: string; url?: string; media_url?: string; token?: string; mime?: string; caption?: string; title?: string; poster?: string }
export interface VideoFrame extends PerChat, MediaFields { type: typeof WIRE.VIDEO }
export interface AudioFrame extends PerChat, MediaFields { type: typeof WIRE.AUDIO }
export interface MediaProcessingFrame extends PerChat { type: typeof WIRE.MEDIA_PROCESSING; media_kind?: string; caption?: string }
export interface MediaFailedFrame extends PerChat { type: typeof WIRE.MEDIA_FAILED; error?: string }
export interface DocumentPreviewFrame extends PerChat { type: typeof WIRE.DOCUMENT_PREVIEW; wopi_url: string; filename: string; file_id: string; download_url: string; snapshot_id?: string; generation?: number }
export interface UiFrame extends PerChat { type: typeof WIRE.UI; token: string; ui_url: string; title?: string; height?: number | null; path?: string }
export interface ArtifactInteractionFrame extends PerChat { type: typeof WIRE.ARTIFACT_INTERACTION; token: string; title?: string; payload?: unknown }
export interface ArtifactAckFrame { type: typeof WIRE.ARTIFACT_ACK; token: string; status: string; reason?: string }
export interface AppActionFrame extends PerChat { type: typeof WIRE.APP_ACTION; app_id: string; slug?: string; title?: string; action_id: string; label?: string; prompt?: string }
export interface AppActionAckFrame { type: typeof WIRE.APP_ACTION_ACK; app_id: string; action_id: string; status: string; reason?: string }
/** The attachment meta a queued or steered message carries: the photos as
 * the server saved them (`{name, path}`) and the validated files
 * (`{path, name}`); absent when the message has none. */
export interface QueueAttachments {
  images?: Array<{ name: string; path?: string }>
  files?: Array<{ path: string; name: string }>
}
export interface QueuedFrame extends PerChat, QueueAttachments { type: typeof WIRE.QUEUED; index: number; text: string }
export interface QueueRemovedFrame extends PerChat, QueueAttachments { type: typeof WIRE.QUEUE_REMOVED; index: number; text: string }
export interface QueueSentFrame extends PerChat, QueueAttachments { type: typeof WIRE.QUEUE_SENT; text: string }
export interface QueueClearedFrame extends PerChat { type: typeof WIRE.QUEUE_CLEARED; text: string }
export interface QueueSnapshotFrame extends PerChat { type: typeof WIRE.QUEUE_SNAPSHOT; messages: Array<{ text: string } & QueueAttachments> }
/** A delegated steer carries the delegating agent's identity in `event_data`
 * (the same meta the row stores), so the bubble wears its badge. */
export interface SteeredFrame extends PerChat, QueueAttachments {
  type: typeof WIRE.STEERED
  text: string
  event_data?: { agent_slug?: string; agent_display_name?: string; agent_color?: string; badge?: string }
}
export interface ModeChangedFrame extends PerChat { type: typeof WIRE.MODE_CHANGED; mode: string }
export interface ModelChangedFrame extends PerChat { type: typeof WIRE.MODEL_CHANGED; model: string }
/** Sent as the persist-only ack of `execution_mode_change`; not acted on. */
export interface ExecutionModeChangedFrame { type: typeof WIRE.EXECUTION_MODE_CHANGED; execution_mode: string }
export interface PlanStatusFrame { type: typeof WIRE.PLAN_STATUS; filename: string; status: string }
export interface ServerTurnStartFrame extends PerChat { type: typeof WIRE.SERVER_TURN_START }
export interface WarmupStartedFrame { type: typeof WIRE.WARMUP_STARTED; chat_id: string; agent: string; execution_path?: string; execution_target?: string; new_chat?: boolean }
export interface WarmupHeartbeatFrame { type: typeof WIRE.WARMUP_HEARTBEAT; chat_id: string }
export interface WarmupReadyFrame {
  type: typeof WIRE.WARMUP_READY
  session_id: string | null
  chat_id: string
  mode: string
  model?: string
  needs_warmup?: boolean
  execution_path?: string
  execution_target?: string
  /** Interactive CLI: true when a live PTY-backed TUI session exists (mount
   *  the terminal); `execution_mode` is the chat's stored override
   *  ('interactive' | '-p' | '') for the toggle state. */
  interactive?: boolean
  execution_mode?: string
  turn_open?: boolean
  fallback_reason?: string | null
  /** Populated only when fallback_reason === 'user-override-offline'. */
  offline_machine_name?: string
  /** Pin-vs-current-target mismatch — present only when the open chat is
   *  pinned to a target different from the agent's resolved one AND the
   *  viewer is the chat owner/admin. Absent = no mismatch (a stored one
   *  is cleared). */
  pinned_target?: string
  pinned_label?: string
  resolved_target?: string
  resolved_label?: string
}
export interface WarmupFailedFrame { type: typeof WIRE.WARMUP_FAILED; chat_id: string; error: string; reason?: string }
export interface PreWarmupReadyFrame { type: typeof WIRE.PRE_WARMUP_READY; session_id: string }
export interface ChatHistoryFrame {
  type: typeof WIRE.CHAT_HISTORY
  chat_id?: string
  agent?: string
  messages: any[]
  has_more?: boolean
  restore?: { todos?: any[]; meeting?: { active?: boolean; participants?: any[]; max_turns?: number } | null; goal?: ThreadGoal | null }
  plans?: any[]
  total_cost?: number
  context_used?: number
  context_max?: number
  cache_read?: number
  cache_write?: number
  output_tokens?: number
  execution_path?: string
  execution_mode?: string
  model?: string
  mode?: string
  process_alive?: boolean
}
export interface ChatStatusFrame { type: typeof WIRE.CHAT_STATUS; chat_id: string; status: string }
export interface ChatStatusSnapshotFrame { type: typeof WIRE.CHAT_STATUS_SNAPSHOT; chat_ids: string[] }
export interface ChatReadFrame { type: typeof WIRE.CHAT_READ; chat_id: string }
export interface ChatRowsFrame { type: typeof WIRE.CHAT_ROWS; chat_id: string; agent?: string }
export interface ChatMetaFrame { type: typeof WIRE.CHAT_META; chat_id: string; delegate_role?: string; project_id?: string }
export interface ChatMovedFrame { type: typeof WIRE.CHAT_MOVED; chat_id: string; new_target: string; resolved_label?: string }
export interface EngineSwitchedFrame { type: typeof WIRE.ENGINE_SWITCHED; chat_id: string; execution_path: string; model: string }
export interface SwitchEngineDeniedFrame { type: typeof WIRE.SWITCH_ENGINE_DENIED; chat_id: string; message: string; process_alive?: boolean }
export interface LivenessFrame { type: typeof WIRE.LIVENESS; chat_id: string; process_alive: boolean }
export interface TitleUpdatedFrame { type: typeof WIRE.TITLE_UPDATED; chat_id: string; title: string }
export interface TurnCompleteFrame { type: typeof WIRE.TURN_COMPLETE; chat_id?: string; title?: string; body?: string }
export interface PtyOutputFrame extends PerChat { type: typeof WIRE.PTY_OUTPUT; session_id: string; data: string; replay?: boolean; reset?: boolean }
export interface PtyExitFrame extends PerChat { type: typeof WIRE.PTY_EXIT; session_id: string; reason: string; code?: number }
export interface PtyPermissionFrame extends PerChat { type: typeof WIRE.PTY_PERMISSION; kind: string; session_id: string; request_id?: string; tool_name?: string; tool_input?: any; plan?: string; filename?: string; meeting_agent?: string }
export interface PtyArtifactFrame extends PerChat { type: typeof WIRE.PTY_ARTIFACT; session_id: string; event: any }
export interface PtyStatusFrame extends PerChat { type: typeof WIRE.PTY_STATUS; session_id: string; state: string; controller?: string; tui_theme?: string }
export interface NotificationFrame { type: typeof WIRE.NOTIFICATION; delivery: any }
export interface NotificationSilentFrame { type: typeof WIRE.NOTIFICATION_SILENT; delivery: any }
export interface NotificationCountFrame { type: typeof WIRE.NOTIFICATION_COUNT; count: number }
export interface FileUpdatedFrame { type: typeof WIRE.FILE_UPDATED; agent_slug: string; rel_path: string; file_id?: string; source?: string; pin?: any }
export interface AppPushFrame { type: typeof WIRE.APP_PUSH; app_id: string; payload: unknown; ts: number }
export interface AppStateFrame { type: typeof WIRE.APP_STATE; app_id: string; doc: Record<string, unknown>; rev: number }
export interface OpenAppFrame {
  type: typeof WIRE.OPEN_APP
  app_id: string
  title: string
  agent: string
  scope_chat_id: string
  scope_project_id: string
  /** Set by the page that showed the app, so the shell's toast (which
   *  looks a tick later) stays quiet. Never on the wire. */
  handled?: boolean
}
export interface AppDeployedFrame { type: typeof WIRE.APP_DEPLOYED; app_id: string; release: number; actions_sig: string }
export interface CatalogFrame { type: typeof WIRE.CATALOG; agent: string; feed: string; seq: number; delta?: Record<string, unknown>; snapshot?: unknown[] }
interface InstallKeyed { machine_id: string; agent: string }
export interface InstallStartedFrame extends InstallKeyed { type: typeof WIRE.INSTALL_STARTED }
export interface InstallMcpPlanFrame extends InstallKeyed { type: typeof WIRE.INSTALL_MCP_PLAN; mcps_to_install: string[]; mcps_to_update: string[] }
export interface InstallProgressFrame extends InstallKeyed { type: typeof WIRE.INSTALL_PROGRESS; mcp: string; phase: string; pct: number; message?: string }
export interface InstallHeartbeatFrame extends InstallKeyed { type: typeof WIRE.INSTALL_HEARTBEAT }
export interface InstallVerifyingFrame extends InstallKeyed { type: typeof WIRE.INSTALL_VERIFYING; message?: string }
export interface InstallDoneFrame extends InstallKeyed { type: typeof WIRE.INSTALL_DONE; warmup_failures?: string[] }
export interface InstallFailedFrame extends InstallKeyed { type: typeof WIRE.INSTALL_FAILED; error: string }
export interface McpInstallFailedFrame extends InstallKeyed { type: typeof WIRE.MCP_INSTALL_FAILED; mcp: string; error: string }
interface TransferKeyed { transfer_id: string; agent_slug: string }
export interface TransferStartedFrame extends TransferKeyed { type: typeof WIRE.TRANSFER_STARTED; rel_path: string; filename: string; kind: string; bytes_total: number; started_at: string; machines: Record<string, unknown> }
export interface TransferMachineStateFrame extends TransferKeyed { type: typeof WIRE.TRANSFER_MACHINE_STATE; machine_id: string; state: string; error?: string }
export interface TransferProgressFrame extends TransferKeyed { type: typeof WIRE.TRANSFER_PROGRESS; machine_id: string; bytes_sent: number; bytes_total: number }
export interface TransferDoneFrame extends TransferKeyed { type: typeof WIRE.TRANSFER_DONE; ok: boolean }
export interface TransferStateFrame extends TransferKeyed { type: typeof WIRE.TRANSFER_STATE; rel_path: string; filename: string; kind: string; bytes_total: number; started_at: string; machines: Record<string, unknown> }
export interface SatelliteUpdatingFrame { type: typeof WIRE.SATELLITE_UPDATING; machine_id: string; machine_name?: string; from_version?: string; to_version: string; started_at?: string }
export interface SatelliteUpdatedFrame { type: typeof WIRE.SATELLITE_UPDATED; machine_id: string; machine_name?: string; version: string }
export interface SatelliteUpdateFailedFrame { type: typeof WIRE.SATELLITE_UPDATE_FAILED; machine_id: string; machine_name?: string; error: string; rolled_back_to?: string; attempts?: number; auto_update_paused?: boolean }
export interface SatelliteUpdateSyncFrame {
  type: typeof WIRE.SATELLITE_UPDATE_SYNC
  inflight: Array<{ machine_id: string; machine_name?: string; from_version?: string; to_version: string }>
}
export interface ServerInfoFrame { type: typeof WIRE.SERVER_INFO; build_id: string; version: string }
export interface PongFrame { type: typeof WIRE.PONG; build_id?: string }

/** Every frame the dashboard socket carries, discriminated on `type`. */
export type WireFrame =
  | TextFrame | ThinkingFrame | ToolStartFrame | ToolInfoFrame | ToolEndFrame | ToolResultFrame
  | TaskSpawnFrame | BgAgentDoneFrame | BgCommandSpawnFrame | BgCommandDoneFrame
  | BgAgentsCompleteFrame | BgCommandsCompleteFrame | FgAgentsCompleteFrame
  | WorkflowStartFrame | WorkflowProgressFrame | WorkflowEndFrame
  | DelegateSpawnFrame | DelegateResultFrame | CheckVerdictFrame
  | PermissionPromptFrame | PlanReviewFrame | QuestionFrame | LocationRequestFrame | PlanModeFrame
  | SystemFrame | MetadataFrame | McpCostFrame | TodoUpdateFrame | GoalUpdateFrame | ContextCompactFrame
  | DoneFrame | ErrorFrame | AbortedFrame | LiveStateFrame | LimitWarningFrame | LimitReachedFrame
  | ImagesFrame | ImageGeneratingFrame | ImageGenFailedFrame | UrlFrame | FileFrame | VideoFrame | AudioFrame
  | MediaProcessingFrame | MediaFailedFrame | DocumentPreviewFrame | UiFrame
  | ArtifactInteractionFrame | ArtifactAckFrame | AppActionFrame | AppActionAckFrame
  | QueuedFrame | QueueRemovedFrame | QueueSentFrame | QueueClearedFrame | QueueSnapshotFrame | SteeredFrame
  | ModeChangedFrame | ModelChangedFrame | ExecutionModeChangedFrame | PlanStatusFrame | ServerTurnStartFrame
  | WarmupStartedFrame | WarmupHeartbeatFrame | WarmupReadyFrame | WarmupFailedFrame | PreWarmupReadyFrame
  | ChatHistoryFrame | ChatStatusFrame | ChatStatusSnapshotFrame | ChatReadFrame | ChatRowsFrame | ChatMetaFrame
  | ChatMovedFrame | EngineSwitchedFrame | SwitchEngineDeniedFrame | LivenessFrame | TitleUpdatedFrame | TurnCompleteFrame
  | PtyOutputFrame | PtyExitFrame | PtyPermissionFrame | PtyArtifactFrame | PtyStatusFrame
  | NotificationFrame | NotificationSilentFrame | NotificationCountFrame | FileUpdatedFrame
  | AppPushFrame | AppStateFrame | OpenAppFrame | AppDeployedFrame | CatalogFrame
  | InstallStartedFrame | InstallMcpPlanFrame | InstallProgressFrame | InstallHeartbeatFrame
  | InstallVerifyingFrame | InstallDoneFrame | InstallFailedFrame | McpInstallFailedFrame
  | TransferStartedFrame | TransferMachineStateFrame | TransferProgressFrame | TransferDoneFrame | TransferStateFrame
  | SatelliteUpdatingFrame | SatelliteUpdatedFrame | SatelliteUpdateFailedFrame | SatelliteUpdateSyncFrame
  | ServerInfoFrame | PongFrame

/** The payload of one frame kind: `WirePayload<'text'>` is `TextFrame`. */
export type WirePayload<T extends WireType> = Extract<WireFrame, { type: T }>
