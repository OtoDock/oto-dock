import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useRuns, useAdminUsers } from '../api/runs'
import { useAgents } from '../api/agents'
import { groupRunsBySession, getTaskTypeLabel, getTaskTypeStyle, formatTrigger, isInternalTaskType } from '../lib/runs'
import StatusBadge from '../components/StatusBadge'
import { formatRelativeTime, formatDuration } from '../lib/format'
import { RUN_STATUS } from '../lib/status/run'
import { Select } from '../components/ui/Field'
import Button from '../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, CARD } from '../components/ui/classes'
import { PAGE_TITLE } from '../components/ui/classes'

// The filter's options: every stored run status (the route accepts any).
const STATUSES = ['', ...Object.values(RUN_STATUS)]

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
      <h1 className={PAGE_TITLE}>Task History</h1>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <Select
          value={agent}
          onChange={(e) => { setAgent(e.target.value); setOffset(0) }}
          className="sm:w-auto"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </Select>
        <Select
          value={status}
          onChange={(e) => { setStatus(e.target.value); setOffset(0) }}
          className="sm:w-auto"
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>{s || 'All statuses'}</option>
          ))}
        </Select>
        <Select
          value={userFilter}
          onChange={(e) => { setUserFilter(e.target.value); setOffset(0) }}
          className="sm:w-auto"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </Select>
        <span className="text-xs text-secondary ml-auto">{total} total</span>
      </div>

      {/* Desktop table */}
      <div className={`${CARD} hidden md:block overflow-hidden`}>
        {isLoading ? (
          <p className="text-sm text-secondary px-4 py-6">Loading...</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
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
                      className="border-b border-subtle hover:bg-surface-hover cursor-pointer"
                    >
                      <td className="px-4 py-3 font-mono text-xs text-tertiary">{run.id.slice(-8)}</td>
                      <td className="px-4 py-3">
                        <span className="font-medium">{run.task_id}</span>
                        {group.turnCount > 1 && (
                          <span className={`${BADGE_BASE} ${BADGE_TONE.info} ml-2`}>
                            {group.turnCount} turns
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-secondary">{run.agent}</td>
                      <td className="px-4 py-3">
                        <span className={`${BADGE_BASE} ${getTaskTypeStyle(run.task_type)}`}>
                          {getTaskTypeLabel(run.task_type)}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-secondary text-xs">
                        {formatTrigger(run.trigger_type, run.trigger_source)}
                      </td>
                      <td className="px-4 py-3"><StatusBadge status={group.latestStatus} /></td>
                      <td className="px-4 py-3 text-secondary text-xs">
                        {run.started_at ? formatRelativeTime(run.started_at) : '\u2014'}
                      </td>
                      <td className="px-4 py-3 text-secondary text-xs">
                        {group.totalDuration ? formatDuration(group.totalDuration) : '\u2014'}
                      </td>
                      <td className="px-4 py-3 text-secondary text-xs">
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
          <p className="text-sm text-secondary px-4 py-6">No runs found.</p>
        )}
      </div>

      {/* Mobile cards */}
      <div className="md:hidden space-y-3">
        {isLoading && <p className="text-sm text-secondary py-4">Loading...</p>}
        {!isLoading && groups.length === 0 && <p className="text-sm text-secondary py-4">No runs found.</p>}
        {groups.map((group) => {
          const run = group.representative
          return (
            <div
              key={group.key}
              onClick={() => navigate(`/runs/${run.id}`)}
              className={`${CARD} p-4 cursor-pointer hover:shadow-elevation-2 transition-shadow`}
            >
              <div className="flex items-start justify-between mb-2">
                <div>
                  <p className="text-sm font-medium text-primary">
                    {run.task_id}
                    {group.turnCount > 1 && (
                      <span className={`${BADGE_BASE} ${BADGE_TONE.info} ml-2`}>
                        {group.turnCount} turns
                      </span>
                    )}
                  </p>
                  <p className="text-xs text-secondary">{run.agent}</p>
                </div>
                <StatusBadge status={group.latestStatus} />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
                <span>
                  <span className={`${BADGE_BASE} ${getTaskTypeStyle(run.task_type)}`}>
                    {getTaskTypeLabel(run.task_type)}
                  </span>
                </span>
                <span>{run.started_at ? formatRelativeTime(run.started_at) : '\u2014'}</span>
                {group.totalDuration > 0 && <span>{formatDuration(group.totalDuration)}</span>}
                {group.totalCost > 0 && <span>${group.totalCost.toFixed(4)}</span>}
              </div>
              <p className="text-xs text-tertiary font-mono mt-1">{run.id.slice(-8)}</p>
            </div>
          )
        })}
      </div>

      {/* Pagination */}
      {total > limit && (
        <div className="flex items-center gap-3 text-sm">
          <Button
            variant="secondary"
            size="xs"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
          >
            Previous
          </Button>
          <span className="text-secondary">
            {offset + 1}–{Math.min(offset + limit, total)} of {total}
          </span>
          <Button
            variant="secondary"
            size="xs"
            disabled={offset + limit >= total}
            onClick={() => setOffset(offset + limit)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  )
}
