import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useStats, useSchedules } from '../api/tasks'
import { useRuns } from '../api/runs'
import GroupedRunsTable from '../components/GroupedRunsTable'
import { formatRelativeTime, formatNextRun } from '../lib/format'
import { CARD } from '../components/ui/classes'
import { PAGE_TITLE } from '../components/ui/classes'

function StatCard({ label, value, color = 'text-primary' }: {
  label: string
  value: number | string
  color?: string
}) {
  return (
    <div className={`${CARD} p-4`}>
      <p className="text-xs text-secondary uppercase tracking-wide">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${color}`}>{value}</p>
    </div>
  )
}

function LiveDuration({ startedAt }: { startedAt: string | null }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!startedAt) return
    const tick = () => {
      setElapsed(Math.floor((Date.now() - new Date(startedAt).getTime()) / 1000))
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [startedAt])
  return <span>{elapsed}s</span>
}

export default function Overview() {
  const { data: stats } = useStats()
  const { data: schedules } = useSchedules()
  // Admin overview: platform-wide running/recent runs across all users (audit).
  const { data: runningData } = useRuns({ status: 'running', limit: 20, audit: true })
  const { data: recentData } = useRuns({ limit: 30, audit: true })
  const navigate = useNavigate()

  const upcoming = (schedules ?? [])
    .filter((j) => j.next_run_time)
    .sort((a, b) => (a.next_run_time! < b.next_run_time! ? -1 : 1))
    .slice(0, 5)

  return (
    <div className="space-y-6">
      <h1 className={PAGE_TITLE}>Overview</h1>

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Scheduled Tasks" value={stats?.scheduled_tasks ?? '—'} />
        <StatCard label="Triggers" value="—" />
        <StatCard label="Runs Today" value={stats?.total_today ?? '—'} />
        <StatCard
          label="Running Now"
          value={stats?.running ?? '—'}
          color={stats?.running ? 'text-status-info-fg' : 'text-primary'}
        />
      </div>

      {/* Running tasks */}
      {(runningData?.runs?.length ?? 0) > 0 && (
        <div className={`${CARD} p-4`}>
          <h2 className="text-sm font-semibold text-primary mb-3">Running Now</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-secondary uppercase border-b border-subtle">
                <th className="pb-2 pr-4">Task</th>
                <th className="pb-2 pr-4">Agent</th>
                <th className="pb-2 pr-4">Started</th>
                <th className="pb-2">Elapsed</th>
              </tr>
            </thead>
            <tbody>
              {runningData?.runs.map((run) => (
                <tr
                  key={run.id}
                  className="hover:bg-surface-hover cursor-pointer"
                  onClick={() => {
                    navigate(`/runs/${run.id}`)
                  }}
                >
                  <td className="py-2 pr-4 font-medium">{run.task_id}</td>
                  <td className="py-2 pr-4 text-secondary">{run.agent}</td>
                  <td className="py-2 pr-4 text-secondary">
                    {run.started_at ? formatRelativeTime(run.started_at) : '—'}
                  </td>
                  <td className="py-2 text-status-info-fg font-mono text-xs">
                    <LiveDuration startedAt={run.started_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Recent runs */}
        <div className={`${CARD} p-4`}>
          <h2 className="text-sm font-semibold text-primary mb-3">Recent Runs</h2>
          <GroupedRunsTable runs={recentData?.runs ?? []} maxGroups={10} />
        </div>

        {/* Upcoming */}
        <div className={`${CARD} p-4`}>
          <h2 className="text-sm font-semibold text-primary mb-3">Upcoming</h2>
          {upcoming.length === 0 ? (
            <p className="text-sm text-secondary">No scheduled jobs.</p>
          ) : (
            <ul className="space-y-2">
              {upcoming.map((job) => (
                <li key={job.id} className="flex justify-between text-sm">
                  <span className="font-medium text-primary">{job.name}</span>
                  <span className="text-secondary text-xs">
                    {formatNextRun(job.next_run_time)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
