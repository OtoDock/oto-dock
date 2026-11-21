// What a task row's state badge says.

import type { BADGE_TONE } from '../ui/classes'
import type { Task } from '../../api/tasks'

/** The fields the badge reads off a task row. */
export type TaskStateRef = Pick<Task, 'enabled' | 'will_not_fire'>

/** What the badge says: a paused row, a one-shot whose time passed with
 * nothing left to run it (the server's `will_not_fire`), or an active row. */
export function taskState(task: TaskStateRef): { label: string; tone: keyof typeof BADGE_TONE; hint: string } {
  if (!task.enabled) return { label: 'Paused', tone: 'neutral', hint: '' }
  if (task.will_not_fire) {
    return { label: 'Will not run', tone: 'warning', hint: 'Its time has passed and nothing will run it. Run it now, or delete it.' }
  }
  return { label: 'Active', tone: 'success', hint: '' }
}
