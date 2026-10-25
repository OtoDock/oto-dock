import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useTasks, useRunTaskNow, useDeleteTask, usePauseTask, useResumeTask, type Task } from '../../api/tasks'
import { formatNextRun, describeSchedule, browserTimeZone } from '../../lib/format'
import { useAuth } from '../../contexts/AuthContext'
import { ScopeFilterSelect, type ScopeFilterValue } from '../../components/ScopeFilterSelect'
import { TaskModelChip } from '../../components/common/TaskModelChip'
import TransferredFrom from '../../components/TransferredFrom'
import { errorText } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import { isReachableMachine } from '../../lib/status/machine'
import { TaskSettingsDialog } from './AgentSchedules.modals'
import { AUTO_REVIEW_MODE } from '../../lib/engines/modes'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, CARD, BUTTON_DANGER_QUIET } from '../../components/ui/classes'

/** The task's own machine beside its model chip: "on <name>", offline said. */
function TaskMachine({ task }: { task: Task }) {
  if (!task.target_machine) return null
  const offline = !!task.target_machine_state && !isReachableMachine(task.target_machine_state)
  return (
    <span className="text-3xs text-tertiary min-w-0 break-words" data-testid="task-machine">
      on {task.target_machine_label || task.target_machine}{offline ? ', offline' : ''}
    </span>
  )
}

/** A task that runs in Auto: the engine's own reviewer checks what needs approval. */
function TaskAutoChip({ task }: { task: Task }) {
  if (task.permission_mode !== AUTO_REVIEW_MODE) return null
  return (
    <span className={`px-1.5 py-0.5 rounded-sm text-2xs font-medium ${BADGE_TONE.action}`}
          title="Auto: a tool that needs approval is checked by the engine's own reviewer first">Auto</span>
  )
}

const GEAR = (
  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
  </svg>
)

export default function AgentSchedules() {
  const dialogs = useDialogs()
  const { name } = useParams<{ name: string }>()
  const { data: tasks, isLoading } = useTasks(name)
  // "Next" is the browser's clock; the schedule names its own zone when
  // that differs, so "Daily at 08:00" and "in 3h" never read as a mismatch.
  const browserTz = browserTimeZone()
  const runNow = useRunTaskNow()
  const deleteTask = useDeleteTask()
  const pauseTask = usePauseTask()
  const resumeTask = useResumeTask()
  const navigate = useNavigate()
  const [feedback, setFeedback] = useState<string | null>(null)
  const { user } = useAuth()
  const [scopeFilter, setScopeFilter] = useState<ScopeFilterValue>('all')
  // The task whose settings dialog is open.
  const [settingsFor, setSettingsFor] = useState<Task | null>(null)
  // Pause and resume carry the edit permission (the server's delete
  // authority): one of the two is offered whenever the person may edit.
  const canEdit = (task: Task) => task.can_pause || task.can_resume
  // user-scoped tasks show the owner's name; agent-scoped show "Agent".
  const scopeName = (scope: string) =>
    scope === 'agent' ? 'Agent' : user?.display_name || user?.name || 'You'
  const allTasks = tasks ?? []
  const filtered = allTasks.filter((t) => scopeFilter === 'all' || t.scope === scopeFilter)

  const handleRun = async (taskId: string, taskName: string) => {
    if (!(await dialogs.confirm({ title: `Run "${taskName}" now?`, confirmLabel: 'Run now' }))) return
    try {
      const result = await runNow.mutateAsync(taskId)
      setFeedback(`Started run: ${result.run_id}`)
      setTimeout(() => navigate(`/runs/${result.run_id}`), 800)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  const handleDelete = async (taskId: string, taskName: string) => {
    if (!(await dialogs.confirm({ title: `Delete the task "${taskName}"?`, body: 'This cannot be undone.', confirmLabel: 'Delete', tone: 'danger' }))) return
    try {
      await deleteTask.mutateAsync(taskId)
      setFeedback(`Deleted: ${taskId}`)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  const handlePause = async (taskId: string, taskName: string) => {
    try {
      await pauseTask.mutateAsync(taskId)
      setFeedback(`Paused: ${taskName}`)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  const handleResume = async (taskId: string, taskName: string) => {
    try {
      await resumeTask.mutateAsync(taskId)
      setFeedback(`Resumed: ${taskName}`)
    } catch (e) {
      setFeedback(`Error: ${errorText(e)}`)
    }
  }

  if (isLoading) return <p className="text-sm text-secondary">Loading...</p>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold font-display text-primary">Scheduled Tasks</h1>
        {feedback && (
          <p className="text-sm text-status-info-fg bg-status-info-bg px-3 py-1 rounded-control">{feedback}</p>
        )}
      </div>

      {/* Scope filter */}
      <div className="flex items-center gap-2 flex-wrap">
        <ScopeFilterSelect value={scopeFilter} onChange={setScopeFilter} />
        <span className="text-xs text-secondary ml-auto">
          {filtered.length} of {allTasks.length}
        </span>
      </div>

      {/* Desktop table */}
      <div className={`hidden md:block ${CARD} overflow-hidden`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-surface-sunken text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Schedule</th>
                <th className="px-4 py-3">Next Run</th>
                <th className="px-4 py-3">Scope</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((task) => (
                <tr key={task.id} className="border-b border-subtle hover:bg-surface-hover">
                  <td className="px-4 py-3">
                    <div className="font-medium text-primary">{task.name}</div>
                    <TransferredFrom row={task} className="block" />
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-xs text-tertiary font-mono">{task.id}</span>
                      <TaskModelChip task={task} />
                      <TaskAutoChip task={task} />
                      <TaskMachine task={task} />
                    </div>
                  </td>
                  <td className="px-4 py-3 text-xs text-secondary">
                    <div>{describeSchedule(task, browserTz)}</div>
                    {task.schedule && <div className="font-mono text-tertiary mt-0.5">{task.schedule}</div>}
                    {task.interval_seconds != null && <div className="font-mono text-tertiary mt-0.5">interval {task.interval_seconds}s</div>}
                  </td>
                  <td className="px-4 py-3 text-secondary text-xs">
                    {formatNextRun(task.next_run_time)}
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-xs text-secondary">
                      {scopeName(task.scope)}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`${BADGE_BASE} ${task.enabled ? BADGE_TONE.success : BADGE_TONE.neutral}`}>
                      {task.enabled ? 'Active' : 'Paused'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      {task.can_run && (
                        <Button
                          size="xs"
                          onClick={() => handleRun(task.id, task.name)}
                          disabled={runNow.isPending}
                        >
                          Run
                        </Button>
                      )}
                      {task.can_pause && (
                        <Button
                          size="xs"
                          onClick={() => handlePause(task.id, task.name)}
                          disabled={pauseTask.isPending}
                        >
                          Pause
                        </Button>
                      )}
                      {task.can_resume && (
                        <Button
                          size="xs"
                          onClick={() => handleResume(task.id, task.name)}
                          disabled={resumeTask.isPending}
                        >
                          Resume
                        </Button>
                      )}
                      {canEdit(task) && (
                        <Button
                          size="xs"
                          onClick={() => setSettingsFor(task)}
                          title="Task settings"
                        >
                          {GEAR}
                          Settings
                        </Button>
                      )}
                      {task.can_delete && (
                        <button
                          type="button" className={BUTTON_DANGER_QUIET}
                          onClick={() => handleDelete(task.id, task.name)}
                          disabled={deleteTask.isPending}
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
        {filtered.length === 0 && (
          <p className="text-sm text-tertiary px-4 py-6">No tasks found for this agent.</p>
        )}
      </div>

      {/* Mobile cards */}
      <div className="md:hidden space-y-3">
        {filtered.length === 0 && (
          <p className="text-sm text-tertiary py-4">No tasks found for this agent.</p>
        )}
        {filtered.map((task) => (
          <div key={task.id} className={`${CARD} p-4 space-y-2`}>
            {/* A template-seeded id runs to 120 unbreakable characters: the
                left column may shrink and the id breaks anywhere, the
                status pill never shrinks \u2014 the card fits a 360 px phone. */}
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1" data-testid="task-card-main">
                <p className="text-sm font-medium text-primary">{task.name}</p>
                <TransferredFrom row={task} className="block" />
                <div className="flex items-center gap-1.5 flex-wrap mt-0.5">
                  <span className="text-xs text-tertiary font-mono break-all">{task.id}</span>
                  <TaskModelChip task={task} />
                  <TaskAutoChip task={task} />
                  <TaskMachine task={task} />
                </div>
              </div>
              <div className="flex gap-1 shrink-0" data-testid="task-card-status">
                <span className={`${BADGE_BASE} ${task.enabled ? BADGE_TONE.success : BADGE_TONE.neutral}`}>
                  {task.enabled ? 'Active' : 'Paused'}
                </span>
              </div>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
              <span className="min-w-0 break-words">
                <span className="text-tertiary">Schedule:</span>{' '}
                {describeSchedule(task, browserTz)}
              </span>
              <span className="min-w-0 break-words">
                <span className="text-tertiary">Next:</span> {formatNextRun(task.next_run_time)}
              </span>
              <span className="min-w-0 break-words">
                <span className="text-tertiary">Scope:</span> {scopeName(task.scope)}
              </span>
            </div>
            {(task.can_run || task.can_pause || task.can_resume || task.can_delete) && (
              <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-subtle">
                {task.can_run && (
                  <Button
                    size="xs"
                    onClick={() => handleRun(task.id, task.name)}
                    disabled={runNow.isPending}
                  >
                    Run Now
                  </Button>
                )}
                {task.can_pause && (
                  <Button
                    size="xs"
                    onClick={() => handlePause(task.id, task.name)}
                    disabled={pauseTask.isPending}
                  >
                    Pause
                  </Button>
                )}
                {task.can_resume && (
                  <Button
                    size="xs"
                    onClick={() => handleResume(task.id, task.name)}
                    disabled={resumeTask.isPending}
                  >
                    Resume
                  </Button>
                )}
                {canEdit(task) && (
                  <Button
                    size="xs"
                    onClick={() => setSettingsFor(task)}
                    title="Task settings"
                  >
                    {GEAR}
                    Settings
                  </Button>
                )}
                {task.can_delete && (
                  <button
                    type="button" className={BUTTON_DANGER_QUIET}
                    onClick={() => handleDelete(task.id, task.name)}
                    disabled={deleteTask.isPending}
                  >
                    Delete
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      {settingsFor && name && (
        <TaskSettingsDialog agent={name} task={settingsFor} onClose={() => setSettingsFor(null)} />
      )}
    </div>
  )
}
