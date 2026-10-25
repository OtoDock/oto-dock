import { RUN_STATUS, type RunStatus } from '../lib/status/run'
import { MEETING_STATUS, type MeetingStatus } from '../lib/status/meeting'
import { Badge } from './ui/SettingsControls'
import type { BADGE_TONE } from './ui/classes'

// The badge serves two machines — the task run and the meeting — and takes
// any other word as a grey pill (a lookup, never a branch).
type Status = RunStatus | MeetingStatus | string

const STATUS_TONE: Record<RunStatus | MeetingStatus, keyof typeof BADGE_TONE> = {
  // The task run (lib/status/run.ts)
  pending: 'info',
  running: 'info',
  completed: 'success',
  failed: 'danger',
  cancelled: 'neutral',
  limit_exceeded: 'warning',
  // The meeting (lib/status/meeting.ts); pending and failed share the run's rows
  active: 'agent',
  concluding: 'agent',
  concluded: 'success',
  paused: 'warning',
}

export default function StatusBadge({ status }: { status: Status }) {
  const tone = STATUS_TONE[status as RunStatus | MeetingStatus] ?? 'neutral'
  return (
    <Badge variant={tone}>
      {(status === RUN_STATUS.RUNNING || status === MEETING_STATUS.ACTIVE) && (
        <span className={`w-2 h-2 rounded-full animate-pulse ${status === MEETING_STATUS.ACTIVE ? 'bg-accent-agent-solid' : 'bg-status-info-solid'}`} />
      )}
      {status === RUN_STATUS.PENDING ? 'queued' : status}
    </Badge>
  )
}
