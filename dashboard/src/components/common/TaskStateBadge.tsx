import { BADGE_BASE, BADGE_TONE } from '../ui/classes'
import { taskState, type TaskStateRef } from './TaskStateBadge.helpers'

/** A task row's state badge, on both Scheduled Tasks pages. */
export function TaskStateBadge({ task }: { task: TaskStateRef }) {
  const state = taskState(task)
  return (
    <span className={`${BADGE_BASE} ${BADGE_TONE[state.tone]}`} title={state.hint || undefined}>
      {state.label}
    </span>
  )
}
