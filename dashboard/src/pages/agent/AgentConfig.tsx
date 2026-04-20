import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useAgentInfo, useUpdateAgent, useDeleteAgent, useDelegationTargets, useSetDelegationTargets, useExecutionLayers, useSetDefaultForNewUsers } from '../../api/agents'
import { useRemoteMachines } from '../../api/remoteMachines'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import {
  type VisibilityMode,
  columnsOf,
  modeOf,
  MODE_GROUPS,
  MODE_LABEL,
  MODE_OPTION_HINT,
  MODE_SUMMARY,
} from '../../lib/visibility'
import StrongConfirmModal from '../../components/StrongConfirmModal'
import { Toggle, SavedIndicator, DeleteModal, COLOR_PRESETS, ENGINE_META, orderEngines } from './AgentConfig.parts'
import { MemorySection } from './AgentConfig.memory'

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function AgentConfig() {
  const { name } = useParams<{ name: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { data: info, isLoading } = useAgentInfo(name!)
  const updateAgent = useUpdateAgent()
  const deleteAgent = useDeleteAgent()
  const { data: delegationData } = useDelegationTargets(name!)
  const setDelegationTargets = useSetDelegationTargets()
  const setDefaultForNewUsers = useSetDefaultForNewUsers()
  const { data: layers } = useExecutionLayers()
  const { data: machines } = useRemoteMachines()

  const role = user?.role || 'member'
  const isAdmin = role === 'admin'

  // Local state synced from server
  const [executionPath, setExecutionPath] = useState('claude-code-cli')
  const [executionPaths, setExecutionPaths] = useState<string[]>(['claude-code-cli'])
  const [defaultModel, setDefaultModel] = useState('')
  // Interactive-CLI per-agent default execution mode: '' (unset
  // → platform default), 'interactive', or '-p'. Only shown when the default
  // model runs on a CLI execution layer (claude-code-cli / codex-cli).
  const [defaultExecutionMode, setDefaultExecutionMode] = useState<'' | 'interactive' | '-p'>('')
  const [defaultEffort, setDefaultEffort] = useState('')
  // Visibility mode = (collaborative × default_scope). The two columns are
  // stored independently; the UI presents them as one of four named modes.
  // `default_scope` still drives tasks/notifications/triggers/meetings/memory.
  const [collaborative, setCollaborative] = useState(true)
  const [defaultScope, setDefaultScope] = useState<'user' | 'agent'>('user')
  // Pending mode awaiting a type-to-confirm (set only for into/out-of Shared
  // only flips, which reshuffle which chats a user sees).
  const [pendingMode, setPendingMode] = useState<VisibilityMode | null>(null)
  const [adminOnly, setAdminOnly] = useState(false)
  const [agentColor, setAgentColor] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [description, setDescription] = useState('')
  const [savedField, setSavedField] = useState<string | null>(null)
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [executionTarget, setExecutionTarget] = useState('local')
  const [selectedTargets, setSelectedTargets] = useState<Set<string>>(new Set())
  // Default-for-new-users — admin only. Empty role = disabled.
  const [dfnuEnabled, setDfnuEnabled] = useState(false)
  const [dfnuRole, setDfnuRole] = useState<'viewer' | 'editor' | 'manager'>('viewer')

  useEffect(() => {
    if (!info) return
    setExecutionPath(info.execution_path || 'claude-code-cli')
    setExecutionPaths(info.execution_paths || [info.execution_path || 'claude-code-cli'])
    setDefaultModel(info.default_model || '')
    setDefaultExecutionMode(info.default_execution_mode || '')
    setDefaultEffort(info.default_effort || '')
    setCollaborative(info.collaborative ?? true)
    setDefaultScope(info.default_scope || 'user')
    setAdminOnly(info.admin_only ?? false)
    setAgentColor(info.color || '')
    setDisplayName(info.display_name || '')
    setDescription(info.description || '')
    setExecutionTarget(info.execution_target || 'local')
    const dfnuRoleRaw = info.default_for_new_users_role
    setDfnuEnabled(!!dfnuRoleRaw)
    if (dfnuRoleRaw) {
      setDfnuRole(dfnuRoleRaw)
    }
  }, [info])

  useEffect(() => {
    if (delegationData) {
      setSelectedTargets(new Set(delegationData.targets))
    }
  }, [delegationData])

  const save = useCallback(
    (field: string, value: any) => {
      updateAgent.mutate(
        { name: name!, [field]: value },
        {
          onSuccess: () => {
            setSavedField(field)
            setTimeout(() => setSavedField(null), 1500)
          },
        },
      )
    },
    [name, updateAgent],
  )

  // Current visibility mode + a one-PATCH writer that persists both columns
  // together (so the agent never lands in a transient half-applied state).
  const mode = modeOf(collaborative, defaultScope)
  const saveMode = useCallback(
    (next: VisibilityMode) => {
      const cols = columnsOf(next)
      setCollaborative(cols.collaborative)
      setDefaultScope(cols.default_scope)
      updateAgent.mutate(
        { name: name!, collaborative: cols.collaborative, default_scope: cols.default_scope },
        {
          onSuccess: () => {
            setSavedField('visibility_mode')
            setTimeout(() => setSavedField(null), 1500)
          },
        },
      )
    },
    [name, updateAgent],
  )

  // Flipping into OR out of Shared only changes chat-history grouping (one
  // shared list ↔ per-user lists), so it gets a type-to-confirm. Every other
  // transition saves immediately.
  const onSelectMode = (next: VisibilityMode) => {
    if (next === mode) return
    const crossesShared = (mode === 'shared_only') !== (next === 'shared_only')
    if (crossesShared) setPendingMode(next)
    else saveMode(next)
  }

  // Merge models from all selected execution paths (used for xhigh detection).
  const allLayerModels = executionPaths.flatMap(p => layers?.[p]?.models || [])
  // Grouped, ordered options for the Default Model picker — engine headers via
  // <optgroup> (Claude Code → Codex → Direct → others). The blank option =
  // "auto", resolved server-side to the best model of the first enabled engine.
  const modelGroups = orderEngines(executionPaths)
    .map(p => ({
      path: p,
      label: ENGINE_META[p]?.label || layers?.[p]?.display_name || p,
      // Drop the per-engine "System Default" placeholder (empty value) — the
      // top-level Auto option already covers "let the platform decide".
      models: (layers?.[p]?.models || []).filter(m => m.value && m.label !== 'System Default'),
    }))
    .filter(g => g.models.length > 0)
  const autoModel = modelGroups[0]?.models[0]

  // Interactive-CLI per-agent default-mode control visibility: only when the
  // agent's DEFAULT model runs on a CLI execution layer (claude-code-cli /
  // codex-cli). Direct-LLM models can't run the interactive TUI, so the
  // control is hidden for them (mirrors the resolver + back-end gate).
  const defaultModelIsCliLayer = !!defaultModel && (
    (layers?.['claude-code-cli']?.models || []).some((m: { value: string }) => m.value === defaultModel) ||
    (layers?.['codex-cli']?.models || []).some((m: { value: string }) => m.value === defaultModel)
  )

  // Does the currently-selected default model support the xhigh effort level?
  // Anthropic: newer reasoning models only. OpenAI gpt-5 family: always. Custom models: per
  // admin's supports_xhigh flag. Auto ('') resolves server-side to the first
  // model of the first enabled engine — mirror that via autoModel, else the
  // flagless "System Default" placeholder hides XHigh even when Auto lands on
  // a supporting model (e.g. Fable 5). Used to gate the "XHigh" option in the
  // effort dropdown below and auto-reset effort when user switches to an
  // unsupported model while xhigh is selected.
  const selectedModelSupportsXhigh = Boolean(
    (defaultModel ? allLayerModels.find(m => m.value === defaultModel) : autoModel)?.supports_xhigh
  )

  // Ultra (Codex multi-agent orchestration on top of max reasoning) is gated
  // per model AND per engine: the backend emits supports_ultra only on the
  // codex-cli layer's entries (gpt-5.6 Sol/Terra), so a Terra selected under
  // direct-llm-only never offers it. `.some()` (not `.find()`) because the
  // same model value can appear in several engines' lists — offered when ANY
  // enabled engine can serve it with ultra.
  const selectedModelSupportsUltra = Boolean(
    defaultModel
      ? allLayerModels.some(m => m.value === defaultModel && m.supports_ultra)
      : autoModel?.supports_ultra
  )

  // If the user switches default_model to one that doesn't support xhigh
  // while default_effort is currently "xhigh", reset effort to "high" in the
  // DB too. Without this the stored value stays "xhigh" and silently failsafes
  // to "max" at session start — surprising when the dropdown shows "High".
  // Same for "ultra", whose nearest valid neighbor is "max".
  useEffect(() => {
    if (defaultEffort === 'xhigh' && defaultModel && !selectedModelSupportsXhigh) {
      setDefaultEffort('high')
      save('default_effort', 'high')
    }
    if (defaultEffort === 'ultra' && defaultModel && !selectedModelSupportsUltra) {
      setDefaultEffort('max')
      save('default_effort', 'max')
    }
  }, [defaultModel, defaultEffort, selectedModelSupportsXhigh, selectedModelSupportsUltra, save])

  const handleDelete = () => {
    deleteAgent.mutate(
      { name: name!, confirm_slug: name! },
      { onSuccess: () => navigate('/agents') },
    )
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  const canManage = !!name && canManageAgent(user, name)
  const agentRole = name ? user?.agent_roles?.[name] : undefined
  const isReadOnly = !canManage  // editor + viewer: read-only

  return (
    <div className="space-y-6">
      {isReadOnly && (
        <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-xl px-4 py-3 text-xs text-amber-800 dark:text-amber-200">
          <strong>Read-only.</strong> Agent settings are owner-only.{' '}
          {agentRole === 'editor'
            ? 'As an editor you can collaborate on the agent\'s shared workspace and files; agent behavior (prompt, MCPs, knowledge, default scope) is curated by an owner.'
            : agentRole === 'viewer'
              ? 'As a viewer you can read the agent\'s workspace, knowledge, and config; only owners can change them.'
              : 'You do not have manager access to this agent.'}
        </div>
      )}
      {/* Settings */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
        <p className="text-xs font-semibold text-p-text-secondary uppercase mb-4">Agent Configuration</p>
        {/* divide-y draws a subtle separator between every setting; the child
            padding utilities give each row consistent breathing room. */}
        <div className="divide-y divide-p-border-light [&>*]:py-4 [&>*:first-child]:pt-0 [&>*:last-child]:pb-0">
          {/* Display Name */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Display Name</p>
              <p className="text-xs text-p-text-light">Human-readable agent name</p>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                onBlur={() => {
                  if (displayName && displayName !== info?.display_name) {
                    save('display_name', displayName)
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    (e.target as HTMLInputElement).blur()
                  }
                }}
                className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 w-48"
              />
              <SavedIndicator show={savedField === 'display_name'} />
            </div>
          </div>

          {/* Slug (read-only) */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Slug</p>
              <p className="text-xs text-p-text-light">Unique identifier (read-only)</p>
            </div>
            <span className="px-2.5 py-1.5 text-sm text-p-text-secondary font-mono bg-p-surface rounded-lg border border-p-border-light">
              {name}
            </span>
          </div>

          {/* Description */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-p-text">Description</p>
                <p className="text-xs text-p-text-light">What this agent does — shown on cards and to other agents</p>
              </div>
              <SavedIndicator show={savedField === 'description'} />
            </div>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              onBlur={() => {
                if (description !== (info?.description || '')) {
                  save('description', description)
                }
              }}
              rows={2}
              placeholder="e.g., Manages smart home devices, cameras, and automations"
              className="w-full px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 resize-none"
            />
          </div>

          {/* Agent Color */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Agent Color</p>
              <p className="text-xs text-p-text-light">Color for cards and chat avatar</p>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex gap-1.5 flex-wrap justify-end">
                {COLOR_PRESETS.map(({ hex, name }) => (
                  <button
                    key={hex}
                    title={name}
                    onClick={() => {
                      setAgentColor(hex)
                      save('color', hex)
                    }}
                    className={`w-6 h-6 rounded-full border-2 transition-all ${
                      agentColor === hex
                        ? 'border-p-text scale-110'
                        : 'border-transparent hover:scale-105'
                    }`}
                    style={{ backgroundColor: hex }}
                  />
                ))}
              </div>
              <SavedIndicator show={savedField === 'color'} />
            </div>
          </div>

          {/* Execution Path */}
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-p-text">AI Engines</p>
                <p className="text-xs text-p-text-light">Which AI engines this agent can use</p>
              </div>
              <SavedIndicator show={savedField === 'execution_paths' || savedField === 'execution_path'} />
            </div>
            {/* Click-to-toggle cards (like the visibility options) — full-width,
                mobile-friendly, with a provider badge per engine. */}
            <div className="space-y-2">
              {orderEngines(layers ? Object.keys(layers) : ['claude-code-cli', 'direct-llm']).map((path) => {
                const meta = ENGINE_META[path] || {}
                const label = meta.label || layers?.[path]?.display_name || path
                const checked = executionPaths.includes(path)
                return (
                  <button
                    type="button"
                    key={path}
                    disabled={isReadOnly}
                    onClick={() => {
                      let next: string[]
                      if (checked) {
                        next = executionPaths.filter(p => p !== path)
                        if (next.length === 0) return // at least one required
                      } else {
                        next = [...executionPaths, path]
                      }
                      setExecutionPaths(next)
                      setExecutionPath(next[0])
                      // Reset model if it's not available in any selected engine.
                      const allModels = next.flatMap(p => layers?.[p]?.models || [])
                      if (!allModels.some((m: { value: string }) => m.value === defaultModel)) {
                        setDefaultModel('')
                        save('default_model', '')
                      }
                      save('execution_paths', next)
                    }}
                    className={`w-full flex items-start gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors ${
                      checked ? 'border-brand bg-brand-surface' : 'border-p-border-light hover:bg-p-surface-hover'
                    } ${isReadOnly ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
                  >
                    <span className={`mt-0.5 w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center ${
                      checked ? 'bg-brand border-brand text-white' : 'border-p-border-light'
                    }`}>
                      {checked && (
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                        </svg>
                      )}
                    </span>
                    <span className="min-w-0">
                      <span className="flex items-center gap-1.5 flex-wrap">
                        {meta.badge && (
                          <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary border border-p-border-light">
                            {meta.badge}
                          </span>
                        )}
                        <span className="text-sm font-medium text-p-text">{label}</span>
                      </span>
                      {meta.desc && <span className="block text-xs text-p-text-light mt-0.5">{meta.desc}</span>}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Execution Target — admins only. Non-admins can't set a remote
              target (backend enforces admin + admin-paired on save), so the
              control is hidden rather than shown-then-rejected. Also hidden
              when this build ships without the remote-machines feature. */}
          {isAdmin && user?.feature_flags?.remote_machines_available !== false && (
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Execution Target</p>
              <p className="text-xs text-p-text-light">
                {executionPath === 'direct-llm'
                  ? 'Direct LLM agents always run locally'
                  : 'Where this agent runs — local or on a remote machine via satellite'}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {executionPath === 'direct-llm' ? (
                <span className="text-sm text-p-text-light">Local only</span>
              ) : (
                <select
                  value={executionTarget}
                  onChange={e => {
                    setExecutionTarget(e.target.value)
                    save('execution_target', e.target.value)
                  }}
                  className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                >
                  <option value="local">Local (this server)</option>
                  {(machines ?? []).filter(m => m.pairing_scope === 'admin').map(m => (
                    <option key={m.id} value={m.id}>
                      {m.name} {m.status === 'online' ? '' : `[${m.status}]`}
                    </option>
                  ))}
                </select>
              )}
              {executionTarget !== 'local' && (
                <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${
                  (machines ?? []).find(m => m.id === executionTarget)?.status === 'online'
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                    : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                }`}>
                  {(machines ?? []).find(m => m.id === executionTarget)?.status ?? 'unknown'}
                </span>
              )}
              <SavedIndicator show={savedField === 'execution_target'} />
            </div>
          </div>
          )}

          {/* Default Model */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Default Model</p>
              <p className="text-xs text-p-text-light">Model used when no override is set</p>
            </div>
            <div className="flex items-center gap-2">
              <select
                value={defaultModel}
                onChange={(e) => {
                  setDefaultModel(e.target.value)
                  save('default_model', e.target.value)
                }}
                className="w-full sm:w-auto sm:max-w-xs px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
              >
                <option value="">{autoModel ? `Auto — ${autoModel.label}` : 'Auto (first available engine)'}</option>
                {modelGroups.map(g => (
                  <optgroup key={g.path} label={g.label}>
                    {g.models.map(m => (
                      <option key={m.value} value={m.value}>{m.label}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <SavedIndicator show={savedField === 'default_model'} />
            </div>
          </div>

          {/* Default Session Mode (Interactive CLI) — only when the
              default model runs on a CLI execution layer; direct-llm can't run
              the interactive TUI so it's hidden there, and only when the
              platform-wide interactive kill-switch is on. Manager-gated
              server-side (PATCH /v1/agents requires manage). */}
          {defaultModelIsCliLayer && user?.feature_flags?.interactive_terminal_enabled !== false && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Default Session Mode</p>
                <p className="text-xs text-p-text-light">
                  How new chats &amp; tasks start — the normal headless stream, or the
                  interactive terminal (the native CLI running as a live TUI)
                </p>
              </div>
              <div className="flex items-center gap-2">
                <select
                  value={defaultExecutionMode || '-p'}
                  onChange={(e) => {
                    const v = e.target.value as 'interactive' | '-p'
                    setDefaultExecutionMode(v)
                    save('default_execution_mode', v)
                  }}
                  className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                >
                  <option value="-p">Normal (headless)</option>
                  <option value="interactive">Interactive terminal</option>
                </select>
                <SavedIndicator show={savedField === 'default_execution_mode'} />
              </div>
            </div>
          )}

          {/* Default Effort */}
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
            <div>
              <p className="text-sm font-medium text-p-text">Default Effort</p>
              <p className="text-xs text-p-text-light">Thinking effort level</p>
            </div>
            <div className="flex items-center gap-2">
              <select
                // Coerce to the nearest valid level when the current model
                // doesn't support the persisted default_effort (user must
                // have picked it before switching models): xhigh → high,
                // ultra → max. This keeps the dropdown visually consistent
                // with what actually ships to the model, instead of showing
                // a stale invalid value.
                value={(
                  defaultEffort === 'xhigh' && !selectedModelSupportsXhigh ? 'high'
                    : defaultEffort === 'ultra' && !selectedModelSupportsUltra ? 'max'
                      : defaultEffort
                ) || 'high'}
                onChange={(e) => {
                  setDefaultEffort(e.target.value)
                  save('default_effort', e.target.value)
                }}
                className="px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
              >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                {selectedModelSupportsXhigh && <option value="xhigh">XHigh</option>}
                <option value="max">Max</option>
                {/* Ultra = max reasoning + Codex-native parallel sub-agents
                    (gpt-5.6 Sol/Terra on the codex engine only). No cost
                    warning by design — Codex's own TUI communicates quota
                    impact, and Claude's equivalent carries none either. */}
                {selectedModelSupportsUltra && <option value="ultra">Ultra</option>}
              </select>
              <SavedIndicator show={savedField === 'default_effort'} />
            </div>
          </div>

          {/* Visibility & workspace — the four modes (collaborative × default
              scope). Replaces the old Default-Scope dropdown + Internal toggle;
              `default_scope` still drives tasks / notifications / triggers /
              meetings / memory. */}
          <div className="flex flex-col gap-3">
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Visibility &amp; workspace</p>
                <p className="text-xs text-p-text-light">
                  Who shares this agent's files, chats, and memory — and the
                  default scope for its tasks, notifications, triggers, and meetings.
                </p>
              </div>
              <SavedIndicator show={savedField === 'visibility_mode'} />
            </div>

            {isReadOnly ? (
              <div className="rounded-lg border border-p-border-light bg-p-bg px-3 py-2">
                <p className="text-sm font-medium text-p-text">{MODE_LABEL[mode]}</p>
                <p className="text-xs text-p-text-light mt-0.5">{MODE_SUMMARY[mode]}</p>
              </div>
            ) : (
              <>
                <div className="space-y-3">
                  {MODE_GROUPS.map((group) => (
                    <fieldset key={group.label} className="space-y-1.5">
                      <legend className="text-xs font-semibold text-p-text-secondary mb-1">
                        {group.label}
                      </legend>
                      {group.modes.map((m) => {
                        const selected = mode === m
                        return (
                          <label
                            key={m}
                            className={`flex items-start gap-2.5 rounded-lg border px-3 py-2 cursor-pointer transition-colors ${
                              selected
                                ? 'border-brand bg-brand-surface'
                                : 'border-p-border-light hover:bg-p-surface-hover'
                            }`}
                          >
                            <input
                              type="radio"
                              name="visibility-mode"
                              checked={selected}
                              onChange={() => onSelectMode(m)}
                              className="mt-0.5 accent-brand"
                            />
                            <span className="min-w-0">
                              <span className="block text-sm font-medium text-p-text">{MODE_LABEL[m]}</span>
                              <span className="block text-xs text-p-text-light">{MODE_OPTION_HINT[m]}</span>
                            </span>
                          </label>
                        )
                      })}
                    </fieldset>
                  ))}
                </div>
                <p className="text-xs text-p-text-secondary">{MODE_SUMMARY[mode]}</p>
              </>
            )}
          </div>

          {/* Admin Only — admin users only */}
          {isAdmin && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
              <div>
                <p className="text-sm font-medium text-p-text">Admin Only</p>
                <p className="text-xs text-p-text-light">Restrict access to admin users</p>
              </div>
              <div className="flex items-center gap-2">
                <Toggle
                  checked={adminOnly}
                  onChange={(v) => {
                    setAdminOnly(v)
                    save('admin_only', v)
                  }}
                />
                <SavedIndicator show={savedField === 'admin_only'} />
              </div>
            </div>
          )}

          {/* Default for new users — admin only.
              Non-admin managers don't see this; flipping it affects every
              platform user (auto-attach at signup), so it's a platform-admin
              policy decision, not a per-agent-manager one. */}
          {isAdmin && (
            <div className="flex flex-col gap-2">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
                <div>
                  <p className="text-sm font-medium text-p-text">Default for new users</p>
                  <p className="text-xs text-p-text-light">
                    Every newly-created user is auto-attached to this agent with the chosen role.
                    Existing users are unaffected. Admin-only.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Toggle
                    checked={dfnuEnabled}
                    onChange={(v) => {
                      setDfnuEnabled(v)
                      setDefaultForNewUsers.mutate(
                        { agent: name!, enabled: v, role: v ? dfnuRole : null },
                        {
                          onSuccess: () => {
                            setSavedField('default_for_new_users_role')
                            setTimeout(() => setSavedField(null), 1500)
                          },
                        },
                      )
                    }}
                  />
                  <SavedIndicator show={savedField === 'default_for_new_users_role'} />
                </div>
              </div>
              <div className={`flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4 ${dfnuEnabled ? '' : 'opacity-40'}`}>
                <p className="text-xs text-p-text-secondary">Role assigned at auto-attach</p>
                <select
                  value={dfnuRole}
                  disabled={!dfnuEnabled}
                  onChange={(e) => {
                    const v = e.target.value as 'viewer' | 'editor' | 'manager'
                    setDfnuRole(v)
                    if (dfnuEnabled) {
                      setDefaultForNewUsers.mutate(
                        { agent: name!, enabled: true, role: v },
                        {
                          onSuccess: () => {
                            setSavedField('default_for_new_users_role')
                            setTimeout(() => setSavedField(null), 1500)
                          },
                        },
                      )
                    }
                  }}
                  className="w-full sm:w-auto px-2.5 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 disabled:cursor-not-allowed"
                >
                  <option value="viewer">Viewer (read-only, recommended for personal assistants)</option>
                  <option value="editor">Editor (shared workspace edits)</option>
                  <option value="manager">Manager (full configuration access)</option>
                </select>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Memory — managers + admins. Rows gate on the mode's available scopes. */}
      <MemorySection name={name!} mode={mode} />


      {/* Delegation Targets */}
      {delegationData && delegationData.available.length > 0 && (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
          <div className="flex items-center justify-between mb-4">
            <div>
              <p className="text-xs font-semibold text-p-text-secondary uppercase">Delegation / Meetings Targets</p>
              <p className="text-xs text-p-text-light mt-0.5">Agents this agent can delegate tasks to or invite to meetings</p>
            </div>
            <SavedIndicator show={savedField === 'delegation_targets'} />
          </div>
          {/* Checkbox-style cards — same pattern as the AI Engines selection. */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {delegationData.available.map((agent) => {
              const selected = selectedTargets.has(agent.name)
              return (
                <button
                  type="button"
                  key={agent.name}
                  disabled={isReadOnly}
                  onClick={() => {
                    // Autosave per toggle (platform convention — same as the
                    // MCP toggles / policy radios): a selection that only
                    // LOOKS applied until a Save click gets silently lost.
                    const next = new Set(selectedTargets)
                    if (next.has(agent.name)) next.delete(agent.name)
                    else next.add(agent.name)
                    setSelectedTargets(next)
                    setDelegationTargets.mutate(
                      { agent: name!, targets: Array.from(next) },
                      {
                        onSuccess: () => {
                          setSavedField('delegation_targets')
                          setTimeout(() => setSavedField(null), 1500)
                        },
                        // On failure resync to the server's truth.
                        onError: () => setSelectedTargets(new Set(delegationData.targets)),
                      },
                    )
                  }}
                  className={`w-full flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors ${
                    selected ? 'border-brand bg-brand-surface' : 'border-p-border-light hover:bg-p-surface-hover'
                  } ${isReadOnly ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
                >
                  <span className={`w-4 h-4 shrink-0 rounded-sm border flex items-center justify-center ${
                    selected ? 'bg-brand border-brand text-white' : 'border-p-border-light'
                  }`}>
                    {selected && (
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                  </span>
                  <span
                    className="w-5 h-5 rounded-full shrink-0 flex items-center justify-center text-white text-[9px] font-bold"
                    style={{ backgroundColor: agent.color || '#6B7280' }}
                  >
                    {agent.name.charAt(0).toUpperCase()}
                  </span>
                  <span className="text-sm text-p-text truncate">{agent.display_name}</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Danger Zone — admin only */}
      {isAdmin && (
        <div className="rounded-xl border-2 border-red-300 dark:border-red-800 p-4">
          <p className="text-xs font-semibold text-red-600 uppercase mb-2">Danger Zone</p>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-p-text">Delete this agent</p>
              <p className="text-xs text-p-text-light">Once deleted, this cannot be undone.</p>
            </div>
            <button
              onClick={() => setDeleteModalOpen(true)}
              className="px-3 py-1.5 text-sm font-medium rounded-lg border border-red-300 dark:border-red-700 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
            >
              Delete Agent
            </button>
          </div>
        </div>
      )}

      {deleteModalOpen && (
        <DeleteModal
          slug={name!}
          onConfirm={handleDelete}
          onCancel={() => setDeleteModalOpen(false)}
          isPending={deleteAgent.isPending}
        />
      )}

      {/* Shared-only flip confirmation. Existing chats are never deleted; only
          which chats a user sees changes (one shared list ↔ per-user lists). */}
      {pendingMode && (
        <StrongConfirmModal
          title={`Switch to ${MODE_LABEL[pendingMode]}?`}
          description={
            pendingMode === 'shared_only' ? (
              <>
                In <strong>Shared only</strong>, everyone assigned to this agent shares
                one workspace and <strong>one chat history</strong> — each person will
                see everybody's conversations. Existing per-user chats are kept but stop
                appearing in the chat list; new chats are shared.
              </>
            ) : (
              <>
                Leaving <strong>Shared only</strong> gives each person their own chats
                again. The existing shared conversations are kept but stop appearing in
                the chat list; new chats are per-user.
              </>
            )
          }
          confirmWord="CONFIRM"
          confirmLabel="Switch mode"
          destructive={false}
          onCancel={() => setPendingMode(null)}
          onConfirm={() => {
            saveMode(pendingMode)
            setPendingMode(null)
          }}
        />
      )}
    </div>
  )
}
