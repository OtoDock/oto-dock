import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTasks, useRunTaskNow, useDeleteTask, usePauseTask, useResumeTask } from '../api/tasks'
import { useAgents } from '../api/agents'
import { useAdminUsers } from '../api/runs'
import { formatNextRun, formatCronDescription, formatIntervalDescription } from '../lib/format'

export default function Schedules() {
  const [agentFilter, setAgentFilter] = useState('')
  const [userFilter, setUserFilter] = useState('')
  // Admin audit page: full-audit view (every user's tasks); agent/user are filters.
  const { data: tasks, isLoading } = useTasks(agentFilter || undefined, { audit: true })
  const { data: agents } = useAgents({ all: true })
  const { data: users } = useAdminUsers()
  const runNow = useRunTaskNow()
  const deleteTask = useDeleteTask()
  const pauseTask = usePauseTask()
  const resumeTask = useResumeTask()
  const navigate = useNavigate()
  const [feedback, setFeedback] = useState<string | null>(null)
  const agentNames = useMemo(() => (agents ?? []).map(a => a.name).sort(), [agents])
  const filteredTasks = useMemo(() => {
    if (!tasks || !userFilter) return tasks ?? []
    return tasks.filter(t => t.created_by === userFilter)
  }, [tasks, userFilter])

  const handleRun = async (taskId: string, name: string) => {
    if (!window.confirm(`Run "${name}" now?`)) return
    try {
      const result = await runNow.mutateAsync(taskId)
      setFeedback(`Started run: ${result.run_id}`)
      setTimeout(() => navigate(`/runs/${result.run_id}`), 800)
    } catch (e: any) {
      setFeedback(`Error: ${e.message}`)
    }
  }

  const handleDelete = async (taskId: string, name: string) => {
    if (!window.confirm(`Delete task "${name}"? This cannot be undone.`)) return
    try {
      await deleteTask.mutateAsync(taskId)
      setFeedback(`Deleted: ${taskId}`)
    } catch (e: any) {
      setFeedback(`Error: ${e.message}`)
    }
  }

  const handlePause = async (taskId: string, name: string) => {
    try {
      await pauseTask.mutateAsync(taskId)
      setFeedback(`Paused: ${name}`)
    } catch (e: any) {
      setFeedback(`Error: ${e.message}`)
    }
  }

  const handleResume = async (taskId: string, name: string) => {
    try {
      await resumeTask.mutateAsync(taskId)
      setFeedback(`Resumed: ${name}`)
    } catch (e: any) {
      setFeedback(`Error: ${e.message}`)
    }
  }

  if (isLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-p-text">Scheduled Tasks</h1>
        {feedback && (
          <p className="text-sm text-brand bg-brand-50 px-3 py-1 rounded-sm">{feedback}</p>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <select
          value={userFilter}
          onChange={(e) => setUserFilter(e.target.value)}
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </select>
        <span className="text-xs text-p-text-secondary ml-auto">{filteredTasks.length} tasks</span>
      </div>

      {/* Desktop table */}
      <div className="hidden md:block bg-white dark:bg-p-surface rounded-xl border border-p-border-light overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-p-bg text-left text-xs text-p-text-secondary uppercase tracking-wide border-b border-p-border-light">
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Agent</th>
                <th className="px-4 py-3">Schedule</th>
                <th className="px-4 py-3">Next Run</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredTasks.map((task) => (
                <tr key={task.id} className="border-b border-p-border-light hover:bg-p-surface-hover">
                  <td className="px-4 py-3">
                    <div className="font-medium text-p-text">{task.name}</div>
                    <div className="text-xs text-p-text-light font-mono">{task.id}</div>
                  </td>
                  <td className="px-4 py-3 text-p-text-secondary">{task.agent}</td>
                  <td className="px-4 py-3 text-xs text-p-text-secondary">
                    <div>{formatIntervalDescription(task.interval_seconds) || formatCronDescription(task.schedule) || task.run_at || (task.delay_seconds != null ? `in ${task.delay_seconds}s` : '\u2014')}</div>
                    {task.schedule && <div className="font-mono text-p-text-light mt-0.5">{task.schedule}</div>}
                    {task.interval_seconds != null && <div className="font-mono text-p-text-light mt-0.5">interval {task.interval_seconds}s</div>}
                  </td>
                  <td className="px-4 py-3 text-p-text-secondary text-xs">{formatNextRun(task.next_run_time)}</td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${
                      task.enabled ? 'bg-green-100 text-green-700 dark:bg-green-900/20 dark:text-green-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
                    }`}>{task.enabled ? 'Active' : 'Paused'}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {task.can_run && (
                        <button
                          onClick={() => handleRun(task.id, task.name)}
                          disabled={runNow.isPending}
                          className="px-2 py-1 text-xs rounded-sm bg-brand/10 text-brand hover:bg-brand/20 transition-colors disabled:opacity-50"
                        >
                          Run
                        </button>
                      )}
                      {task.can_pause && (
                        <button
                          onClick={() => handlePause(task.id, task.name)}
                          disabled={pauseTask.isPending}
                          className="px-2 py-1 text-xs rounded-sm bg-amber-100 text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:hover:bg-amber-900/50 transition-colors disabled:opacity-50"
                        >
                          Pause
                        </button>
                      )}
                      {task.can_resume && (
                        <button
                          onClick={() => handleResume(task.id, task.name)}
                          disabled={resumeTask.isPending}
                          className="px-2 py-1 text-xs rounded-sm bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50 transition-colors disabled:opacity-50"
                        >
                          Resume
                        </button>
                      )}
                      {task.can_delete && (
                        <button
                          onClick={() => handleDelete(task.id, task.name)}
                          disabled={deleteTask.isPending}
                          className="px-2 py-1 text-xs rounded-sm bg-p-accent-red/10 text-p-accent-red hover:bg-p-accent-red/20 transition-colors disabled:opacity-50"
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {filteredTasks.length === 0 && <p className="text-sm text-p-text-secondary px-4 py-6">No tasks found.</p>}
      </div>

      {/* Mobile cards */}
      <div className="md:hidden space-y-3">
        {filteredTasks.length === 0 && <p className="text-sm text-p-text-secondary py-4">No tasks found.</p>}
        {filteredTasks.map((task) => (
          <div key={task.id} className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4 space-y-2">
            <div className="flex items-start justify-between">
              <div>
                <p className="text-sm font-medium text-p-text">{task.name}</p>
                <p className="text-xs text-p-text-secondary">{task.agent}</p>
                <p className="text-xs text-p-text-light font-mono mt-0.5">{task.id}</p>
              </div>
              <div className="flex gap-1 shrink-0">
                <span className={`px-2 py-0.5 rounded-sm text-xs font-medium ${
                  task.enabled ? 'bg-green-100 text-green-700 dark:bg-green-900/20 dark:text-green-300' : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
                }`}>{task.enabled ? 'Active' : 'Paused'}</span>
              </div>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-p-text-secondary">
              <span><span className="text-p-text-light">Schedule:</span> {formatIntervalDescription(task.interval_seconds) || formatCronDescription(task.schedule) || task.run_at || '\u2014'}</span>
              <span><span className="text-p-text-light">Next:</span> {formatNextRun(task.next_run_time)}</span>
            </div>
            {(task.can_run || task.can_pause || task.can_resume || task.can_delete) && (
              <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-p-border-light">
                {task.can_run && (
                  <button
                    onClick={() => handleRun(task.id, task.name)}
                    disabled={runNow.isPending}
                    className="px-3 py-1.5 text-xs rounded-lg bg-brand/10 text-brand hover:bg-brand/20 transition-colors disabled:opacity-50"
                  >
                    Run Now
                  </button>
                )}
                {task.can_pause && (
                  <button
                    onClick={() => handlePause(task.id, task.name)}
                    disabled={pauseTask.isPending}
                    className="px-3 py-1.5 text-xs rounded-lg bg-amber-100 text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 dark:hover:bg-amber-900/50 transition-colors disabled:opacity-50"
                  >
                    Pause
                  </button>
                )}
                {task.can_resume && (
                  <button
                    onClick={() => handleResume(task.id, task.name)}
                    disabled={resumeTask.isPending}
                    className="px-3 py-1.5 text-xs rounded-lg bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50 transition-colors disabled:opacity-50"
                  >
                    Resume
                  </button>
                )}
                {task.can_delete && (
                  <button
                    onClick={() => handleDelete(task.id, task.name)}
                    disabled={deleteTask.isPending}
                    className="px-3 py-1.5 text-xs rounded-lg bg-p-accent-red/10 text-p-accent-red hover:bg-p-accent-red/20 transition-colors disabled:opacity-50"
                  >
                    Delete
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
