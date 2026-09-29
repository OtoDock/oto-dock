/**
 * The task kinds — mirrored from the proxy's authority
 * `proxy/services/scheduler/task_kinds.py` (`tests/core/test_kinds.py`
 * binds this file to it; edit both).
 *
 * Three frozen vocabularies: a task DEFINITION's kind (`dynamic_tasks.task_type`),
 * a RUN's kind (`task_runs.task_type` — its `one-time` keeps the hyphen the
 * run column has always had) and a run's ORIGIN (`task_runs.trigger_type`).
 */

/** `dynamic_tasks.task_type` (`check` is a judge run's in-memory kind). */
export const TASK_KIND = {
  SCHEDULED: 'scheduled',
  ONE_TIME: 'one_time',
  TRIGGER: 'trigger',
  CONTINUATION: 'continuation',
  APP: 'app',
  DELEGATE: 'delegate',
  CHECK: 'check',
} as const
export type TaskKind = (typeof TASK_KIND)[keyof typeof TASK_KIND]

/** `task_runs.task_type`. */
export const RUN_KIND = {
  SCHEDULED: 'scheduled',
  ONE_TIME: 'one-time',
  TRIGGER: 'trigger',
  DELEGATE: 'delegate',
  APP: 'app',
  CHECK: 'check',
} as const
export type RunKind = (typeof RUN_KIND)[keyof typeof RUN_KIND]

export const RUN_KIND_LABEL: Record<RunKind, string> = {
  [RUN_KIND.SCHEDULED]: 'Recurring',
  [RUN_KIND.ONE_TIME]: 'One-time',
  [RUN_KIND.TRIGGER]: 'Trigger',
  [RUN_KIND.DELEGATE]: 'Delegate',
  [RUN_KIND.APP]: 'App',
  [RUN_KIND.CHECK]: 'Check',
}

export const RUN_KIND_STYLE: Record<RunKind, string> = {
  [RUN_KIND.SCHEDULED]: 'bg-blue-100 text-blue-700',
  [RUN_KIND.ONE_TIME]: 'bg-amber-100 text-amber-700',
  [RUN_KIND.TRIGGER]: 'bg-orange-100 text-orange-700',
  [RUN_KIND.DELEGATE]: 'bg-purple-100 text-purple-700',
  [RUN_KIND.APP]: 'bg-teal-100 text-teal-700',
  [RUN_KIND.CHECK]: 'bg-indigo-100 text-indigo-700',
}

export function isRunKind(t: string | null | undefined): t is RunKind {
  return !!t && Object.prototype.hasOwnProperty.call(RUN_KIND_LABEL, t)
}

/** `task_runs.trigger_type`: what fired the run. */
export const TRIGGER_KIND = {
  SCHEDULED: 'scheduled',
  MANUAL: 'manual',
  TRIGGER: 'trigger',
  CHECK: 'check',
  APP_HANDLER: 'app_handler',
  APP_ACTION: 'app_action',
} as const
export type TriggerKind = (typeof TRIGGER_KIND)[keyof typeof TRIGGER_KIND]

export const TRIGGER_KIND_LABEL: Record<TriggerKind, string> = {
  [TRIGGER_KIND.SCHEDULED]: 'Scheduled',
  [TRIGGER_KIND.MANUAL]: 'Manual',
  [TRIGGER_KIND.TRIGGER]: 'Trigger',
  [TRIGGER_KIND.CHECK]: 'Check',
  [TRIGGER_KIND.APP_HANDLER]: 'App handler',
  [TRIGGER_KIND.APP_ACTION]: 'App action',
}

function isTriggerKind(t: string): t is TriggerKind {
  return Object.prototype.hasOwnProperty.call(TRIGGER_KIND_LABEL, t)
}

/** The origin column's words: a trigger names its trigger, a manual fire
 * its actor, everything else its kind's label (a word outside the six
 * renders verbatim, as it always did). */
export function formatTrigger(triggerType: string, triggerSource: string | null): string {
  if (triggerType === TRIGGER_KIND.TRIGGER) {
    return triggerSource ? `Trigger: ${triggerSource}` : TRIGGER_KIND_LABEL[TRIGGER_KIND.TRIGGER]
  }
  if (triggerType === TRIGGER_KIND.MANUAL && triggerSource) return triggerSource
  return isTriggerKind(triggerType) ? TRIGGER_KIND_LABEL[triggerType] : triggerType
}
