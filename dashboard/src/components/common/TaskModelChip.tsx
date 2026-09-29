import { TierMark } from './TierMark'
import { tierText } from '../../lib/tiers'

/** The fields the chip needs: a task row, or a trigger row's linked-task
 * fields mapped onto the same names. */
export interface ModelRef {
  effective_model: string
  override_model?: string | null
  override_execution_path?: string | null
  effective_model_source?: string
  effective_model_tier?: number | null
  tier_label?: string
}

/** A trigger row's linked-task fields, mapped onto the chip's names. */
export function linkedTaskModel(t: {
  task_effective_model?: string
  task_override_model?: string
  task_effective_execution_path?: string
  task_effective_model_source?: string
  task_effective_model_tier?: number | null
  task_tier_label?: string
}): ModelRef {
  return {
    effective_model: t.task_effective_model || '',
    override_model: t.task_override_model || null,
    override_execution_path: null,
    effective_model_source: t.task_effective_model_source,
    effective_model_tier: t.task_effective_model_tier,
    tier_label: t.task_tier_label,
  }
}

export { TIER_LABELS, tierText } from '../../lib/tiers'

/**
 * The model (and engine) a scheduled task will actually run on.
 *
 * Always rendered when the server could resolve one, so a reader never has to
 * guess whether a blank cell means "default" or "unknown": a task pinned by an
 * agent through the schedules MCP gets the branded chip, a task inheriting the
 * agent's default gets a muted one. The execution layer is appended only when
 * the task pins it — the default layer is already implied by the agent. The
 * capability tier rides along as the four-dot mark so a reader can tell a
 * frontier model from a fast one without knowing the ids; the word sits in
 * the tooltip.
 *
 * Display only. Pins are set by agents (schedules-mcp `model` / `layer`, on
 * create or `edit_task`), not from the dashboard.
 */
export function TaskModelChip({ task }: { task: ModelRef }) {
  if (!task.effective_model) return null
  const pinned = !!task.override_model
  const layer = task.override_execution_path || ''
  const source = task.effective_model_source
    || (pinned ? 'pinned' : 'agent default')
  const tier = tierText(task.effective_model_tier, task.tier_label)
  // max-w-full + the id in its own truncating span: a long model id on a
  // phone card ends in an ellipsis while the tier dots and the layer keep
  // their width (truncate on the inline-flex box itself would clip the dots
  // with no ellipsis).
  return (
    <span
      title={
        pinned
          ? `Pinned to ${task.effective_model}${layer ? ` on ${layer}` : ''} — this task only (${tier})`
          : `${source === 'layer default' ? 'Engine default' : 'Agent default'}: ${task.effective_model} (${tier})`
      }
      className={`inline-flex items-center max-w-full overflow-hidden px-1.5 py-0.5 rounded-sm text-[10px] font-medium font-mono ${
        pinned
          ? 'bg-brand/10 text-brand'
          : 'bg-p-bg text-p-text-light border border-p-border-light'
      }`}
    >
      <span className="truncate min-w-0">{task.effective_model}</span>
      <TierMark tier={task.effective_model_tier} label={task.tier_label} size="xs" className="ml-1.5 opacity-80" />
      {layer && <span className="opacity-70 shrink-0">&nbsp;· {layer}</span>}
    </span>
  )
}
