/**
 * Engine descriptor fixtures — the shape `GET /v1/execution-layers` serves
 * per engine (api/engineDescriptor.ts + the catalog's `configured` /
 * `auto_model` extras), built with sensible defaults so a test states only
 * what it is about.
 *
 * `descriptor()` makes an engine that is NOT one of the real three (id
 * `acme-engine`) — the helper tests use it so they prove the descriptor is
 * read, not remembered. `claudeLike` / `codexLike` / `directLike` mirror the
 * three engines the proxy declares today, for page tests whose agent rows
 * name them.
 */
import type {
  EngineAuth, EngineBehaviour, EngineIdentity, EngineModelPolicy, EngineRuntime, EngineUsage,
} from '@/api/engineDescriptor'
import type { LayerCapabilities } from '@/api/agents'

export interface DescriptorOverrides extends Partial<Omit<
  LayerCapabilities, 'identity' | 'runtime' | 'behaviour' | 'model_policy' | 'auth' | 'usage'
>> {
  identity?: Partial<EngineIdentity>
  runtime?: Partial<EngineRuntime>
  behaviour?: Partial<EngineBehaviour>
  model_policy?: Partial<EngineModelPolicy>
  auth?: Partial<EngineAuth>
  usage?: Partial<EngineUsage>
}

export function descriptor(over: DescriptorOverrides = {}): LayerCapabilities {
  const base: LayerCapabilities = {
    name: 'acme-engine',
    display_name: 'Acme Engine',
    supports_resume: false,
    supports_permissions: true,
    supports_plan_mode: false,
    supports_todos: false,
    supports_subagents: false,
    supports_context_compression: false,
    supports_control_commands: false,
    supports_mcps: true,
    permission_modes: ['default', 'acceptEdits', 'dontAsk'],
    control_commands: [],
    models: [],
    effort_levels: ['low', 'medium', 'high'],
    effort_changeable_mid_session: false,
    compression_threshold_pct: null,
    mcp_delivery: 'external_config',
    mcp_config_format: 'json',
    providers: null,
    identity: {
      short_name: 'acme', vendor_id: '', vendor_label: '', account_label: '', role: 'supporting', sort_order: 100,
    },
    runtime: {
      has_os_process: false, hard_abort_kills_process: false, supports_remote_execution: false,
      supports_interactive_pty: false, interactive_first_prompt_via_argv: false,
      supports_reattach_after_restart: false, binary: '', pin_key: '', config_dir_name: '',
      self_wakes: false, event_queue_depth: 0, interactive_submit_backstop: false,
      installed_name: '',
    },
    behaviour: {
      rebuilds_history_from_db: false, attach_images_inline: false, phone_http_mcps: true,
      skills_delivery: 'materialized_dir', supports_bash: false, supports_plans_dir: false,
      builtin_file_tools: false, has_shell_on_external_route: true, provider_pinned_per_session: false,
      supports_steer: false, supports_compact: false, supports_interrupt_for_queued: false,
      tools: {}, question_tool_holds_turn: false,
    },
    model_policy: { default_model: '', model_filter_policy: 'none', pricing_editable: false },
    auth: { auth_types: ['api_key'], credential_file: null, oauth_flow: '' },
    usage: { windows: [] },
  }
  return {
    ...base,
    ...over,
    identity: { ...base.identity, ...over.identity },
    runtime: { ...base.runtime, ...over.runtime },
    behaviour: { ...base.behaviour, ...over.behaviour },
    model_policy: { ...base.model_policy, ...over.model_policy },
    auth: { ...base.auth, ...over.auth },
    usage: { ...base.usage, ...over.usage },
  }
}

/** The Claude Code engine as the proxy declares it (identity, auth, runtime,
 *  the effort ladder and its one provider). */
export function claudeLike(over: DescriptorOverrides = {}): LayerCapabilities {
  return descriptor({
    name: 'claude-code-cli',
    display_name: 'Claude Code CLI',
    supports_plan_mode: true,
    effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'],
    providers: [
      { id: 'anthropic', label: 'Anthropic', requires_key: true,
        effort_scale: ['low', 'medium', 'high', 'xhigh', 'max'], effort_per_model: ['xhigh'] },
    ],
    ...over,
    identity: {
      short_name: 'claude', vendor_id: 'anthropic', vendor_label: 'Anthropic', account_label: 'Claude',
      role: 'coding', sort_order: 10, ...over.identity,
    },
    runtime: {
      has_os_process: true, supports_remote_execution: true, supports_interactive_pty: true,
      binary: 'claude', pin_key: 'claude_code', config_dir_name: '.claude', ...over.runtime,
    },
    model_policy: { default_model: 'claude-opus-5-5', model_filter_policy: 'none', pricing_editable: false, ...over.model_policy },
    auth: {
      auth_types: ['oauth', 'api_key'],
      credential_file: { wire_kind: 'claude', dirname: '.claude', filename: '.credentials.json' },
      oauth_flow: 'code_paste',
      ...over.auth,
    },
  })
}

/** The Codex engine as the proxy declares it. */
export function codexLike(over: DescriptorOverrides = {}): LayerCapabilities {
  return descriptor({
    name: 'codex-cli',
    display_name: 'OpenAI Codex',
    supports_plan_mode: true,
    effort_levels: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
    providers: [
      { id: 'openai', label: 'OpenAI', requires_key: true,
        effort_scale: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], effort_per_model: ['ultra'] },
      { id: 'ollama', label: 'Ollama (Local)', requires_key: false,
        effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
      { id: 'openai_compatible', label: 'OpenAI-compatible endpoint', requires_key: false,
        effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
    ],
    ...over,
    identity: {
      short_name: 'codex', vendor_id: 'openai', vendor_label: 'OpenAI', account_label: 'ChatGPT',
      role: 'coding', sort_order: 20, ...over.identity,
    },
    runtime: {
      has_os_process: true, supports_remote_execution: true, supports_interactive_pty: true,
      interactive_first_prompt_via_argv: true, binary: 'codex', pin_key: 'codex', config_dir_name: '.codex',
      ...over.runtime,
    },
    model_policy: { default_model: 'gpt-6-sol', model_filter_policy: 'local_providers', pricing_editable: false, ...over.model_policy },
    auth: {
      auth_types: ['oauth', 'api_key', 'local_endpoint'],
      credential_file: { wire_kind: 'codex', dirname: '.codex', filename: 'auth.json' },
      oauth_flow: 'device_code',
      ...over.auth,
    },
  })
}

/** The Direct LLM engine as the proxy declares it. */
export function directLike(over: DescriptorOverrides = {}): LayerCapabilities {
  return descriptor({
    name: 'direct-llm',
    display_name: 'Direct LLM API',
    effort_levels: ['low', 'medium', 'high', 'xhigh', 'max'],
    providers: [
      { id: 'anthropic', label: 'Anthropic', requires_key: true,
        effort_scale: ['low', 'medium', 'high', 'xhigh', 'max'], effort_per_model: ['xhigh'] },
      { id: 'openai', label: 'OpenAI', requires_key: true,
        effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
      { id: 'groq', label: 'Groq', requires_key: true,
        effort_scale: ['low', 'medium', 'high'], effort_per_model: [] },
      { id: 'ollama', label: 'Ollama', requires_key: false,
        effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
      { id: 'openai_compatible', label: 'OpenAI-compatible endpoint', requires_key: false,
        effort_scale: ['low', 'medium', 'high', 'xhigh'], effort_per_model: [] },
    ],
    ...over,
    identity: { short_name: 'direct', role: 'supporting', sort_order: 30, ...over.identity },
    runtime: { ...over.runtime },
    model_policy: { default_model: 'claude-sonnet-5', model_filter_policy: 'all', pricing_editable: true, ...over.model_policy },
    auth: { auth_types: ['api_key', 'local_endpoint', 'relay'], credential_file: null, oauth_flow: '', ...over.auth },
  })
}
