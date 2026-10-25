import { useId, useRef, useState, type RefObject } from 'react'
import { safeHref } from '../../lib/safeUrl'
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
import { usePhoneRoutes, PHONE_OUTBOUND } from '../../api/phone'
import { useAudioProviders } from '../../api/audio'
import { InstanceFieldInput } from './McpInstanceManager.fieldInput'
import { AgentAssignmentPicker } from './McpInstanceManager.agentPicker'
import { useDialogs } from '../ui/dialogs'
import Button from '../ui/Button'
import { Input } from '../ui/Field'
import { BADGE_BASE, BADGE_TONE, BUTTON_BASE, BUTTON_SIZE, CARD_BORDERED, SEGMENT } from '../ui/classes'
import InfoTip from '../ui/InfoTip'
import { useInvokedForm } from '../common/useInvokedForm'
import AlertDialog from '../ui/AlertDialog'
import { toastError } from '../../lib/http/apiJson'
import { useSaveState } from '../../hooks/useSaveState'
import { SaveState } from '../ui/SettingsControls'

// The rows' chips: a badge's tone by meaning, at the rows' small size.
const CHIP = `${BADGE_BASE} px-1.5 text-3xs`
const CHIP_ACTION = `${CHIP} ${BADGE_TONE.action}`
const CHIP_WARNING = `${CHIP} ${BADGE_TONE.warning}`
const CHIP_SUCCESS = `${CHIP} ${BADGE_TONE.success}`
const CHIP_NEUTRAL = `${CHIP} ${BADGE_TONE.neutral}`

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
  const dialogs = useDialogs()
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
  const { stateOf, run } = useSaveState()
  const newFormId = useId()
  // The button a closed form gives the focus to when its opener is gone:
  // Configure, the single instance's Edit and the first Add are replaced by
  // the form they open, then rendered anew (the header's Add after a first
  // instance is saved). At most one of them renders at a time.
  const reopenRef = useRef<HTMLButtonElement>(null)

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

  // Resolves true once saved, so the form can give the focus back.
  const handleSave = async (): Promise<boolean> => {
    if (!editing) return false
    const name = isSingleInstance ? 'default' : editing.instance_name
    if (!isSingleInstance && !name) return false

    const payload = {
      instance_name: name,
      field_values: editing.field_values,
      agents: editing.agents,
      assigned_to_all: editing.assigned_to_all,
      hosted_mode: editing.hosted_mode,
    }

    // The form closes itself once this resolves true (it gives the focus back).
    const id = editing.id
    return run('instance', () => {
      if (id) return updateInstance.mutateAsync({ mcpName, instanceId: id, data: payload })
      return createInstance.mutateAsync({ mcpName, data: payload })
    }, { failTitle: id ? 'The instance was not saved' : 'The instance was not created' })
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

  const form = editing && (
    <InstanceForm
      id={editing.id ? undefined : newFormId}
      editing={editing}
      setEditing={setEditing}
      fields={fields}
      agents={agents}
      isSingleInstance={isSingleInstance}
      isPending={createInstance.isPending || updateInstance.isPending}
      onSave={handleSave}
      onCancel={() => setEditing(null)}
      returnTo={reopenRef}
      delivery={delivery}
      otherInstances={instances}
      apiKeyRelay={apiKeyRelay}
    />
  )
  const adding = !!editing && !editing.id

  // For single-instance env MCPs with no instances yet, auto-open the form
  if (isSingleInstance && instances.length === 0 && !editing) {
    return (
      <div className="space-y-4">
        <div className="text-xs text-tertiary">No instance configured.</div>
        <Button ref={reopenRef} variant="primary" size="xs" onClick={startAdd}>
          Configure
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* OtoDock Unified API availability — the platform-managed (hosted) instance
          routes through the relay, which is unreachable on air-gapped installs and
          not built yet. */}
      {apiKeyRelay?.available && (airGapped || !relayAvailable) && (
        <div className={`text-2xs px-2.5 py-2 rounded-card border ${airGapped
          ? 'border-subtle bg-surface-sunken text-tertiary'
          : 'border-status-warning-border bg-status-warning-bg text-status-warning-fg'}`}>
          {airGapped
            ? 'OtoDock Unified API is not available. This air-gapped install cannot reach OtoDock. Use a self-managed instance with your own key instead.'
            : 'OtoDock Unified API is not live yet. Until it is, hosted calls will not work. You can use a self-managed instance with your own key.'}
        </div>
      )}

      {/* SSH Keys section (only for MCPs with ssh_key_select fields) */}
      {hasSshKeyField && <SshKeysSection />}

      {/* Trust note: authorizing an agent for a host hands it shell access
          there — the session gets the referenced private key (0600, wiped at
          session end) and each ssh command still passes the bash permission
          gate, but key material is readable by that agent while it runs. */}
      {hasSshKeyField && (
        <div className="text-2xs px-2.5 py-2 rounded-card border border-status-warning-border bg-status-warning-bg text-status-warning-fg">
          Giving an agent a host gives it shell access there. Its sessions get
          the private key and run ssh directly. Give hosts only to agents you
          trust that much.
        </div>
      )}

      {/* Instance list (multi-instance) with inline edit form */}
      {!isSingleInstance && instances.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider">Instances</h4>
            <div className="flex items-center gap-2">
              <SaveState state={stateOf('instance')} besideButton />
              {canAdd && (
                <Button
                  ref={reopenRef}
                  variant="primary"
                  size="xs"
                  onClick={startAdd}
                  aria-expanded={adding}
                  aria-controls={adding ? newFormId : undefined}
                >
                  Add
                </Button>
              )}
            </div>
          </div>
          {/* The new-instance form opens under its Add button, above the
              rows: below a long list it would open out of sight. */}
          {adding && <div className="mb-2">{form}</div>}
          <div className="space-y-2">
            {instances.map(inst => (
              <div key={inst.id}>
                <InstanceRow
                  instance={inst}
                  fields={fields}
                  isEditing={editing?.id === inst.id}
                  onEdit={() => startEdit(inst)}
                  onDelete={async () => {
                    if (await dialogs.confirm({ title: `Delete "${inst.instance_name}"?`, confirmLabel: 'Delete', tone: 'danger' }))
                      deleteInstance.mutateAsync({ mcpName, instanceId: inst.id }).catch(toastError('The instance was not deleted'))
                  }}
                />
                {/* Inline edit form directly below the row being edited */}
                {editing && editing.id === inst.id && (
                  <div className="mt-2">{form}</div>
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
            <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider">Configuration</h4>
            <div className="flex items-center gap-2">
              <SaveState state={stateOf('instance')} besideButton />
            </div>
          </div>
          <div className={`${CARD_BORDERED} flex items-center gap-3 px-3 py-2.5`}>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                {fields.filter(f => !f.secret).map(f => {
                  const val = instances[0].field_values[f.key]
                  return val ? (
                    <span key={f.key} className="text-xs text-secondary">
                      <span className="text-tertiary">{f.label}:</span> {val}
                    </span>
                  ) : null
                })}
                {fields.some(f => f.secret && instances[0].configured_keys?.includes(f.key)) && (
                  <span className={CHIP_SUCCESS}>
                    Credentials configured
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                {instances[0].managed_by === 'system' && (
                  <span
                    className={CHIP_ACTION}
                    title="Platform-managed: OtoDock hosts it. You can rename it or choose its agents. You can't delete it or switch it to self-managed."
                  >
                    Platform-managed
                  </span>
                )}
                {instances[0].assigned_to_all && (
                  <span
                    className={CHIP_WARNING}
                    title="Available to every current and future agent"
                  >
                    Available to all
                  </span>
                )}
                {instances[0].agents.map(a => (
                  <span key={a} className={CHIP_NEUTRAL}>{a}</span>
                ))}
                {!instances[0].assigned_to_all && instances[0].agents.length === 0 && (
                  <span className="text-3xs text-status-warning-fg">No agents assigned</span>
                )}
              </div>
            </div>
            <button ref={reopenRef} onClick={() => startEdit(instances[0])} className="text-xs text-link hover:text-link-hover shrink-0">
              Edit
            </button>
          </div>
        </div>
      )}

      {/* No instances + multi-instance */}
      {!isSingleInstance && instances.length === 0 && !editing && (
        <div className="flex items-center justify-between">
          <span className="text-xs text-tertiary">No instances configured.</span>
          <Button ref={reopenRef} variant="primary" size="xs" onClick={startAdd}>
            Add
          </Button>
        </div>
      )}

      {/* The single instance's form takes its display's place; the first
          instance of a multi-instance MCP opens where "No instances
          configured" was. */}
      {editing && (isSingleInstance || (adding && instances.length === 0)) && form}
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
    // The row being edited takes a ring outside its hairline: a second border
    // colour on one element would resolve by stylesheet order.
    <div className={`${CARD_BORDERED} flex items-center gap-3 px-3 py-2.5 ${isEditing ? 'ring-1 ring-action' : ''}`}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-primary">{inst.instance_name}</span>
          <span className="text-xs text-secondary truncate">{summary}</span>
        </div>
        <div className="flex items-center gap-1.5 mt-1 flex-wrap">
          {inst.managed_by === 'system' && (
            <span
              className={CHIP_ACTION}
              title="Platform-managed: OtoDock hosts it. You can rename it or choose its agents. You can't delete it or switch it to self-managed."
            >
              Platform-managed
            </span>
          )}
          {inst.assigned_to_all && (
            <span
              className={CHIP_WARNING}
              title="Available to every current and future agent"
            >
              Available to all
            </span>
          )}
          {inst.agents.map(a => (
            <span key={a} className={CHIP_NEUTRAL}>{a}</span>
          ))}
          {!inst.assigned_to_all && inst.agents.length === 0 && (
            <span className="text-3xs text-status-warning-fg">No agents assigned</span>
          )}
        </div>
      </div>
      <div className="flex gap-1.5 shrink-0">
        <button onClick={onEdit} className="text-xs text-link hover:text-link-hover">Edit</button>
        {inst.managed_by !== 'system' && (
          <button onClick={onDelete} className="text-xs text-status-danger-fg hover:underline">Delete</button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Instance form (add/edit)
// ---------------------------------------------------------------------------

function InstanceForm({
  id,
  returnTo,
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
  id?: string
  /** Takes the focus on close when the button that opened the form is gone. */
  returnTo?: RefObject<HTMLElement | null>
  editing: { id?: number; instance_name: string; field_values: Record<string, string>; agents: string[]; assigned_to_all: boolean; hosted_mode: 'self_managed' | 'hosted'; managed_by?: 'admin' | 'system' }
  setEditing: (v: typeof editing) => void
  fields: McpInstanceField[]
  agents: string[]
  isSingleInstance: boolean
  isPending: boolean
  onSave: () => Promise<boolean>
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
    r => r.direction === PHONE_OUTBOUND && r.enabled,
  )
  // Audio-provider dropdown sources for `stt_provider_select` (transcribe-mcp)
  // and `tts_provider_select` (tts-mcp): the platform's providers, so an
  // instance can bind a specific engine. Blank = platform default.
  const { data: audioProviders } = useAudioProviders()
  const sttProviders = (audioProviders || []).filter(p => p.provider_type === 'stt')
  const ttsProviders = (audioProviders || []).filter(p => p.provider_type === 'tts')
  const [confirmAllOpen, setConfirmAllOpen] = useState(false)
  const formRef = useRef<HTMLDivElement>(null)
  const newFieldIds = useId()
  const { close: closeForm, onKeyDown: formKeyDown } = useInvokedForm(formRef, returnTo)
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
        return 'Must be a valid URL, e.g. http://localhost:9090'
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
    if (Object.keys(errs).length > 0) return
    void onSave().then((saved) => { if (saved) closeForm(onCancel) })
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
    <div
      ref={formRef}
      id={id}
      onKeyDown={(e) => formKeyDown(e, onCancel)}
      className="rounded-card border border-default bg-surface-raised p-4 space-y-3 shadow-elevation-2"
    >
      <h4 className="text-sm font-semibold text-primary">
        {isEdit ? 'Edit' : 'New'} {isSingleInstance ? 'Configuration' : 'Instance'}
      </h4>

      {/* Hosted relay toggle — only for MCPs declaring
          hosted.api_key_relay. The system instance is locked to hosted. */}
      {apiKeyRelay?.available && (
        <div className={`${CARD_BORDERED} p-3 space-y-2`}>
          <div className="flex items-center justify-between">
            <div className="min-w-0">
              <p className="inline text-2xs font-semibold text-tertiary uppercase tracking-wide">Credentials source</p>
              {editing.hosted_mode === 'hosted' && (
                <InfoTip label="Credentials source">Calls go through OtoDock, so no API key is needed.</InfoTip>
              )}
            </div>
            {editing.managed_by === 'system' && (
              <span className={CHIP_ACTION}>Platform-managed</span>
            )}
          </div>
          {editing.managed_by === 'system' ? (
            <p className="text-xs text-secondary">
              Hosted by OtoDock: calls go through OtoDock. This instance can't become
              self-managed. To use your own key, create a separate instance.
            </p>
          ) : (
            <div role="group" aria-label="Credentials source" className="flex gap-2">
              <button
                type="button"
                onClick={() => setEditing({ ...editing, hosted_mode: 'self_managed' })}
                aria-pressed={editing.hosted_mode === 'self_managed'}
                className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} ${editing.hosted_mode === 'self_managed' ? SEGMENT.on : SEGMENT.off}`}
              >
                My own key
              </button>
              <button
                type="button"
                onClick={() => setEditing({ ...editing, hosted_mode: 'hosted' })}
                aria-pressed={editing.hosted_mode === 'hosted'}
                className={`${BUTTON_BASE} ${BUTTON_SIZE.xs} ${editing.hosted_mode === 'hosted' ? SEGMENT.on : SEGMENT.off}`}
              >
                Use OtoDock
              </button>
            </div>
          )}
          {editing.hosted_mode === 'hosted' && (
            <p className="text-3xs text-tertiary italic">
              Hosted via OtoDock.
              Credit is billed for each user
              {apiKeyRelay.billing_setup_url ? (
                <>
                  {' '}(<a href={safeHref(apiKeyRelay.billing_setup_url)} target="_blank" rel="noreferrer" className="text-link underline">set up billing</a>).
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
            <label htmlFor={`${newFieldIds}-name`} className="text-xs text-tertiary">Name</label>
            {editing.managed_by === 'system' ? (
              <p className="mt-1 text-sm text-primary px-2.5 py-1.5">{editing.instance_name}</p>
            ) : (
              <Input
                id={`${newFieldIds}-name`}
                className="mt-1"
                value={editing.instance_name}
                onChange={e => setEditing({ ...editing, instance_name: e.target.value })}
                placeholder="e.g. production-server"
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
        <Button
          variant="primary"
          size="xs"
          onClick={validateAndSave}
          disabled={isPending || !nameValid || !requiredFilled || hasFieldErrors}
        >
          {isPending ? 'Saving...' : 'Save'}
        </Button>
        <Button variant="ghost" size="xs" onClick={() => closeForm(onCancel)}>
          Cancel
        </Button>
      </div>

      {confirmAllOpen && (
        <AlertDialog
          title="Make this instance available to all agents?"
          confirmLabel="Enable for all"
          onConfirm={confirmEnableAll}
          onCancel={() => setConfirmAllOpen(false)}
        >
          Every current and future agent will be able to enable this MCP.
          This instance may hold paid API keys or private credentials.
          Only do this if every agent should use them.
        </AlertDialog>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// SSH Keys section (for MCPs with ssh_key_select fields)
// ---------------------------------------------------------------------------

function SshKeysSection() {
  const dialogs = useDialogs()
  const { data: keys } = useSshKeys()
  const uploadKey = useUploadSshKey()
  const deleteKey = useDeleteSshKey()

  const handleUpload = () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.onchange = () => {
      const file = input.files?.[0]
      if (file) uploadKey.mutateAsync(file).catch(toastError('The key was not uploaded'))
    }
    input.click()
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-xs font-semibold text-secondary uppercase tracking-wider">SSH Keys</h4>
        <Button variant="primary" size="xs" onClick={handleUpload} disabled={uploadKey.isPending}>
          {uploadKey.isPending ? 'Uploading...' : 'Upload Key'}
        </Button>
      </div>
      {keys && keys.length > 0 ? (
        <div className="space-y-1">
          {keys.map(k => (
            <div key={k.name} className="flex items-center justify-between px-3 py-2 rounded-card bg-surface-sunken">
              <div className="flex items-center gap-2 min-w-0">
                <svg className="w-4 h-4 text-tertiary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                </svg>
                <span className="text-sm font-mono text-primary truncate">{k.name}</span>
                <span className="text-xs text-tertiary">{k.size}B</span>
              </div>
              <button
                onClick={async () => {
                  if (await dialogs.confirm({ title: `Delete the key "${k.name}"?`, confirmLabel: 'Delete', tone: 'danger' })) deleteKey.mutateAsync(k.name).catch(toastError('The key was not deleted'))
                }}
                className="text-xs text-status-danger-fg hover:underline shrink-0"
              >
                Delete
              </button>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-tertiary">No SSH keys uploaded.</p>
      )}
    </div>
  )
}
