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

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-bold text-p-text">Triggers</h1>
          <p className="text-sm text-p-text-secondary">
            Webhook triggers for <span className="font-medium text-p-text">{agent}</span>
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="self-start sm:self-auto shrink-0 px-3 py-1.5 rounded-lg bg-brand text-white text-sm font-medium hover:bg-brand-hover"
        >
          + New trigger
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={scopeFilter}
          onChange={(e) => setScopeFilter(e.target.value as ScopeFilter)}
          className="px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
        >
          <option value="all">All scopes</option>
          <option value="user">My triggers (user)</option>
          <option value="agent">Agent triggers</option>
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
          className="px-3 py-1.5 rounded-lg border border-p-border-light bg-white dark:bg-p-surface text-sm"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
        </select>
        <span className="text-xs text-p-text-secondary ml-auto">
          {filtered.length} of {triggers.length}
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-8 text-center">
          <p className="text-sm text-p-text-secondary">
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
  const [showFire, setShowFire] = useState(false)
  const [showEdit, setShowEdit] = useState(false)
  const pauseM = usePauseTrigger()
  const resumeM = useResumeTrigger()
  const deleteM = useDeleteTrigger()

  // Visual chips: scope (always shown), an extra Vendor pill for
  // subscription-backed triggers, then status. rounded-full + dark variants so
  // they read as distinct pills in both light and dark.
  const chips: { label: string; cls: string }[] = [
    trigger.scope === 'agent'
      ? { label: 'Agent', cls: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300' }
      : { label: 'User', cls: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300' },
  ]
  if (trigger.subscription_id) {
    chips.push({ label: 'Vendor', cls: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300' })
  }
  chips.push(
    trigger.enabled
      ? { label: 'Active', cls: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300' }
      : { label: 'Paused', cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300' },
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
    if (!confirm(`Permanently delete trigger "${trigger.name}"? This cannot be undone.`)) return
    try {
      await deleteM.mutateAsync(trigger.id)
    } catch (e: any) {
      alert(`Delete failed: ${e?.message ?? e}`)
    }
  }

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 space-y-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-medium text-p-text">{trigger.name}</span>
            {chips.map((c) => (
              <span
                key={c.label}
                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${c.cls}`}
              >
                {c.label}
              </span>
            ))}
          </div>
          <p className="text-xs text-p-text-secondary mt-1 font-mono break-all">{trigger.slug}</p>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap sm:justify-end shrink-0">
          {trigger.can_fire && (
            <button
              onClick={() => setShowFire(true)}
              className="px-2 py-1 rounded-sm text-xs font-medium bg-blue-100 text-blue-700 hover:bg-blue-200"
              title="Test fire"
            >
              Test
            </button>
          )}
          {trigger.can_pause && (
            <button
              onClick={() => pauseM.mutate(trigger.id)}
              className="px-2 py-1 rounded-sm text-xs font-medium bg-amber-100 text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:hover:bg-amber-900/50"
            >
              Pause
            </button>
          )}
          {trigger.can_resume && (
            <button
              onClick={() => resumeM.mutate(trigger.id)}
              className="px-2 py-1 rounded-sm text-xs font-medium bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50"
            >
              Resume
            </button>
          )}
          {trigger.can_edit && (
            <button
              onClick={() => setShowEdit(true)}
              className="px-2 py-1 rounded-sm text-xs font-medium bg-p-bg text-p-text hover:bg-p-surface-hover"
            >
              Edit
            </button>
          )}
          {trigger.can_delete && (
            <button
              onClick={onDelete}
              className="px-2 py-1 rounded-sm text-xs font-medium bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50"
            >
              Delete
            </button>
          )}
        </div>
      </div>

      {/* Webhook URL */}
      {webhookUrl && (
        <div className="space-y-1">
          <p className="text-xs text-p-text-light">
            <span className="font-medium text-p-text-secondary">Webhook URL</span>
            {' '}— external systems POST here to fire this trigger
          </p>
          <div className="flex items-center gap-2 bg-p-bg rounded-lg px-3 py-2">
            <code className="text-xs text-p-text-secondary break-all flex-1 min-w-0">{webhookUrl}</code>
            <button
              onClick={onCopyUrl}
              className="text-xs px-2 py-0.5 rounded-sm bg-white dark:bg-p-surface hover:bg-p-surface-hover whitespace-nowrap"
            >
              Copy URL
            </button>
          </div>
        </div>
      )}

      {/* Detail row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
        <div>
          <span className="text-p-text-light">Action</span>
          <p className="text-p-text">
            {trigger.task_id ? `Task: ${trigger.task_name ?? trigger.task_id.slice(0, 8)}` : ''}
            {trigger.task_id && trigger.notify_enabled ? ' + ' : ''}
            {trigger.notify_enabled ? `Notify (${trigger.notify_severity})` : ''}
            {!trigger.task_id && !trigger.notify_enabled ? '—' : ''}
          </p>
        </div>
        <div>
          <span className="text-p-text-light">Source</span>
          <p className="text-p-text">
            {trigger.subscription_id ? 'Vendor subscription' : 'Webhook URL'}
          </p>
        </div>
        <div>
          <span className="text-p-text-light">Fires</span>
          <p className="text-p-text">{trigger.fired_count}</p>
        </div>
        <div>
          <span className="text-p-text-light">Last fired</span>
          <p className="text-p-text">{trigger.last_fired_at ? new Date(trigger.last_fired_at).toLocaleString() : 'never'}</p>
        </div>
        <div>
          <span className="text-p-text-light">Debounce</span>
          <p className="text-p-text">{trigger.debounce_seconds > 0 ? `${trigger.debounce_seconds}s` : 'none'}</p>
        </div>
      </div>

      {trigger.last_error && (
        <div className="text-xs bg-red-50 text-red-700 rounded-sm px-3 py-2">
          Last error: {trigger.last_error}
        </div>
      )}

      {showFire && <FireTestModal trigger={trigger} onClose={() => setShowFire(false)} />}
      {showEdit && <EditTriggerModal trigger={trigger} onClose={() => setShowEdit(false)} />}
    </div>
  )
}
