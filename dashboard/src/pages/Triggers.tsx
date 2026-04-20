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

/**
 * Admin Triggers page — platform-wide list of every trigger across all
 * agents and users. Mirrors admin Notifications / Tasks page UX.
 *
 * Filters: agent, scope, status, source. Admin can mutate any trigger
 * (override on permission flags). Static triggers cannot be deleted.
 */
export default function Triggers() {
  const [agentFilter, setAgentFilter] = useState('')
  const [scopeFilter, setScopeFilter] = useState<'all' | 'user' | 'agent'>('all')
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'paused'>('all')
  // Source filter replaced 'static/mcp/dashboard' (legacy `source`
  // column dropped) with 'generic' vs 'vendor' based on subscription_id.
  const [sourceFilter, setSourceFilter] = useState<'all' | 'generic' | 'vendor'>('all')

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

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-p-text">Triggers</h1>
      <p className="text-sm text-p-text-secondary">
        Platform-wide webhook triggers. Admins can pause / resume / delete any
        trigger here. Webhook URLs require a per-agent or per-user API key
        (see the agent's API Keys section or User Settings → API Keys).
      </p>

      <div className="flex flex-wrap gap-2 items-center">
        <select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <select
          value={scopeFilter}
          onChange={(e) => setScopeFilter(e.target.value as any)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface"
        >
          <option value="all">All scopes</option>
          <option value="user">User</option>
          <option value="agent">Agent</option>
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as any)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="paused">Paused</option>
        </select>
        <select
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value as any)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface"
        >
          <option value="all">All sources</option>
          <option value="generic">Generic webhook</option>
          <option value="vendor">Vendor subscription</option>
        </select>
        <span className="text-xs text-p-text-secondary ml-auto">
          {filtered.length} of {triggers.length}
        </span>
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-p-text-secondary py-6 text-center">No triggers match the current filters.</p>
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
  const pauseM = usePauseTrigger()
  const resumeM = useResumeTrigger()
  const deleteM = useDeleteTrigger()
  const fireM = useFireTrigger()

  const scopeBadge = trigger.subscription_id
    ? { label: 'Vendor', cls: 'bg-purple-100 text-purple-700' }
    : trigger.scope === 'agent'
      ? { label: 'Agent', cls: 'bg-blue-100 text-blue-700' }
      : { label: 'User', cls: 'bg-emerald-100 text-emerald-700' }
  const statusBadge = trigger.enabled
    ? { label: 'Active', cls: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' }
    : { label: 'Paused', cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400' }

  const onDelete = async () => {
    if (!confirm(`Permanently delete trigger "${trigger.name}"? This cannot be undone.`)) return
    try {
      await deleteM.mutateAsync(trigger.id)
    } catch (e: any) {
      alert(`Delete failed: ${e?.message ?? e}`)
    }
  }

  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-p-text">{trigger.name}</span>
            <span className={`px-2 py-0.5 rounded-sm text-xs font-medium ${scopeBadge.cls}`}>
              {scopeBadge.label}
            </span>
            <span className={`px-2 py-0.5 rounded-sm text-xs font-medium ${statusBadge.cls}`}>
              {statusBadge.label}
            </span>
            <span className="text-xs text-p-text-secondary">{trigger.agent}</span>
            {trigger.scope === 'user' && trigger.created_by_name && (
              <span className="text-xs text-p-text-secondary">by {trigger.created_by_name}</span>
            )}
          </div>
          <p className="text-xs font-mono text-p-text-secondary mt-1 break-all">{trigger.slug}</p>
          {trigger.webhook_path && (
            <p className="text-xs text-p-text-secondary mt-1 break-all font-mono">
              {window.location.origin}{trigger.webhook_path}
            </p>
          )}
        </div>
        <div className="flex items-center gap-1.5 flex-wrap justify-end">
          <button
            onClick={() => fireM.mutate({ id: trigger.id, body: {} })}
            className="px-2 py-1 rounded-sm text-xs font-medium bg-blue-100 text-blue-700 hover:bg-blue-200"
          >
            Test
          </button>
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
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-3 text-xs">
        <span><span className="text-p-text-light">Action: </span>
          {trigger.task_id ? `Task` : ''}
          {trigger.task_id && trigger.notify_enabled ? ' + ' : ''}
          {trigger.notify_enabled ? `Notify` : ''}
          {!trigger.task_id && !trigger.notify_enabled ? '—' : ''}
        </span>
        <span><span className="text-p-text-light">Source: </span>
          {trigger.subscription_id ? 'Vendor' : 'Webhook'}
        </span>
        <span><span className="text-p-text-light">Fires: </span>{trigger.fired_count}</span>
        <span><span className="text-p-text-light">Last: </span>{trigger.last_fired_at ? new Date(trigger.last_fired_at).toLocaleString() : 'never'}</span>
        <span><span className="text-p-text-light">Debounce: </span>{trigger.debounce_seconds > 0 ? `${trigger.debounce_seconds}s` : 'none'}</span>
      </div>
      {trigger.last_error && (
        <div className="text-xs bg-red-50 text-red-700 rounded-sm px-3 py-2 mt-2">
          Last error: {trigger.last_error}
        </div>
      )}
    </div>
  )
}
