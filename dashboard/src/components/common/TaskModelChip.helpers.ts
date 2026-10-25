// A trigger row read as the model chip's task fields.

import type { ModelRef } from './TaskModelChip'

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
