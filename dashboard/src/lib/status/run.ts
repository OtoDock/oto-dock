/**
 * The task run's status — mirrored from the proxy's authority
 * `proxy/storage/automation/run_status.py` (`tests/storage/test_status_vocabularies.py`
 * binds this file to it; edit both).
 *
 * `task_runs.status` takes six values: `pending` (queued), `running` (in
 * its slot) and the four ends. The delegate result is what a delegate lane
 * reports to its caller (the `delegate_result` frame, the live delegate
 * block): the three ends plus `user_interrupted`, never a stored run
 * status.
 */

/** The stored `task_runs.status` values. */
export const RUN_STATUS = {
  PENDING: 'pending',
  RUNNING: 'running',
  COMPLETED: 'completed',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
  LIMIT_EXCEEDED: 'limit_exceeded',
} as const
export type RunStatus = (typeof RUN_STATUS)[keyof typeof RUN_STATUS]

/** Queued or running — a row that has not ended (an unknown or absent
 * value reads as not live). */
export function isLiveRunStatus(status: string | null | undefined): boolean {
  return status === RUN_STATUS.PENDING || status === RUN_STATUS.RUNNING
}

/** The delegate result: what a lane reports to its caller. */
export const DELEGATE_RESULT = {
  COMPLETED: 'completed',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
  USER_INTERRUPTED: 'user_interrupted',
} as const
export type DelegateResult = (typeof DELEGATE_RESULT)[keyof typeof DELEGATE_RESULT]

/** A delegate block's status: `running` until its result arrives. */
export type DelegateBlockStatus = typeof RUN_STATUS.RUNNING | DelegateResult
