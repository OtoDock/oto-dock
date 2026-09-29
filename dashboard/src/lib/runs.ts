import type { Run } from '../api/runs'
import { RUN_KIND_LABEL, RUN_KIND_STYLE, formatTrigger, isRunKind } from './kinds/task'

export { formatTrigger }

// A run row older than the platform's dynamic tasks may carry `static`
// (the initial release's config-file tasks); no writer has minted it since,
// so it is a legacy read-through beside the mirror's kinds, not a kind.
const LEGACY_RUN_KIND_LABEL: Record<string, string> = { static: 'Static' }
const LEGACY_RUN_KIND_STYLE: Record<string, string> = { static: 'bg-gray-100 text-gray-700' }
const NEUTRAL_STYLE = 'bg-gray-100 text-gray-700'

// Internal task types that should be filtered out of user-facing lists
// (admin History). Admin views show them via the audit log.
// Task types hidden from user-facing run lists. Empty since the
// memory_run type was retired with the Memory rebuild; kept as the
// mechanism for future internal types.
export const INTERNAL_TASK_TYPES = new Set<string>([])

export function isInternalTaskType(taskType: string | null): boolean {
  return !!taskType && INTERNAL_TASK_TYPES.has(taskType)
}

/** The run kind's badge word (lib/kinds/task.ts); an unknown word verbatim. */
export function getTaskTypeLabel(taskType: string | null): string {
  if (!taskType) return '—'
  if (isRunKind(taskType)) return RUN_KIND_LABEL[taskType]
  return LEGACY_RUN_KIND_LABEL[taskType] ?? taskType
}

export function getTaskTypeStyle(taskType: string | null): string {
  if (!taskType) return NEUTRAL_STYLE
  if (isRunKind(taskType)) return RUN_KIND_STYLE[taskType]
  return LEGACY_RUN_KIND_STYLE[taskType] ?? NEUTRAL_STYLE
}

export interface SessionGroup {
  key: string
  runs: Run[]
  representative: Run
  latestStatus: Run['status']
  turnCount: number
  totalCost: number
  totalDuration: number
}

/**
 * Group runs by session_id. Multi-run sessions become a single group
 * with the first run as representative and the latest run's status.
 * Runs without a session_id are treated as standalone groups.
 * Results are sorted by representative's started_at descending.
 */
export function groupRunsBySession(runs: Run[]): SessionGroup[] {
  const sessionMap = new Map<string, Run[]>()
  const standalone: Run[] = []

  for (const run of runs) {
    if (run.session_id) {
      const existing = sessionMap.get(run.session_id)
      if (existing) {
        existing.push(run)
      } else {
        sessionMap.set(run.session_id, [run])
      }
    } else {
      standalone.push(run)
    }
  }

  const result: SessionGroup[] = []

  for (const [sessionId, sessionRuns] of sessionMap) {
    const sorted = [...sessionRuns].sort((a, b) =>
      (a.started_at ?? '').localeCompare(b.started_at ?? '')
    )
    const latest = sorted[sorted.length - 1]
    result.push({
      key: sessionId,
      runs: sorted,
      representative: sorted[0],
      latestStatus: latest.status,
      turnCount: sorted.length,
      totalCost: sorted.reduce((sum, r) => sum + (r.cost_usd || 0), 0),
      totalDuration: sorted.reduce((sum, r) => sum + (r.duration_ms || 0), 0),
    })
  }

  for (const run of standalone) {
    result.push({
      key: run.id,
      runs: [run],
      representative: run,
      latestStatus: run.status,
      turnCount: 1,
      totalCost: run.cost_usd || 0,
      totalDuration: run.duration_ms || 0,
    })
  }

  result.sort((a, b) =>
    (b.representative.started_at ?? '').localeCompare(a.representative.started_at ?? '')
  )

  return result
}
