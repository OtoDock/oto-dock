import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useRuns, useAdminUsers } from '../api/runs'
import { useAgents } from '../api/agents'
import { groupRunsBySession, getTaskTypeLabel, getTaskTypeStyle, formatTrigger, isInternalTaskType } from '../lib/runs'
import StatusBadge from '../components/StatusBadge'
import { formatRelativeTime, formatDuration } from '../lib/format'

const STATUSES = ['', 'pending', 'running', 'completed', 'failed', 'cancelled']

export default function History() {
  const navigate = useNavigate()
  const [agent, setAgent] = useState('')
  const [status, setStatus] = useState('')
  const [userFilter, setUserFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const limit = 100

  const { data: agents } = useAgents({ all: true })
  const { data: users } = useAdminUsers()
  const agentNames = useMemo(() => (agents ?? []).map(a => a.name).sort(), [agents])

  // Admin audit page: full-audit view (every user's runs); agent/user are filters.
  const { data, isLoading } = useRuns({
    agent: agent || undefined,
    status: status || undefined,
    created_by: userFilter || undefined,
    audit: true,
    limit,
    offset,
  })

  const total = data?.total ?? 0
  // Hide internal task types (if any are ever reintroduced) from the user-facing task
  // history. They're system-initiated and surface in admin audit views only.
  const runs = useMemo(
    () => (data?.runs ?? []).filter(r => !isInternalTaskType(r.task_type)),
    [data?.runs],
  )

  const groups = useMemo(() => groupRunsBySession(runs), [runs])

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-p-text">Task History</h1>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <select
          value={agent}
          onChange={(e) => { setAgent(e.target.value); setOffset(0) }}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => { setStatus(e.target.value); setOffset(0) }}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s || 'All statuses'}</option>
          ))}
        </select>
        <select
          value={userFilter}
          onChange={(e) => { setUserFilter(e.target.value); setOffset(0) }}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </select>
        <span className="text-xs text-p-text-secondary ml-auto">{total} total</span>
      </div>

      {/* Desktop table */}
      <div className="hidden md:block bg-white dark:bg-p-surface rounded-xl border border-p-border-light overflow-hidden">
        {isLoading ? (
          <p className="text-sm text-p-text-secondary px-4 py-6">Loading...</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-p-bg text-left text-xs text-p-text-secondary uppercase tracking-wide border-b border-p-border-light">
                  <th className="px-4 py-3">Run</th>
                  <th className="px-4 py-3">Task</th>
                  <th className="px-4 py-3">Agent</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Trigger</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Started</th>
                  <th className="px-4 py-3">Duration</th>
                  <th className="px-4 py-3">Cost</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => {
                  const run = group.representative
                  return (
                    <tr
                      key={group.key}
                      onClick={() => navigate(`/runs/${run.id}`)}
                      className="border-b border-p-border-light hover:bg-p-surface-hover cursor-pointer"
                    >
                      <td className="px-4 py-3 font-mono text-xs text-p-text-light">{run.id.slice(-8)}</td>
                      <td className="px-4 py-3">
                        <span className="font-medium">{run.task_id}</span>
                        {group.turnCount > 1 && (
                          <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-sm text-xs bg-brand-100 text-brand">
                            {group.turnCount} turns
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary">{run.agent}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${getTaskTypeStyle(run.task_type)}`}>
                          {getTaskTypeLabel(run.task_type)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary text-xs">
                        {formatTrigger(run.trigger_type, run.trigger_source)}
                      </td>
                      <td className="px-4 py-3"><StatusBadge status={group.latestStatus} /></td>
                      <td className="px-4 py-3 text-p-text-secondary text-xs">
                        {run.started_at ? formatRelativeTime(run.started_at) : '\u2014'}
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary text-xs">
                        {group.totalDuration ? formatDuration(group.totalDuration) : '\u2014'}
                      </td>
                      <td className="px-4 py-3 text-p-text-secondary text-xs">
                        {group.totalCost ? `$${group.totalCost.toFixed(4)}` : '\u2014'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        {!isLoading && groups.length === 0 && (
          <p className="text-sm text-p-text-secondary px-4 py-6">No runs found.</p>
        )}
      </div>

      {/* Mobile cards */}
      <div className="md:hidden space-y-3">
        {isLoading && <p className="text-sm text-p-text-secondary py-4">Loading...</p>}
        {!isLoading && groups.length === 0 && <p className="text-sm text-p-text-secondary py-4">No runs found.</p>}
        {groups.map((group) => {
          const run = group.representative
          return (
            <div
              key={group.key}
              onClick={() => navigate(`/runs/${run.id}`)}
              className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 cursor-pointer hover:shadow-xs transition-shadow"
            >
              <div className="flex items-start justify-between mb-2">
                <div>
                  <p className="text-sm font-medium text-p-text">
                    {run.task_id}
                    {group.turnCount > 1 && (
                      <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-sm text-xs bg-brand-100 text-brand">
                        {group.turnCount} turns
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-p-text-secondary">{run.agent}</p>
                </div>
                <StatusBadge status={group.latestStatus} />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-p-text-secondary">
                <span>
                  <span className={`inline-flex items-center px-1.5 py-0.5 rounded-sm font-medium ${getTaskTypeStyle(run.task_type)}`}>
                    {getTaskTypeLabel(run.task_type)}
                  </span>
                </span>
                <span>{run.started_at ? formatRelativeTime(run.started_at) : '\u2014'}</span>
                {group.totalDuration > 0 && <span>{formatDuration(group.totalDuration)}</span>}
                {group.totalCost > 0 && <span>${group.totalCost.toFixed(4)}</span>}
              </div>
              <p className="text-xs text-p-text-light font-mono mt-1">{run.id.slice(-8)}</p>
            </div>
          )
        })}
      </div>

      {/* Pagination */}
      {total > limit && (
        <div className="flex items-center gap-3 text-sm">
          <button
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
            className="px-3 py-1 rounded-sm border border-p-border-light disabled:opacity-40"
          >
            Previous
          </button>
          <span className="text-p-text-secondary">
            {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <button
            disabled={offset + limit >= total}
            onClick={() => setOffset(offset + limit)}
            className="px-3 py-1 rounded-sm border border-p-border-light disabled:opacity-40"
          >
            Next
          </button>
        </div>
      )}
    </div>
  )
}
