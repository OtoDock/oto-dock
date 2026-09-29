import { RUN_STATUS, type RunStatus } from '../lib/status/run'
import { MEETING_STATUS, type MeetingStatus } from '../lib/status/meeting'

// The badge serves two machines — the task run and the meeting — and takes
// any other word as a grey pill (a lookup, never a branch).
type Status = RunStatus | MeetingStatus | string

const STATUS_STYLES: Record<RunStatus | MeetingStatus, string> = {
  // The task run (lib/status/run.ts)
  pending: 'bg-indigo-100 text-indigo-800',
  running: 'bg-blue-100 text-blue-800',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
  cancelled: 'bg-gray-100 text-gray-700',
  limit_exceeded: 'bg-orange-100 text-orange-800',
  // The meeting (lib/status/meeting.ts); pending and failed share the run's rows
  active: 'bg-blue-100 text-blue-800',
  concluding: 'bg-indigo-100 text-indigo-800',
  concluded: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  paused: 'bg-yellow-100 text-yellow-800',
}

export default function StatusBadge({ status }: { status: Status }) {
  const style = STATUS_STYLES[status as RunStatus | MeetingStatus] ?? 'bg-gray-100 text-gray-700'
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {(status === RUN_STATUS.RUNNING || status === MEETING_STATUS.ACTIVE) && (
        <span className="mr-1 w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
      )}
      {status === RUN_STATUS.PENDING ? 'queued' : status}
    </span>
  )
}
