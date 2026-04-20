import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Run } from '../api/runs'
import { groupRunsBySession, getTaskTypeLabel, getTaskTypeStyle } from '../lib/runs'
import StatusBadge from './StatusBadge'
import { formatDuration, formatRelativeTime } from '../lib/format'

interface GroupedRunsTableProps {
  runs: Run[]
  showAgent?: boolean
  showType?: boolean
  maxGroups?: number
}

export default function GroupedRunsTable({ runs, showAgent = true, showType = false, maxGroups }: GroupedRunsTableProps) {
  const navigate = useNavigate()

  const groups = useMemo(() => {
    const all = groupRunsBySession(runs)
    return maxGroups ? all.slice(0, maxGroups) : all
  }, [runs, maxGroups])

  if (groups.length === 0) {
    return <p className="text-sm text-p-text-secondary py-4">No runs found.</p>
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-p-border-light text-left text-xs text-p-text-secondary uppercase tracking-wide">
            <th className="pb-2 pr-4">Run ID</th>
            <th className="pb-2 pr-4">Task</th>
            {showAgent && <th className="pb-2 pr-4">Agent</th>}
            {showType && <th className="pb-2 pr-4">Type</th>}
            <th className="pb-2 pr-4">Status</th>
            <th className="pb-2 pr-4">Started</th>
            <th className="pb-2">Duration</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => {
            const run = group.representative
            return (
              <tr
                key={group.key}
                onClick={() => {
                  navigate(`/runs/${run.id}`)
                }}
                className="border-b border-p-border-light hover:bg-p-surface-hover cursor-pointer"
              >
                <td className="py-2 pr-4 font-mono text-xs text-p-text-light">{run.id.slice(-8)}</td>
                <td className="py-2 pr-4">
                  <span className="font-medium text-p-text">{run.task_id}</span>
                  {group.turnCount > 1 && (
                    <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded-sm text-xs bg-brand-100 text-brand">
                      {group.turnCount} turns
                    </span>
                  )}
                </td>
                {showAgent && <td className="py-2 pr-4 text-p-text-secondary">{run.agent}</td>}
                {showType && (
                  <td className="py-2 pr-4">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${getTaskTypeStyle(run.task_type)}`}>
                      {getTaskTypeLabel(run.task_type)}
                    </span>
                  </td>
                )}
                <td className="py-2 pr-4">
                  <StatusBadge status={group.latestStatus} />
                </td>
                <td className="py-2 pr-4 text-p-text-secondary">
                  {run.started_at ? formatRelativeTime(run.started_at) : '—'}
                </td>
                <td className="py-2 text-p-text-secondary">
                  {run.duration_ms ? formatDuration(run.duration_ms) : '—'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
