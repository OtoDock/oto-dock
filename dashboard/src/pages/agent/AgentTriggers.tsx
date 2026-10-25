import { useState, useMemo } from 'react'
import { useParams } from 'react-router-dom'
import {
  Trigger,
  useTriggers,
  useDeleteTrigger,
  usePauseTrigger,
  useResumeTrigger,
} from '../../api/triggers'
import { useAuth } from '../../contexts/AuthContext'
import { canManageAgent } from '../../lib/permissions'
import { CreateTriggerModal, EditTriggerModal, FireTestModal } from './AgentTriggers.modals'
import { AgentApiKeysSection } from './AgentTriggers.apiKeys'
import { TaskModelChip } from '../../components/common/TaskModelChip'
import TransferredFrom from '../../components/TransferredFrom'
import { linkedTaskModel } from '../../components/common/TaskModelChip.helpers'
import { toastError } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import Button from '../../components/ui/Button'
import { Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD, BUTTON_DANGER_QUIET } from '../../components/ui/classes'

type ScopeFilter = 'all' | 'user' | 'agent'
type StatusFilter = 'all' | 'active' | 'paused'

export default function AgentTriggers() {
  const { name } = useParams<{ name: string }>()
  const agent = name || ''
  const { user } = useAuth()
  const canManage = canManageAgent(user, agent)
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [showCreate, setShowCreate] = useState(false)

  const { data: triggers = [], isLoading } = useTriggers({ agent })

  const filtered = useMemo(() => {
    return triggers.filter((t) => {
      if (scopeFilter !== 'all' && t.scope !== scopeFilter) return false
      if (statusFilter === 'active' && !t.enabled) return false
      if (statusFilter === 'paused' && t.enabled) return false
      return true
    })
  }, [triggers, scopeFilter, statusFilter])

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold font-display text-primary">Triggers</h1>
          <p className="text-sm text-secondary">
            Webhook triggers for <span className="font-medium text-primary">{agent}</span>
          </p>
        </div>
        <Button
          variant="primary"
          className="self-start sm:self-auto shrink-0"
          onClick={() => setShowCreate(true)}
        >
          + New trigger
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={scopeFilter}
          onChange={(e) => setScopeFilter(e.target.value as ScopeFilter)}
          className="max-w-max"
        >
          <option value="all">All scopes</option>
          <option value="user">My triggers</option>
          <option value="agent">Agent triggers</option>
        </Select>
        <Select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
          className="max-w-max"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
        </Select>
        <span className="text-xs text-secondary ml-auto">
          {filtered.length} of {triggers.length}
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className={`${CARD} p-8 text-center`}>
          <p className="text-sm text-tertiary">
            {triggers.length === 0
              ? 'No triggers configured. Create one to wire up a webhook.'
              : 'No triggers match the current filters.'}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((t) => (
            <TriggerRow key={t.id} trigger={t} />
          ))}
        </div>
      )}

      {showCreate && (
        <CreateTriggerModal agent={agent} onClose={() => setShowCreate(false)} canCreateAgentScope={canManage} />
      )}

      {canManage && <AgentApiKeysSection agent={agent} />}
    </div>
  )
}


// =====================================================================
// Trigger row
// =====================================================================

function TriggerRow({ trigger }: { trigger: Trigger }) {
  const dialogs = useDialogs()
  const [showFire, setShowFire] = useState(false)
  const [showEdit, setShowEdit] = useState(false)
  const pauseM = usePauseTrigger()
  const resumeM = useResumeTrigger()
  const deleteM = useDeleteTrigger()

  // Visual chips: scope (always shown), an extra Vendor pill for
  // subscription-backed triggers, then status.
  const chips: { label: string; cls: string }[] = [
    trigger.scope === 'agent'
      ? { label: 'Agent', cls: BADGE_TONE.agent }
      : { label: 'User', cls: BADGE_TONE.neutral },
  ]
  if (trigger.subscription_id) {
    chips.push({ label: 'Vendor', cls: BADGE_TONE.info })
  }
  chips.push(
    trigger.enabled
      ? { label: 'Active', cls: BADGE_TONE.success }
      : { label: 'Paused', cls: BADGE_TONE.neutral },
  )

  const webhookUrl = trigger.webhook_path
    ? `${window.location.origin}${trigger.webhook_path}`
    : null

  const onCopyUrl = async () => {
    if (!webhookUrl) return
    try {
      await navigator.clipboard.writeText(webhookUrl)
    } catch { /* ignore */ }
  }

  const onDelete = async () => {
    if (!(await dialogs.confirm({
      title: `Delete the trigger "${trigger.name}"?`,
      body: 'This cannot be undone.',
      confirmLabel: 'Delete',
      tone: 'danger',
    }))) return
    await deleteM.mutateAsync(trigger.id).catch(toastError('The trigger was not deleted'))
  }

  return (
    <div className={`${CARD} p-4 space-y-3`}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-medium text-primary">{trigger.name}</span>
            <TransferredFrom row={trigger} />
            {chips.map((c) => (
              <span
                key={c.label}
                className={`${BADGE_BASE} ${c.cls}`}
              >
                {c.label}
              </span>
            ))}
          </div>
          <p className="text-xs text-secondary mt-1 font-mono break-all">{trigger.slug}</p>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap sm:justify-end shrink-0">
          {trigger.can_fire && (
            <Button size="xs" onClick={() => setShowFire(true)} title="Test fire">
              Test
            </Button>
          )}
          {trigger.can_pause && (
            <Button size="xs" onClick={() => { void pauseM.mutateAsync(trigger.id).catch(toastError('The trigger was not paused')) }}>
              Pause
            </Button>
          )}
          {trigger.can_resume && (
            <Button size="xs" onClick={() => { void resumeM.mutateAsync(trigger.id).catch(toastError('The trigger was not resumed')) }}>
              Resume
            </Button>
          )}
          {trigger.can_edit && (
            <Button size="xs" onClick={() => setShowEdit(true)}>
              Edit
            </Button>
          )}
          {trigger.can_delete && (
            <button type="button" className={BUTTON_DANGER_QUIET} onClick={onDelete}>
              Delete
            </button>
          )}
        </div>
      </div>

      {/* Webhook URL */}
      {webhookUrl && (
        <div className="space-y-1">
          <p className="text-xs text-tertiary">
            <span className="font-medium text-secondary">Webhook URL</span>
            {': '}other systems send a POST request to it to fire this trigger
          </p>
          <div className="flex items-center gap-2 bg-surface-sunken rounded-card px-3 py-2">
            <code className="text-xs text-secondary break-all flex-1 min-w-0">{webhookUrl}</code>
            <Button size="xs" onClick={onCopyUrl} className="whitespace-nowrap">
              Copy URL
            </Button>
          </div>
        </div>
      )}

      {/* Detail row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
        <div>
          <span className="text-tertiary">Action</span>
          <p className="text-primary">
            {trigger.task_id ? (
              <>
                Task: {trigger.task_name ?? trigger.task_id.slice(0, 8)}
                {trigger.task_effective_model ? (
                  <>{' '}<TaskModelChip task={linkedTaskModel(trigger)} /></>
                ) : null}
              </>
            ) : ''}
            {trigger.app_id ? (
              <>
                App: <a href={`/apps/${trigger.app_id}`} className="text-link hover:text-link-hover hover:underline">{trigger.app_title ?? trigger.app_slug}</a>
                {' → '}<code className="font-mono text-2xs">{trigger.handler}</code>
              </>
            ) : ''}
            {(trigger.task_id || trigger.app_id) && trigger.notify_enabled ? ' + ' : ''}
            {trigger.notify_enabled ? `Notify, ${trigger.notify_severity}` : ''}
            {!trigger.task_id && !trigger.app_id && !trigger.notify_enabled ? '—' : ''}
          </p>
        </div>
        <div>
          <span className="text-tertiary">Source</span>
          <p className="text-primary">
            {trigger.subscription_id ? 'Vendor subscription' : 'Webhook URL'}
          </p>
        </div>
        <div>
          <span className="text-tertiary">Fires</span>
          <p className="text-primary">{trigger.fired_count}</p>
        </div>
        <div>
          <span className="text-tertiary">Last fired</span>
          <p className="text-primary">{trigger.last_fired_at ? new Date(trigger.last_fired_at).toLocaleString() : 'never'}</p>
        </div>
        <div>
          <span className="text-tertiary">Debounce</span>
          <p className="text-primary">{trigger.debounce_seconds > 0 ? `${trigger.debounce_seconds}s` : 'none'}</p>
        </div>
      </div>

      {trigger.last_error && (
        <div className="text-xs bg-status-danger-bg text-status-danger-fg border border-status-danger-border rounded-card px-3 py-2">
          Last error: {trigger.last_error}
        </div>
      )}

      {showFire && <FireTestModal trigger={trigger} onClose={() => setShowFire(false)} />}
      {showEdit && <EditTriggerModal trigger={trigger} onClose={() => setShowEdit(false)} />}
    </div>
  )
}
