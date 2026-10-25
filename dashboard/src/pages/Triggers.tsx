import { useState, useMemo } from 'react'
import {
  Trigger,
  useTriggers,
  usePauseTrigger,
  useResumeTrigger,
  useDeleteTrigger,
  useFireTrigger,
} from '../api/triggers'
import { useAgents } from '../api/agents'
import { TaskModelChip } from '../components/common/TaskModelChip'
import TransferredFrom from '../components/TransferredFrom'
import { linkedTaskModel } from '../components/common/TaskModelChip.helpers'
import { toastError } from '../lib/http/apiJson'
import { useDialogs } from '../components/ui/dialogs'
import { Select } from '../components/ui/Field'
import Button from '../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, BUTTON_DANGER_QUIET, CARD, PAGE_TITLE } from '../components/ui/classes'

/**
 * Admin Triggers page — platform-wide list of every trigger across all
 * agents and users. Mirrors admin Notifications / Tasks page UX.
 *
 * Filters: agent, scope, status, source. Admin can mutate any trigger
 * (override on permission flags). Static triggers cannot be deleted.
 */
// The filter menus' values: a pick is read back through these lists, so the
// state keeps its union.
const SCOPE_FILTERS = ['all', 'user', 'agent'] as const
const STATUS_FILTERS = ['all', 'active', 'paused'] as const
const SOURCE_FILTERS = ['all', 'generic', 'vendor'] as const

export default function Triggers() {
  const [agentFilter, setAgentFilter] = useState('')
  const [scopeFilter, setScopeFilter] = useState<(typeof SCOPE_FILTERS)[number]>('all')
  const [statusFilter, setStatusFilter] = useState<(typeof STATUS_FILTERS)[number]>('all')
  // Source filter replaced 'static/mcp/dashboard' (legacy `source`
  // column dropped) with 'generic' vs 'vendor' based on subscription_id.
  const [sourceFilter, setSourceFilter] = useState<(typeof SOURCE_FILTERS)[number]>('all')

  // Admin audit page: full-audit view (every user's triggers); agent/scope are filters.
  const { data: triggers = [], isLoading } = useTriggers({
    agent: agentFilter || undefined,
    scope: scopeFilter === 'all' ? undefined : scopeFilter,
    audit: true,
  })
  const { data: agents } = useAgents({ all: true })
  const agentNames = useMemo(
    () => (agents ?? []).map((a) => a.name).sort(),
    [agents],
  )

  const filtered = useMemo(() => {
    return triggers.filter((t) => {
      if (statusFilter === 'active' && !t.enabled) return false
      if (statusFilter === 'paused' && t.enabled) return false
      if (sourceFilter === 'vendor' && !t.subscription_id) return false
      if (sourceFilter === 'generic' && t.subscription_id) return false
      return true
    })
  }, [triggers, statusFilter, sourceFilter])

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <h1 className={PAGE_TITLE}>Triggers</h1>
      <p className="text-sm text-secondary">
        Every webhook trigger on the platform. Admins can pause, resume or
        delete any trigger here. A webhook URL needs an agent's or a user's
        API key. Create one in the agent's API Keys section or in User
        Settings, Integrations.
      </p>

      <div className="flex flex-wrap gap-2 items-center">
        <Select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="sm:w-auto"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </Select>
        <Select
          value={scopeFilter}
          onChange={(e) => setScopeFilter(SCOPE_FILTERS.find((v) => v === e.target.value) ?? 'all')}
          className="sm:w-auto"
        >
          <option value="all">All scopes</option>
          <option value="user">User</option>
          <option value="agent">Agent</option>
        </Select>
        <Select
          value={statusFilter}
          onChange={(e) => setStatusFilter(STATUS_FILTERS.find((v) => v === e.target.value) ?? 'all')}
          className="sm:w-auto"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
        </Select>
        <Select
          value={sourceFilter}
          onChange={(e) => setSourceFilter(SOURCE_FILTERS.find((v) => v === e.target.value) ?? 'all')}
          className="sm:w-auto"
        >
          <option value="all">All sources</option>
          <option value="generic">Generic webhook</option>
          <option value="vendor">Vendor subscription</option>
        </Select>
        <span className="text-xs text-secondary ml-auto">
          {filtered.length} of {triggers.length}
        </span>
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-secondary py-6 text-center">No triggers match the current filters.</p>
      ) : (
        <div className="space-y-3">
          {filtered.map((t) => (
            <AdminTriggerRow key={t.id} trigger={t} />
          ))}
        </div>
      )}
    </div>
  )
}


function AdminTriggerRow({ trigger }: { trigger: Trigger }) {
  const dialogs = useDialogs()
  const pauseM = usePauseTrigger()
  const resumeM = useResumeTrigger()
  const deleteM = useDeleteTrigger()
  const fireM = useFireTrigger()

  const scopeBadge = trigger.subscription_id
    ? { label: 'Vendor', cls: BADGE_TONE.info }
    : trigger.scope === 'agent'
      ? { label: 'Agent', cls: BADGE_TONE.agent }
      : { label: 'User', cls: BADGE_TONE.neutral }
  const statusBadge = trigger.enabled
    ? { label: 'Active', cls: BADGE_TONE.success }
    : { label: 'Paused', cls: BADGE_TONE.warning }

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
    <div className={`${CARD} p-4`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-primary">{trigger.name}</span>
            <TransferredFrom row={trigger} />
            <span className={`${BADGE_BASE} ${scopeBadge.cls}`}>
              {scopeBadge.label}
            </span>
            <span className={`${BADGE_BASE} ${statusBadge.cls}`}>
              {statusBadge.label}
            </span>
            <span className="text-xs text-secondary">{trigger.agent}</span>
            {trigger.scope === 'user' && trigger.created_by_name && (
              <span className="text-xs text-secondary">by {trigger.created_by_name}</span>
            )}
          </div>
          <p className="text-xs font-mono text-secondary mt-1 break-all">{trigger.slug}</p>
          {trigger.webhook_path && (
            <p className="text-xs text-secondary mt-1 break-all font-mono">
              {window.location.origin}{trigger.webhook_path}
            </p>
          )}
        </div>
        <div className="flex items-center gap-1.5 flex-wrap justify-end">
          <Button
            variant="secondary"
            size="xs"
            onClick={() => { void fireM.mutateAsync({ id: trigger.id, body: {} }).catch(toastError('The trigger was not fired')) }}
          >
            Test
          </Button>
          {trigger.can_pause && (
            <Button
              variant="secondary"
              size="xs"
              onClick={() => { void pauseM.mutateAsync(trigger.id).catch(toastError('The trigger was not paused')) }}
            >
              Pause
            </Button>
          )}
          {trigger.can_resume && (
            <Button
              variant="secondary"
              size="xs"
              onClick={() => { void resumeM.mutateAsync(trigger.id).catch(toastError('The trigger was not resumed')) }}
            >
              Resume
            </Button>
          )}
          {trigger.can_delete && (
            <button
              onClick={onDelete}
              className={BUTTON_DANGER_QUIET}
            >
              Delete
            </button>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-3 text-xs">
        <span><span className="text-tertiary">Action: </span>
          {trigger.task_id ? (
            <>
              Task: {trigger.task_name ?? trigger.task_id.slice(0, 8)}
              {trigger.task_effective_model ? (
                <>{' '}<TaskModelChip task={linkedTaskModel(trigger)} /></>
              ) : null}
            </>
          ) : ''}
          {trigger.app_id ? `App: ${trigger.app_title ?? trigger.app_slug} → ${trigger.handler}` : ''}
          {(trigger.task_id || trigger.app_id) && trigger.notify_enabled ? ' + ' : ''}
          {trigger.notify_enabled ? `Notify` : ''}
          {!trigger.task_id && !trigger.app_id && !trigger.notify_enabled ? '—' : ''}
        </span>
        <span><span className="text-tertiary">Source: </span>
          {trigger.subscription_id ? 'Vendor' : 'Webhook'}
        </span>
        <span><span className="text-tertiary">Fires: </span>{trigger.fired_count}</span>
        <span><span className="text-tertiary">Last: </span>{trigger.last_fired_at ? new Date(trigger.last_fired_at).toLocaleString() : 'never'}</span>
        <span><span className="text-tertiary">Debounce: </span>{trigger.debounce_seconds > 0 ? `${trigger.debounce_seconds}s` : 'none'}</span>
      </div>
      {trigger.last_error && (
        <div className="text-xs bg-status-danger-bg text-status-danger-fg rounded-card px-3 py-2 mt-2">
          Last error: {trigger.last_error}
        </div>
      )}
    </div>
  )
}
