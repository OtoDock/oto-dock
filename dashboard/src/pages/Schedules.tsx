import { useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTasks, useRunTaskNow, useDeleteTask, usePauseTask, useResumeTask } from '../api/tasks'
import { useAgents } from '../api/agents'
import { useAdminUsers } from '../api/runs'
import { formatNextRun, describeSchedule, browserTimeZone } from '../lib/format'
import { TaskModelChip } from '../components/common/TaskModelChip'
import TransferredFrom from '../components/TransferredFrom'
import { errorText } from '../lib/http/apiJson'
import { useDialogs } from '../components/ui/dialogs'
import { Select } from '../components/ui/Field'
import Button from '../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, BUTTON_DANGER_QUIET, CARD, PAGE_TITLE } from '../components/ui/classes'


export default function Schedules() {
  const dialogs = useDialogs()
  const [agentFilter, setAgentFilter] = useState('')
  const [userFilter, setUserFilter] = useState('')
  // Admin audit page: full-audit view (every user's tasks); agent/user are filters.
  const { data: tasks, isLoading } = useTasks(agentFilter || undefined, { audit: true })
  // "Next" is the browser's clock; the schedule names its own zone when
  // that differs (see AgentSchedules).
  const browserTz = browserTimeZone()
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
    if (!(await dialogs.confirm({ title: `Run "${name}" now?`, confirmLabel: 'Run now' }))) return
    try {
      const result = await runNow.mutateAsync(taskId)
      setFeedback(`Started run: ${result.run_id}`)
      setTimeout(() => navigate(`/runs/${result.run_id}`), 800)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  const handleDelete = async (taskId: string, name: string) => {
    if (!(await dialogs.confirm({ title: `Delete the task "${name}"?`, body: 'This cannot be undone.', confirmLabel: 'Delete', tone: 'danger' }))) return
    try {
      await deleteTask.mutateAsync(taskId)
      setFeedback(`Deleted: ${taskId}`)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  const handlePause = async (taskId: string, name: string) => {
    try {
      await pauseTask.mutateAsync(taskId)
      setFeedback(`Paused: ${name}`)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  const handleResume = async (taskId: string, name: string) => {
    try {
      await resumeTask.mutateAsync(taskId)
      setFeedback(`Resumed: ${name}`)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-end md:justify-between">
        <h1 className={PAGE_TITLE}>Scheduled Tasks</h1>
        {feedback && (
          <p className="text-sm text-status-info-fg bg-status-info-bg px-3 py-1 rounded-card">{feedback}</p>
        )}
      </div>

      {/* Filters */}
      <div className="flex gap-3 items-center flex-wrap">
        <Select
          value={agentFilter}
          onChange={(e) => setAgentFilter(e.target.value)}
          className="sm:w-auto"
        >
          <option value="">All agents</option>
          {agentNames.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </Select>
        <Select
          value={userFilter}
          onChange={(e) => setUserFilter(e.target.value)}
          className="sm:w-auto"
        >
          <option value="">All users</option>
          {(users ?? []).map((u) => (
            <option key={u.sub} value={u.sub}>{u.name}</option>
          ))}
        </Select>
        <span className="text-xs text-secondary ml-auto">{filteredTasks.length} tasks</span>
      </div>

      {/* Desktop table */}
      <div className={`${CARD} hidden md:block overflow-hidden`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
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
                <tr key={task.id} className="border-b border-subtle hover:bg-surface-hover">
                  <td className="px-4 py-3">
                    <div className="font-medium text-primary">{task.name}</div>
                    <TransferredFrom row={task} className="block" />
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-xs text-tertiary font-mono">{task.id}</span>
                      <TaskModelChip task={task} />
                    </div>
                  </td>
                  <td className="px-4 py-3 text-secondary">{task.agent}</td>
                  <td className="px-4 py-3 text-xs text-secondary">
                    <div>{describeSchedule(task, browserTz)}</div>
                    {task.schedule && <div className="font-mono text-tertiary mt-0.5">{task.schedule}</div>}
                    {task.interval_seconds != null && <div className="font-mono text-tertiary mt-0.5">interval {task.interval_seconds}s</div>}
                  </td>
                  <td className="px-4 py-3 text-secondary text-xs">{formatNextRun(task.next_run_time)}</td>
                  <td className="px-4 py-3">
                    <span className={`${BADGE_BASE} ${BADGE_TONE[task.enabled ? 'success' : 'neutral']}`}>{task.enabled ? 'Active' : 'Paused'}</span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {task.can_run && (
                        <Button
                          variant="secondary"
                          size="xs"
                          onClick={() => handleRun(task.id, task.name)}
                          disabled={runNow.isPending}
                        >
                          Run
                        </Button>
                      )}
                      {task.can_pause && (
                        <Button
                          variant="secondary"
                          size="xs"
                          onClick={() => handlePause(task.id, task.name)}
                          disabled={pauseTask.isPending}
                        >
                          Pause
                        </Button>
                      )}
                      {task.can_resume && (
                        <Button
                          variant="secondary"
                          size="xs"
                          onClick={() => handleResume(task.id, task.name)}
                          disabled={resumeTask.isPending}
                        >
                          Resume
                        </Button>
                      )}
                      {task.can_delete && (
                        <button
                          onClick={() => handleDelete(task.id, task.name)}
                          disabled={deleteTask.isPending}
                          className={BUTTON_DANGER_QUIET}
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
        {filteredTasks.length === 0 && <p className="text-sm text-secondary px-4 py-6">No tasks found.</p>}
      </div>

      {/* Mobile cards */}
      <div className="md:hidden space-y-3">
        {filteredTasks.length === 0 && <p className="text-sm text-secondary py-4">No tasks found.</p>}
        {filteredTasks.map((task) => (
          <div key={task.id} className={`${CARD} p-4 space-y-2`}>
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-primary">{task.name}</p>
                <TransferredFrom row={task} className="block" />
                <p className="text-xs text-secondary">{task.agent}</p>
                <div className="flex items-center gap-1.5 flex-wrap mt-0.5">
                  <span className="text-xs text-tertiary font-mono break-all">{task.id}</span>
                  <TaskModelChip task={task} />
                </div>
              </div>
              <div className="flex gap-1 shrink-0">
                <span className={`${BADGE_BASE} ${BADGE_TONE[task.enabled ? 'success' : 'neutral']}`}>{task.enabled ? 'Active' : 'Paused'}</span>
              </div>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
              <span className="min-w-0 break-words"><span className="text-tertiary">Schedule:</span> {describeSchedule(task, browserTz)}</span>
              <span className="min-w-0 break-words"><span className="text-tertiary">Next:</span> {formatNextRun(task.next_run_time)}</span>
            </div>
            {(task.can_run || task.can_pause || task.can_resume || task.can_delete) && (
              <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-subtle">
                {task.can_run && (
                  <Button
                    variant="secondary"
                    size="xs"
                    onClick={() => handleRun(task.id, task.name)}
                    disabled={runNow.isPending}
                  >
                    Run Now
                  </Button>
                )}
                {task.can_pause && (
                  <Button
                    variant="secondary"
                    size="xs"
                    onClick={() => handlePause(task.id, task.name)}
                    disabled={pauseTask.isPending}
                  >
                    Pause
                  </Button>
                )}
                {task.can_resume && (
                  <Button
                    variant="secondary"
                    size="xs"
                    onClick={() => handleResume(task.id, task.name)}
                    disabled={resumeTask.isPending}
                  >
                    Resume
                  </Button>
                )}
                {task.can_delete && (
                  <button
                    onClick={() => handleDelete(task.id, task.name)}
                    disabled={deleteTask.isPending}
                    className={BUTTON_DANGER_QUIET}
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
