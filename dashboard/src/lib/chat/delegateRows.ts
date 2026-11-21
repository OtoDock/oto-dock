/**
 * What a page of rows says about the delegations it holds: the one reading
 * of the `delegate_spawn` and `delegate_result` rows, for the converter
 * (`dbMessagesToDisplay`, a delegate block's status) and for the rebuilds
 * from REST rows (`withRunningInView`, a listed delegation the rows end).
 */
import { WIRE, type DbMessageRow } from '../../api/wireEvents'
import { DELEGATE_RESULT } from '../status/run'

/** The delegations the rows report on: each `delegate_result` row's end by
 *  its task id, and the spawns and results counted per task name (the
 *  fallback for a row with no task id). */
export function delegateTallies(rows: DbMessageRow[]): {
  statusByTaskId: Record<string, string>
  spawnCounts: Record<string, number>
  resultCounts: Record<string, number>
} {
  const statusByTaskId: Record<string, string> = {}
  const spawnCounts: Record<string, number> = {}
  const resultCounts: Record<string, number> = {}
  for (const m of rows) {
    // Only event rows carry these two types.
    if ((m.event_type !== WIRE.DELEGATE_SPAWN && m.event_type !== WIRE.DELEGATE_RESULT) || !m.event_data) continue
    try {
      const ed = JSON.parse(m.event_data)
      const name = ed.task_name || ''
      if (m.event_type === WIRE.DELEGATE_SPAWN) {
        spawnCounts[name] = (spawnCounts[name] || 0) + 1
      } else if (m.event_type === WIRE.DELEGATE_RESULT) {
        resultCounts[name] = (resultCounts[name] || 0) + 1
        if (ed.task_id) statusByTaskId[ed.task_id] = ed.status || DELEGATE_RESULT.COMPLETED
      }
    } catch { /* skip */ }
  }
  return { statusByTaskId, spawnCounts, resultCounts }
}

/** The task ids of the delegations whose result is among the rows. */
export function endedDelegations(rows: DbMessageRow[]): Set<string> {
  return new Set(Object.keys(delegateTallies(rows).statusByTaskId))
}
