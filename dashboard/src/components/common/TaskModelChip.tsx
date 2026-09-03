import type { Task } from '../../api/tasks'

/**
 * The model (and engine) a scheduled task will actually run on.
 *
 * Always rendered when the server could resolve one, so a reader never has to
 * guess whether a blank cell means "default" or "unknown": a task pinned by an
 * agent through the schedules MCP gets the branded chip, a task inheriting the
 * agent's default gets a muted one. The execution layer is appended only when
 * the task pins it — the default layer is already implied by the agent.
 *
 * Display only. Pins are set by agents (schedules-mcp `model` / `layer`, on
 * create or `edit_task`), not from the dashboard.
 */
export function TaskModelChip({ task }: { task: Task }) {
  if (!task.effective_model) return null
  const pinned = !!task.override_model
  const layer = task.override_execution_path || ''
  return (
    <span
      title={
        pinned
          ? `Pinned to ${task.effective_model}${layer ? ` on ${layer}` : ''} — this task only`
          : `Agent default: ${task.effective_model}`
      }
      className={`inline-flex items-center px-1.5 py-0.5 rounded-sm text-[10px] font-medium font-mono ${
        pinned
          ? 'bg-brand/10 text-brand'
          : 'bg-p-bg text-p-text-light border border-p-border-light'
      }`}
    >
      {task.effective_model}
      {layer && <span className="opacity-70">&nbsp;· {layer}</span>}
    </span>
  )
}
