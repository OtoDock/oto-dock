import { useState } from 'react'
import { createPortal } from 'react-dom'
import {
  useMcpInstances,
  useCreateMcpInstance,
  useUpdateMcpInstance,
  useDeleteMcpInstance,
  useSshKeys,
  useUploadSshKey,
  useDeleteSshKey,
  type McpInstanceField,
  type McpInstance,
} from '../../api/mcps'
import { usePhoneRoutes } from '../../api/phone'
import { useAudioProviders } from '../../api/audio'
import { InstanceFieldInput } from './McpInstanceManager.fieldInput'
import { AgentAssignmentPicker } from './McpInstanceManager.agentPicker'

// The MCP's manifest `hosted.api_key_relay` block (if any), threaded
// down so the instance form can offer the hosted toggle + billing link.
export interface ApiKeyRelayInfo {
  available: boolean
  default_mode: 'self_managed' | 'hosted'
  relay_path: string
  min_balance_to_enable_usd: number
  billing_setup_url: string
}

interface Props {
  mcpName: string
  agents: string[]
  apiKeyRelay?: ApiKeyRelayInfo
  airGapped?: boolean
  relayAvailable?: boolean
}

export default function McpInstanceManager({ mcpName, agents, apiKeyRelay, airGapped, relayAvailable }: Props) {
  const { data } = useMcpInstances(mcpName)
  const createInstance = useCreateMcpInstance()
  const updateInstance = useUpdateMcpInstance()
  const deleteInstance = useDeleteMcpInstance()

  const [editing, setEditing] = useState<{
    id?: number
    instance_name: string
    field_values: Record<string, string>
    agents: string[]
    assigned_to_all: boolean
    hosted_mode: 'self_managed' | 'hosted'
    managed_by?: 'admin' | 'system'
  } | null>(null)
  const [saved, setSaved] = useState(false)

  if (!data) return null

  const { instances: allInstances, fields, delivery, max_instances } = data
  // On air-gapped installs the relay is unreachable, so the platform-managed
  // ("Hosted by OtoDock") instance can't work — hide it entirely (the DB row is
  // preserved and reappears when reconnected). The banner below explains why,
  // and the admin can still add a self-managed instance with their own key.
  const instances = airGapped
    ? allInstances.filter(i => i.managed_by !== 'system')
    : allInstances
  const isSingleInstance = max_instances === 1
  const hasSshKeyField = fields.some(f => f.input_type === 'ssh_key_select')
  const canAdd = !isSingleInstance || instances.length === 0

  const makeEmptyValues = () => {
    const vals: Record<string, string> = {}
    for (const f of fields) vals[f.key] = f.default || ''
    return vals
  }

  const handleSave = () => {
    if (!editing) return
    const name = isSingleInstance ? 'default' : editing.instance_name
    if (!isSingleInstance && !name) return

    const payload = {
      instance_name: name,
      field_values: editing.field_values,
      agents: editing.agents,
      assigned_to_all: editing.assigned_to_all,
      hosted_mode: editing.hosted_mode,
    }

    const onSuccess = () => {
      setEditing(null)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    }

    if (editing.id) {
      updateInstance.mutate({ mcpName, instanceId: editing.id, data: payload }, { onSuccess })
    } else {
      createInstance.mutate({ mcpName, data: payload }, { onSuccess })
    }
  }

  const startEdit = (inst: McpInstance) => {
    setEditing({
      id: inst.id,
      instance_name: inst.instance_name,
      field_values: { ...inst.field_values },
      agents: [...inst.agents],
      assigned_to_all: !!inst.assigned_to_all,
      // Preserve hosted_mode (the system instance is 'hosted'; sending it back
      // unchanged is required — the backend rejects flipping system→self_managed).
      hosted_mode: inst.hosted_mode || 'self_managed',
      managed_by: inst.managed_by,
    })
  }

  const startAdd = () => {
    setEditing({
      instance_name: '',
      field_values: makeEmptyValues(),
      agents: [],
      assigned_to_all: false,
      // Admin-created instances bring their own key → self_managed.
      hosted_mode: 'self_managed',
    })
  }

  // For single-instance env MCPs with no instances yet, auto-open the form
  if (isSingleInstance && instances.length === 0 && !editing) {
    return (
      <div className="space-y-4">
        <div className="text-xs text-p-text-light">No instance configured.</div>
        <button
          onClick={startAdd}
          className="text-xs px-3 py-1.5 rounded-sm bg-brand text-white hover:bg-brand-hover"
        >
          Configure
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* OtoDock Unified API availability — the platform-managed (hosted) instance
          routes through the relay, which is unreachable on air-gapped installs and
          not built yet. */}
      {apiKeyRelay?.available && (airGapped || !relayAvailable) && (
        <div className={`text-[11px] px-2.5 py-2 rounded-lg border ${airGapped
          ? 'border-p-border-light bg-gray-50 dark:bg-gray-800/50 text-p-text-light'
          : 'border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400'}`}>
          {airGapped
            ? 'OtoDock Unified API not available — this is an air-gapped install (no outbound to OtoDock). Use a self-managed instance with your own key instead.'
            : 'OtoDock Unified API activates when the OtoDock relay is live. Until then, hosted calls return a "not available yet" message — or use a self-managed instance with your own key.'}
        </div>
      )}

      {/* SSH Keys section (only for MCPs with ssh_key_select fields) */}
      {hasSshKeyField && <SshKeysSection />}

      {/* Trust note: authorizing an agent for a host hands it shell access
          there — the session gets the referenced private key (0600, wiped at
          session end) and each ssh command still passes the bash permission
          gate, but key material is readable by that agent while it runs. */}
      {hasSshKeyField && (
        <div className="text-[11px] px-2.5 py-2 rounded-lg border border-amber-200 dark:border-amber-900/40 bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400">
          Authorizing an agent for a host grants it shell access there: its
          sessions receive the referenced private key and run ssh directly.
          Assign hosts only to agents you trust at that level.
        </div>
      )}

      {/* Instance list (multi-instance) with inline edit form */}
      {!isSingleInstance && instances.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">Instances</h4>
            <div className="flex items-center gap-2">
              {saved && <span className="text-xs text-green-600">Saved</span>}
              {canAdd && (
                <button
                  onClick={startAdd}
                  className="text-xs px-3 py-1.5 rounded-sm bg-brand text-white hover:bg-brand-hover"
                >
                  Add
                </button>
              )}
            </div>
          </div>
          <div className="space-y-2">
            {instances.map(inst => (
              <div key={inst.id}>
                <InstanceRow
                  instance={inst}
                  fields={fields}
                  isEditing={editing?.id === inst.id}
                  onEdit={() => startEdit(inst)}
                  onDelete={() => {
                    if (confirm(`Delete "${inst.instance_name}"?`))
                      deleteInstance.mutate({ mcpName, instanceId: inst.id })
                  }}
                />
                {/* Inline edit form directly below the row being edited */}
                {editing && editing.id === inst.id && (
                  <div className="mt-2">
                    <InstanceForm
                      editing={editing}
                      setEditing={setEditing}
                      fields={fields}
                      agents={agents}
                      isSingleInstance={false}
                      isPending={createInstance.isPending || updateInstance.isPending}
                      onSave={handleSave}
                      onCancel={() => setEditing(null)}
                      delivery={delivery}
                      otherInstances={instances}
                      apiKeyRelay={apiKeyRelay}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Single-instance display */}
      {isSingleInstance && instances.length > 0 && !editing && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">Configuration</h4>
            <div className="flex items-center gap-2">
              {saved && <span className="text-xs text-green-600">Saved</span>}
            </div>
          </div>
          <div className="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                {fields.filter(f => !f.secret).map(f => {
                  const val = instances[0].field_values[f.key]
                  return val ? (
                    <span key={f.key} className="text-xs text-p-text-secondary">
                      <span className="text-p-text-light">{f.label}:</span> {val}
                    </span>
                  ) : null
                })}
                {fields.some(f => f.secret && instances[0].configured_keys?.includes(f.key)) && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400">
                    credentials configured
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                {instances[0].managed_by === 'system' && (
                  <span
                    className="text-[10px] px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand"
                    title="Platform-managed (Hosted by OtoDock) — you can rename or scope it, but it can't be deleted or switched to self-managed."
                  >
                    Platform-managed
                  </span>
                )}
                {instances[0].assigned_to_all && (
                  <span
                    className="text-[10px] px-1.5 py-0.5 rounded-sm bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400"
                    title="Available to every current and future agent"
                  >
                    available to all
                  </span>
                )}
                {instances[0].agents.map(a => (
                  <span key={a} className="text-[10px] px-1.5 py-0.5 rounded-sm bg-p-surface dark:bg-gray-800 text-p-text-light">{a}</span>
                ))}
                {!instances[0].assigned_to_all && instances[0].agents.length === 0 && (
                  <span className="text-[10px] text-amber-600">No agents assigned</span>
                )}
              </div>
            </div>
            <button onClick={() => startEdit(instances[0])} className="text-xs text-brand hover:text-brand-hover shrink-0">
              Edit
            </button>
          </div>
        </div>
      )}

      {/* No instances + multi-instance */}
      {!isSingleInstance && instances.length === 0 && !editing && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-p-text-light">No instances configured.</span>
          <button onClick={startAdd} className="text-xs px-3 py-1.5 rounded-sm bg-brand text-white hover:bg-brand-hover">
            Add
          </button>
        </div>
      )}

      {/* Add form (new instances) or single-instance edit — shown at bottom */}
      {editing && (!editing.id || isSingleInstance) && (
        <InstanceForm
          editing={editing}
          setEditing={setEditing}
          fields={fields}
          agents={agents}
          isSingleInstance={isSingleInstance}
          isPending={createInstance.isPending || updateInstance.isPending}
          onSave={handleSave}
          onCancel={() => setEditing(null)}
          delivery={delivery}
          otherInstances={instances}
          apiKeyRelay={apiKeyRelay}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Instance row (multi-instance list item)
// ---------------------------------------------------------------------------

function InstanceRow({
  instance: inst,
  fields,
  isEditing,
  onEdit,
  onDelete,
}: {
  instance: McpInstance
  fields: McpInstanceField[]
  isEditing?: boolean
  onEdit: () => void
  onDelete: () => void
}) {
  const visibleFields = fields.filter(f => !f.secret)
  const summary = visibleFields
    .map(f => inst.field_values[f.key])
    .filter(Boolean)
    .slice(0, 3)
    .join(' / ')

  return (
    <div className={`flex items-center gap-3 px-3 py-2.5 rounded-lg border bg-white dark:bg-p-surface ${isEditing ? 'border-brand/50 ring-1 ring-brand/20' : 'border-p-border-light'}`}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-p-text">{inst.instance_name}</span>
          <span className="text-xs text-p-text-secondary truncate">{summary}</span>
        </div>
        <div className="flex items-center gap-1.5 mt-1 flex-wrap">
          {inst.managed_by === 'system' && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand"
              title="Platform-managed (Hosted by OtoDock) — you can rename or scope it, but it can't be deleted or switched to self-managed."
            >
              Platform-managed
            </span>
          )}
          {inst.assigned_to_all && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-sm bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400"
              title="Available to every current and future agent"
            >
              available to all
            </span>
          )}
          {inst.agents.map(a => (
            <span key={a} className="text-[10px] px-1.5 py-0.5 rounded-sm bg-p-surface dark:bg-gray-800 text-p-text-light">{a}</span>
          ))}
          {!inst.assigned_to_all && inst.agents.length === 0 && (
            <span className="text-[10px] text-amber-600">No agents assigned</span>
          )}
        </div>
      </div>
      <div className="flex gap-1.5 shrink-0">
        <button onClick={onEdit} className="text-xs text-brand hover:text-brand-hover">Edit</button>
        {inst.managed_by !== 'system' && (
          <button onClick={onDelete} className="text-xs text-red-500 hover:text-red-700">Delete</button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Instance form (add/edit)
// ---------------------------------------------------------------------------

function InstanceForm({
  editing,
  setEditing,
  fields,
  agents,
  isSingleInstance,
  isPending,
  onSave,
  onCancel,
  delivery,
  otherInstances,
  apiKeyRelay,
}: {
  editing: { id?: number; instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all: boolean; hosted_mode: 'self_managed' | 'hosted'; managed_by?: 'admin' | 'system' }
  setEditing: (v: typeof editing) => void
  fields: McpInstanceField[]
  agents: string[]
  isSingleInstance: boolean
  isPending: boolean
  onSave: () => void
  onCancel: () => void
  delivery?: string
  otherInstances?: McpInstance[]
  apiKeyRelay?: ApiKeyRelayInfo
}) {
  const { data: sshKeys } = useSshKeys()
  // Phone-route dropdown source for `phone_route_outbound_select`
  // manifest fields. Same pattern as `useSshKeys` — fetched here so the
  // form has the data ready by the time the user opens it. Filtered to
  // enabled outbound routes (the only ones phone-mcp can use).
  const { data: phoneRoutes } = usePhoneRoutes()
  const outboundRoutes = (phoneRoutes || []).filter(
    r => r.direction === 'outbound' && r.enabled,
  )
  // Audio-provider dropdown sources for `stt_provider_select` (transcribe-mcp)
  // and `tts_provider_select` (tts-mcp): the platform's providers, so an
  // instance can bind a specific engine. Blank = platform default.
  const { data: audioProviders } = useAudioProviders()
  const sttProviders = (audioProviders || []).filter(p => p.provider_type === 'stt')
  const ttsProviders = (audioProviders || []).filter(p => p.provider_type === 'tts')
  const [confirmAllOpen, setConfirmAllOpen] = useState(false)
  const isEdit = !!editing.id
  // Hosted instances need no install-side credentials (the relay holds them),
  // so the required-field gate doesn't apply when hosted.
  const requiredFilled = editing.hosted_mode === 'hosted' || fields
    .filter(f => f.required && !f.secret)
    .every(f => editing.field_values[f.key]?.trim())
  const nameValid = isSingleInstance || editing.instance_name.trim()

  // Field-level validation — runs live on change AND as a final gate on
  // submit. Returns a short error string or null.
  //
  // ``input_type: "url"`` is the case that matters: catches the user
  // pasting non-URL text (e.g. multi-line content from an install_failed
  // admin_note) into a URL field. Without this the value would be saved
  // verbatim and the MCP would crash at launch with a cryptic stack
  // trace. ``new URL`` is broad enough to accept any realistic endpoint
  // while still rejecting free-text paste.
  const validateFieldValue = (
    field: McpInstanceField,
    value: string,
  ): string | null => {
    const v = (value || '').trim()
    if (!v) return null  // emptiness handled by ``requiredFilled`` above
    if (field.input_type === 'url') {
      try {
        new URL(v)
      } catch {
        return 'Must be a valid URL (e.g. http://localhost:9090)'
      }
    }
    return null
  }

  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const hasFieldErrors = Object.keys(fieldErrors).length > 0

  const validateAndSave = () => {
    const errs: Record<string, string> = {}
    for (const f of fields) {
      const err = validateFieldValue(f, editing.field_values[f.key] || '')
      if (err) errs[f.key] = err
    }
    setFieldErrors(errs)
    if (Object.keys(errs).length === 0) onSave()
  }

  // For env-delivery MCPs, find agents that already have another instance.
  // An "other" instance authorizes an agent if either: agent is in its agents
  // list, OR it has assigned_to_all=true. We treat both as "another source"
  // so the warning fires correctly across both kinds of overlap.
  const agentsWithOtherInstance = new Set<string>()
  let otherHasAssignedToAll = false
  if (delivery === 'env' && otherInstances) {
    for (const inst of otherInstances) {
      if (inst.id === editing.id) continue
      if (inst.assigned_to_all) {
        otherHasAssignedToAll = true
        for (const a of agents) agentsWithOtherInstance.add(a)
      } else {
        for (const a of inst.agents) agentsWithOtherInstance.add(a)
      }
    }
  }

  const updateField = (key: string, value: string) => {
    setEditing({ ...editing, field_values: { ...editing.field_values, [key]: value } })
    // Live re-validation: clear the field's error on every keystroke
    // and re-set it only if the new value is still bad. Avoids stale
    // error messages persisting after the user fixes the input.
    const field = fields.find(f => f.key === key)
    if (!field) return
    const err = validateFieldValue(field, value)
    setFieldErrors(prev => {
      const next = { ...prev }
      if (err) next[key] = err
      else delete next[key]
      return next
    })
  }

  const handleToggleAll = () => {
    if (!editing.assigned_to_all) {
      // Enabling: confirm first because this exposes the instance (potentially
      // with paid API keys) to every current and future agent.
      setConfirmAllOpen(true)
    } else {
      // Disabling: no confirmation. Per-agent list is restored to what was
      // saved (we never strip it on the backend).
      setEditing({ ...editing, assigned_to_all: false })
    }
  }

  const confirmEnableAll = () => {
    setEditing({ ...editing, assigned_to_all: true })
    setConfirmAllOpen(false)
  }

  return (
    <div className="border border-brand/30 rounded-xl bg-brand-surface/30 dark:bg-brand/5 p-4 space-y-3">
      <h4 className="text-sm font-semibold text-p-text">
        {isEdit ? 'Edit' : 'New'} {isSingleInstance ? 'Configuration' : 'Instance'}
      </h4>

      {/* Hosted relay toggle — only for MCPs declaring
          hosted.api_key_relay. The system instance is locked to hosted. */}
      {apiKeyRelay?.available && (
        <div className="rounded-lg border border-p-border-light/60 bg-white dark:bg-gray-800/50 p-3 space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-[11px] font-semibold text-p-text-light uppercase tracking-wide">Credentials source</p>
            {editing.managed_by === 'system' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand">Platform-managed</span>
            )}
          </div>
          {editing.managed_by === 'system' ? (
            <p className="text-xs text-p-text-secondary">
              Routes through the OtoDock relay (hosted). This can't be converted to
              self-managed — create a separate instance with your own key instead.
            </p>
          ) : (
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setEditing({ ...editing, hosted_mode: 'self_managed' })}
                className={`text-xs px-3 py-1.5 rounded-md transition-colors ${editing.hosted_mode === 'self_managed' ? 'bg-brand text-white' : 'border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800'}`}
              >
                My own key
              </button>
              <button
                type="button"
                onClick={() => setEditing({ ...editing, hosted_mode: 'hosted' })}
                className={`text-xs px-3 py-1.5 rounded-md transition-colors ${editing.hosted_mode === 'hosted' ? 'bg-brand text-white' : 'border border-p-border-light text-p-text-secondary hover:bg-gray-50 dark:hover:bg-gray-800'}`}
              >
                Use OtoDock relay
              </button>
            </div>
          )}
          {editing.hosted_mode === 'hosted' && (
            <p className="text-[10px] text-p-text-light italic">
              Hosted via OtoDock — calls route through the relay, no API key needed.
              Credit is billed per-user
              {apiKeyRelay.billing_setup_url ? (
                <>
                  {' '}(<a href={apiKeyRelay.billing_setup_url} target="_blank" rel="noreferrer" className="text-brand underline">set up billing</a>).
                </>
              ) : '.'}
            </p>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {/* Instance name (multi-instance only). System instances have a fixed,
            platform-controlled name — shown read-only. */}
        {!isSingleInstance && (
          <div>
            <label className="text-xs text-p-text-light">Name</label>
            {editing.managed_by === 'system' ? (
              <p className="mt-1 text-sm text-p-text px-2.5 py-1.5">{editing.instance_name}</p>
            ) : (
              <input
                value={editing.instance_name}
                onChange={e => setEditing({ ...editing, instance_name: e.target.value })}
                placeholder="e.g. production-server"
                className="w-full mt-1 text-sm px-2.5 py-1.5 rounded-sm border border-p-border-light bg-white dark:bg-gray-900 text-p-text"
              />
            )}
          </div>
        )}

        {/* Dynamic fields from manifest — hidden when hosted (the relay holds
            the credentials; any values entered here are ignored at runtime). */}
        {editing.hosted_mode !== 'hosted' && fields.map(f => (
          <InstanceFieldInput
            key={f.key}
            f={f}
            editing={editing}
            updateField={updateField}
            isEdit={isEdit}
            fieldErrors={fieldErrors}
            sshKeys={sshKeys}
            outboundRoutes={outboundRoutes}
            sttProviders={sttProviders}
            ttsProviders={ttsProviders}
          />
        ))}

        {/* Agent assignment */}
        <AgentAssignmentPicker
          isSingleInstance={isSingleInstance}
          fields={fields}
          editing={editing}
          setEditing={setEditing}
          handleToggleAll={handleToggleAll}
          agents={agents}
          agentsWithOtherInstance={agentsWithOtherInstance}
          delivery={delivery}
          otherHasAssignedToAll={otherHasAssignedToAll}
        />
      </div>

      <div className="flex gap-2 pt-1">
        <button
          onClick={validateAndSave}
          disabled={isPending || !nameValid || !requiredFilled || hasFieldErrors}
          className="text-xs px-4 py-1.5 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40"
        >
          {isPending ? 'Saving...' : 'Save'}
        </button>
        <button
          onClick={onCancel}
          className="text-xs px-4 py-1.5 rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover"
        >
          Cancel
        </button>
      </div>

      {confirmAllOpen && createPortal(
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-xs" onClick={() => setConfirmAllOpen(false)}>
          <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light shadow-xl p-6 max-w-sm mx-4" onClick={e => e.stopPropagation()}>
            <h3 className="text-sm font-semibold text-p-text mb-2">Make this instance available to all agents?</h3>
            <p className="text-sm text-p-text-secondary mb-5">
              Every current and future agent will be able to enable this MCP.
              If this instance contains paid API keys or sensitive credentials,
              only enable it for instances you intend to share platform-wide.
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirmAllOpen(false)}
                className="px-4 py-2 rounded-lg text-sm font-medium text-p-text-secondary bg-p-surface hover:bg-p-surface-hover transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={confirmEnableAll}
                className="px-4 py-2 rounded-lg text-sm font-medium text-white bg-amber-500 hover:bg-amber-600 transition-colors"
              >
                Enable for all
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SSH Keys section (for MCPs with ssh_key_select fields)
// ---------------------------------------------------------------------------

function SshKeysSection() {
  const { data: keys } = useSshKeys()
  const uploadKey = useUploadSshKey()
  const deleteKey = useDeleteSshKey()

  const handleUpload = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.onchange = () => {
      const file = input.files?.[0]
      if (file) uploadKey.mutate(file)
    }
    input.click()
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-xs font-semibold text-p-text-secondary uppercase tracking-wider">SSH Keys</h4>
        <button
          onClick={handleUpload}
          disabled={uploadKey.isPending}
          className="text-xs px-3 py-1.5 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40"
        >
          {uploadKey.isPending ? 'Uploading...' : 'Upload Key'}
        </button>
      </div>
      {keys && keys.length > 0 ? (
        <div className="space-y-1">
          {keys.map(k => (
            <div key={k.name} className="flex items-center justify-between px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-900/50">
              <div className="flex items-center gap-2 min-w-0">
                <svg className="w-4 h-4 text-p-text-light shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                </svg>
                <span className="text-sm font-mono text-p-text truncate">{k.name}</span>
                <span className="text-xs text-p-text-light">{k.size}B</span>
              </div>
              <button
                onClick={() => { if (confirm(`Delete key "${k.name}"?`)) deleteKey.mutate(k.name) }}
                className="text-xs text-red-500 hover:text-red-700 shrink-0"
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-p-text-light">No SSH keys uploaded.</p>
      )}
    </div>
  )
}
