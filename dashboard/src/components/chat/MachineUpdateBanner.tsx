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

  // The clock the elapsed hint reads, advanced every second while updating.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (slice?.status !== 'updating') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [slice?.status])

  if (!slice || !machineId) return null

  if (slice.status === 'updating') {
    const elapsed = Math.max(0, Math.floor((now - slice.startedAt) / 1000))
    return (
      <div className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-card border border-status-warning-border bg-status-warning-bg text-status-warning-fg">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-status-warning-solid animate-pulse" />
          <span className="truncate">
            Updating OtoDock on{' '}
            <strong>{slice.machineName || slice.machineId.slice(0, 8)}</strong>
            {' '}({slice.fromVersion || 'old'} → {slice.toVersion}).{' '}
            It reconnects in about 30 seconds. Your messages wait until then.
          </span>
          <span className="ml-auto tabular-nums opacity-60">{elapsed}s</span>
        </div>
      </div>
    )
  }

  if (slice.status === 'updated') {
    return (
      <div className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-card border border-status-success-border bg-status-success-bg text-status-success-fg">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-status-success-solid" />
          <span>
            OtoDock on{' '}
            <strong>{slice.machineName || slice.machineId.slice(0, 8)}</strong>
            {' '}updated to {slice.toVersion}.
          </span>
        </div>
      </div>
    )
  }

  // failed
  return (
    <div className="w-full px-3 py-2 mx-auto max-w-4xl text-xs rounded-card border border-status-danger-border bg-status-danger-bg text-status-danger-fg">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-block w-2 h-2 rounded-full bg-status-danger-solid shrink-0" />
          <span className="truncate">
            Update of{' '}
            <strong>{slice.machineName || slice.machineId.slice(0, 8)}</strong>
            {' '}failed
            {slice.error ? `: ${slice.error}` : ''}.
            {slice.rolledBackTo ? ` It rolled back to ${slice.rolledBackTo}.` : ''}
            {slice.autoUpdatePaused
              ? ' Automatic updates are paused for this machine. Retry with Update now in Admin, Remote Machines.'
              : ''}
          </span>
        </div>
        <button
          type="button"
          onClick={() => dismiss(slice.machineId)}
          className="shrink-0 px-1.5 py-0.5 rounded-control hover:underline"
          title="Dismiss"
        >
          ×
        </button>
      </div>
    </div>
  )
}
