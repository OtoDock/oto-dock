import type { Run } from '../api/runs'

const TASK_TYPE_LABELS: Record<string, string> = {
  static: 'Static',
  scheduled: 'Recurring',
  'one-time': 'One-time',
  delegate: 'Delegate',
  trigger: 'Trigger',
}

const TASK_TYPE_STYLES: Record<string, string> = {
  static: 'bg-gray-100 text-gray-700',
  scheduled: 'bg-blue-100 text-blue-700',
  'one-time': 'bg-amber-100 text-amber-700',
  delegate: 'bg-purple-100 text-purple-700',
  trigger: 'bg-orange-100 text-orange-700',
}

// Internal task types that should be filtered out of user-facing lists
// (admin History). Admin views show them via the audit log.
// Task types hidden from user-facing run lists. Empty since the
// memory_run type was retired with the Memory rebuild; kept as the
// mechanism for future internal types.
export const INTERNAL_TASK_TYPES = new Set<string>([])

export function isInternalTaskType(taskType: string | null): boolean {
  return !!taskType && INTERNAL_TASK_TYPES.has(taskType)
}

export function getTaskTypeLabel(taskType: string | null): string {
  return taskType ? TASK_TYPE_LABELS[taskType] ?? taskType : '—'
}

export function getTaskTypeStyle(taskType: string | null): string {
  return taskType ? TASK_TYPE_STYLES[taskType] ?? 'bg-gray-100 text-gray-700' : 'bg-gray-100 text-gray-700'
}

export function formatTrigger(triggerType: string, triggerSource: string | null): string {
  if (triggerType === 'triggered') return triggerSource ? `Trigger: ${triggerSource}` : 'Trigger'
  if (triggerType === 'scheduled') return 'Scheduled'
  if (triggerType === 'manual' && triggerSource) return triggerSource
  return triggerType
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
