import { useEffect, useState } from 'react'

import {
  useMachineUpdateSlice,
  useMachineUpdateStore,
} from '../../store/machineUpdateStore'

interface Props {
  machineId: string | null | undefined
}

// Renders a top-of-chat banner while the target satellite is being
// updated (amber → green flash → red on failure). Reads the per-machine
// slice from machineUpdateStore so the banner state survives chat
// navigation — if you switch chats mid-update, the new chat will still
// show the banner as long as its execution_target is the same machine.
export default function MachineUpdateBanner({ machineId }: Props) {
  const slice = useMachineUpdateSlice(machineId)
  const dismiss = useMachineUpdateStore((s) => s.dismiss)

  // Auto-dismiss "updated" banners after 4s. Failures stay until manual
  // dismiss so the user actually sees the error.
  useEffect(() => {
    if (slice?.status !== 'updated') return
    const t = setTimeout(() => {
      if (machineId) dismiss(machineId)
    }, 4000)
    return () => clearTimeout(t)
  }, [slice?.status, machineId, dismiss])

  // Re-render every second while updating so the elapsed-time hint is fresh.
  const [, setTick] = useState(0)
  useEffect(() => {
    if (slice?.status !== 'updating') return
    const t = setInterval(() => setTick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [slice?.status])

  if (!slice || !machineId) return null

  if (slice.status === 'updating') {
    const elapsed = Math.floor((Date.now() - slice.startedAt) / 1000)
    return (
      <div className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-amber-300/40 bg-amber-50/40 text-amber-900 dark:bg-amber-500/10 dark:text-amber-200">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-amber-500 animate-pulse" />
          <span className="truncate">
            Updating satellite{' '}
            <strong>{slice.machineName || slice.machineId.slice(0, 8)}</strong>
            {' '}({slice.fromVersion || 'old'} → {slice.toVersion}).{' '}
            Will reconnect in ~30s. Your messages will be queued.
          </span>
          <span className="ml-auto tabular-nums opacity-60">{elapsed}s</span>
        </div>
      </div>
    )
  }

  if (slice.status === 'updated') {
    return (
      <div className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-emerald-300/40 bg-emerald-50/40 text-emerald-900 dark:bg-emerald-500/10 dark:text-emerald-200">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-emerald-500" />
          <span>
            Satellite{' '}
            <strong>{slice.machineName || slice.machineId.slice(0, 8)}</strong>
            {' '}updated to {slice.toVersion}.
          </span>
        </div>
      </div>
    )
  }

  // failed
  return (
    <div className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-sm border border-red-300/40 bg-red-50/40 text-red-900 dark:bg-red-500/10 dark:text-red-200">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-block w-2 h-2 rounded-full bg-red-500 shrink-0" />
          <span className="truncate">
            Update of{' '}
            <strong>{slice.machineName || slice.machineId.slice(0, 8)}</strong>
            {' '}failed
            {slice.error ? `: ${slice.error}` : ''}
            {slice.rolledBackTo ? ` (rolled back to ${slice.rolledBackTo})` : ''}.
          </span>
        </div>
        <button
          type="button"
          onClick={() => dismiss(slice.machineId)}
          className="shrink-0 px-1.5 py-0.5 rounded-sm hover:bg-red-500/20"
          title="Dismiss"
        >
          ×
        </button>
      </div>
    </div>
  )
}
