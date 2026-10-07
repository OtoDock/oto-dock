/**
 * The engine descriptor — the TypeScript mirror of the proxy's
 * `LayerCapabilities.to_dict()` (proxy/core/execution_layer.py). Every AI
 * engine the platform registers is described by one of these; the dashboard
 * asks the descriptor (through lib/engines.ts) and never compares an engine
 * id, so a fourth engine renders correctly the day it declares itself.
 *
 * The flat fields are the frozen public contract; every capability added
 * since 2026-09-20 lives in a typed group. Types only — no hooks here, so
 * both api/agents.ts (the public catalog) and api/executionLayers.ts (the
 * admin and user rows) can import them without a cycle.
 */

/** One model an engine offers, as `GET /v1/execution-layers` lists it. */
export interface LayerModelOption {
  value: string
  label: string
  provider?: string
  supports_xhigh?: boolean
  supports_ultra?: boolean
  // Capability tier (1 = frontier … 4 = fast; null/absent = untiered), its
  // label and the one-line "good at". Absent on an older proxy.
  tier?: number | null
  tier_label?: string
  good_at?: string
}

/** A provider the engine's models come from (every engine declares at least
 *  its vendor). `requires_key` is false for a self-hosted endpoint (Ollama,
 *  an OpenAI-compatible server) and true for a vendor key. `effort_scale`
 *  is the platform effort levels the provider's API takes, in ladder order;
 *  `effort_per_model` the subset a model row's `supports_<level>` flag
 *  gates. Both are declared on every served entry (the proxy's contract
 *  test) and absent only on a synthetic entry built from the vendor. */
export interface EngineProvider {
  id: string
  label: string
  requires_key?: boolean
  effort_scale?: string[]
  effort_per_model?: string[]
  /** `vendor` (a company's API, keyed or logged in) or `local` (a
   *  self-hosted server on the operator's network — never offered on hosted
   *  OtoDock). */
  kind?: 'vendor' | 'local' | string
  /** The hosted relay's base path for a vendor the relay fronts, '' otherwise. */
  relay_path?: string
  /** The path under a bare endpoint where a local provider's
   *  OpenAI-compatible API answers ('/v1' for Ollama), '' when the root is it. */
  api_path?: string
}

/** Who the engine is, for labels and ordering. `vendor_*` are empty for a
 *  multi-provider engine; `account_label` is what a subscription to it is
 *  called ("Claude", "ChatGPT"); `role` separates the coding engines from a
 *  supporting one; `sort_order` is the AI Engines page order. */
export interface EngineIdentity {
  short_name: string
  vendor_id: string
  vendor_label: string
  account_label: string
  role: 'coding' | 'supporting' | string
  sort_order: number
}

/** How the engine runs — process, placement, binary, its TUI. */
export interface EngineRuntime {
  has_os_process: boolean
  hard_abort_kills_process: boolean
  supports_remote_execution: boolean
  supports_interactive_pty: boolean
  interactive_first_prompt_via_argv: boolean
  supports_reattach_after_restart: boolean
  /** An idle remote session a satellite kept across a platform restart is
   *  taken back. */
  readopts_idle_session: boolean
  binary: string
  pin_key: string
  config_dir_name: string
  self_wakes: boolean
  event_queue_depth: number
  interactive_submit_backstop: boolean
  /** The name a satellite's `installed_clis` capability reports for the
   *  engine's CLI (`claude-code` / `codex`) — frozen satellite wire; the
   *  machine cards map it to `binary` to read `cli_status`. */
  installed_name: string
}

/** What the engine does with a conversation, tools and files. */
export interface EngineBehaviour {
  rebuilds_history_from_db: boolean
  attach_images_inline: boolean
  phone_http_mcps: boolean
  skills_delivery: 'materialized_dir' | 'prompt_catalog' | string
  supports_bash: boolean
  supports_plans_dir: boolean
  builtin_file_tools: boolean
  has_shell_on_external_route: boolean
  provider_pinned_per_session: boolean
  supports_steer: boolean
  supports_compact: boolean
  supports_interrupt_for_queued: boolean
  /** The engine's native tool names by role (`lib/tools/roles` is the
   *  canonical set they map into on the proxy). */
  tools: Record<string, string[]>
  /** The engine's question tool holds the turn open for the platform's
   *  answers (Codex); false when the proxy denies the call, shows the card
   *  and the answer arrives as the next message (Claude headless). */
  question_tool_holds_turn: boolean
}

/** Which model an unpinned agent on the engine runs, and how its model list
 *  is trimmed to the providers with an active subscription. */
export interface EngineModelPolicy {
  default_model: string
  model_filter_policy: 'all' | 'local_providers' | 'none' | string
  pricing_editable: boolean
}

/** The credential file a CLI engine reads its login from (null for an engine
 *  whose credentials never touch disk). */
export interface EngineCredentialFile {
  wire_kind: string
  dirname: string
  filename: string
}

/** The SHAPE of the login the dashboard renders: a popup whose page shows a
 *  code the user pastes back, or a verification URL plus a one-time code the
 *  page polls on. "" for an engine without a login. */
export type EngineOAuthFlow = 'code_paste' | 'device_code' | ''

/** How a subscription to the engine is held. `auth_types` are the row kinds
 *  the engine accepts (`oauth` | `api_key` | `local_endpoint` | `relay`). */
export interface EngineAuth {
  auth_types: string[]
  credential_file: EngineCredentialFile | null
  oauth_flow: EngineOAuthFlow
}

/** One rate-limit window the engine's vendor reports on a subscription. */
export interface EngineWindowSpec {
  key: string
  length_s: number
  role: 'session' | 'quota' | string
  label: string
}

export interface EngineUsage {
  windows: EngineWindowSpec[]
}

export interface EngineDescriptor {
  // The frozen flat contract.
  name: string
  display_name: string
  supports_resume: boolean
  supports_permissions: boolean
  supports_plan_mode: boolean
  supports_todos: boolean
  supports_subagents: boolean
  supports_context_compression: boolean
  supports_control_commands: boolean
  supports_mcps: boolean
  permission_modes: string[]
  control_commands: string[]
  models: LayerModelOption[]
  effort_levels: string[]
  effort_changeable_mid_session: boolean
  compression_threshold_pct: number | null
  mcp_delivery: string
  mcp_config_format: string | null
  providers: EngineProvider[] | null
  // The typed groups.
  identity: EngineIdentity
  runtime: EngineRuntime
  behaviour: EngineBehaviour
  model_policy: EngineModelPolicy
  auth: EngineAuth
  usage: EngineUsage
}
