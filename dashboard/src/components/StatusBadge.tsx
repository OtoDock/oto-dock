type Status = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | string

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-yellow-100 text-yellow-800',
  running: 'bg-blue-100 text-blue-800',
  completed: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  failed: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400',
  cancelled: 'bg-gray-100 text-gray-700',
  limit_exceeded: 'bg-orange-100 text-orange-800',
  // Meeting statuses
  active: 'bg-blue-100 text-blue-800',
  concluding: 'bg-indigo-100 text-indigo-800',
  concluded: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400',
  paused: 'bg-yellow-100 text-yellow-800',
}

export default function StatusBadge({ status }: { status: Status }) {
  const style = STATUS_STYLES[status] ?? 'bg-gray-100 text-gray-700'
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {(status === 'running' || status === 'active') && (
        <span className="mr-1 w-2 h-2 rounded-full bg-blue-500 animate-pulse" />
      )}
      {status}
    </span>
  )
}
