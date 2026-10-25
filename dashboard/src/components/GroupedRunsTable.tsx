import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { Run } from '../api/runs'
import { groupRunsBySession, getTaskTypeLabel, getTaskTypeStyle } from '../lib/runs'
import StatusBadge from './StatusBadge'
import { formatDuration, formatRelativeTime } from '../lib/format'
import { BADGE_BASE, BADGE_TONE } from './ui/classes'

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
    return <p className="text-sm text-secondary py-4">No runs found.</p>
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-subtle text-left text-xs text-secondary uppercase tracking-wide">
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
                className="border-b border-subtle hover:bg-surface-hover cursor-pointer"
              >
                <td className="py-2 pr-4 font-mono text-xs text-tertiary">{run.id.slice(-8)}</td>
                <td className="py-2 pr-4">
                  <span className="font-medium text-primary">{run.task_id}</span>
                  {group.turnCount > 1 && (
                    <span className={`${BADGE_BASE} ${BADGE_TONE.info} ml-2`}>
                      {group.turnCount} turns
                    </span>
                  )}
                </td>
                {showAgent && <td className="py-2 pr-4 text-secondary">{run.agent}</td>}
                {showType && (
                  <td className="py-2 pr-4">
                    <span className={`${BADGE_BASE} ${getTaskTypeStyle(run.task_type)}`}>
                      {getTaskTypeLabel(run.task_type)}
                    </span>
                  </td>
                )}
                <td className="py-2 pr-4">
                  <StatusBadge status={group.latestStatus} />
                  {run.background_pending ? (
                    <span
                      title="Background work was still running when the run ended. The session was kept for it. Its output is not part of this run."
                      className={`${BADGE_BASE} ${BADGE_TONE.warning} ml-2`}
                    >
                      {run.background_pending} left running
                    </span>
                  ) : null}
                </td>
                <td className="py-2 pr-4 text-secondary">
                  {run.started_at ? formatRelativeTime(run.started_at) : '—'}
                </td>
                <td className="py-2 text-secondary">
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
