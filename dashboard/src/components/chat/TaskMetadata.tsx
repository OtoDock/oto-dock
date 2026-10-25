import { useState, useEffect, useRef } from 'react'
import type { Run } from '../../api/runs'
import { getTaskTypeLabel, getTaskTypeStyle, formatTrigger } from '../../lib/runs'
import { formatDuration, formatRelativeTime } from '../../lib/format'
import StatusBadge from '../StatusBadge'
import { pushEscHandler } from '../../lib/escStack'
import { RUN_STATUS } from '../../lib/status/run'
import { GLASS, GLASS_HOVER } from '../ui/glass'
import { POPOVER } from '../ui/classes'

interface Props {
  run: Run
  /** The chat's gauge rule (see ChatStatusBar): false = the run's chat runs on
   * a subscription or a local model, so its cost is an activity estimate and
   * the Cost block is hidden like the gauge. Undefined = shown. */
  costBilled?: boolean
}

export default function TaskMetadata({ run, costBilled }: Props) {
  const [open, setOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  // Close on click outside
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // Close on Escape (precedence stack)
  useEffect(() => {
    if (!open) return
    return pushEscHandler(() => setOpen(false))
  }, [open])

  const statusColor =
    run.status === RUN_STATUS.COMPLETED ? 'bg-status-success-solid' :
    run.status === RUN_STATUS.RUNNING ? 'bg-action' :
    run.status === RUN_STATUS.FAILED ? 'bg-status-danger-solid' :
    'bg-status-info-solid'

  return (
    <div ref={panelRef}>
      {/* Icon button */}
      <button
        onClick={() => setOpen(!open)}
        title={run.status === RUN_STATUS.PENDING
          ? 'Task run: queued, waiting for a free task slot'
          : `Task run: ${run.status}`}
        className={`relative flex size-control-md items-center justify-center rounded-xl ${GLASS} ${GLASS_HOVER}
                   transition-colors duration-fast`}
      >
        {/* Clipboard/task icon */}
        <svg className="w-5 h-5 text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" strokeWidth={1.5} strokeLinecap="round" />
          <rect x="9" y="3" width="6" height="4" rx="1" strokeWidth={1.5} />
          <path d="M9 12h6M9 16h4" strokeWidth={1.3} strokeLinecap="round" />
        </svg>
        {/* Status badge -- bottom-right corner */}
        <span className={`absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full ${statusColor} border-2 border-surface-card`}>
          {run.status === RUN_STATUS.RUNNING && (
            <span className="absolute inset-0 rounded-full animate-ping bg-action opacity-50" />
          )}
          {run.status === RUN_STATUS.PENDING && (
            <span className="absolute inset-0 rounded-full animate-ping bg-status-info-solid opacity-40" />
          )}
        </span>
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className={`mt-2 ${POPOVER} overflow-hidden
                        w-72 sm:w-80 max-w-[calc(100vw-2rem)]`}>
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-subtle">
            <span className="text-xs font-medium text-secondary uppercase tracking-wide">Task Run</span>
            <StatusBadge status={run.status} />
          </div>

          {/* Content */}
          <div className="px-4 py-3 space-y-3">
            {/* Task name */}
            <div>
              <p className="text-xs text-tertiary uppercase">Task</p>
              <p className="text-sm font-semibold text-primary mt-0.5">{run.task_id}</p>
            </div>

            {/* Type + Trigger row */}
            <div className="flex gap-4">
              <div>
                <p className="text-xs text-tertiary uppercase">Type</p>
                <p className="mt-0.5">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${getTaskTypeStyle(run.task_type)}`}>
                    {getTaskTypeLabel(run.task_type)}
                  </span>
                </p>
              </div>
              <div className="flex-1">
                <p className="text-xs text-tertiary uppercase">Trigger</p>
                <p className="text-sm text-primary mt-0.5">{formatTrigger(run.trigger_type, run.trigger_source)}</p>
              </div>
            </div>

            {/* Started + Duration row */}
            <div className="flex gap-4">
              <div>
                <p className="text-xs text-tertiary uppercase">Started</p>
                <p className="text-sm text-secondary mt-0.5">
                  {run.started_at ? formatRelativeTime(run.started_at) : '\u2014'}
                </p>
              </div>
              <div>
                <p className="text-xs text-tertiary uppercase">Duration</p>
                <p className="text-sm text-secondary mt-0.5">
                  {run.duration_ms ? formatDuration(run.duration_ms) : '\u2014'}
                </p>
              </div>
            </div>

            {/* Cost — the same number as the gauge (chats.total_cost), so it
                follows the same credential-kind rule. */}
            {costBilled !== false && (run.cost_usd > 0 || (run.session_cost_usd && run.session_cost_usd > 0)) && (
              <div>
                <p className="text-xs text-tertiary uppercase">Cost</p>
                {run.session_cost_usd && run.session_turn_count && run.session_turn_count > 1 ? (
                  <div className="mt-0.5">
                    <p className="text-sm text-secondary">
                      ${run.session_cost_usd.toFixed(4)}
                      <span className="text-xs text-tertiary ml-1">
                        over {run.session_turn_count} turns
                      </span>
                    </p>
                    {run.cost_usd > 0 && (
                      <p className="text-xs text-tertiary">This turn: ${run.cost_usd.toFixed(4)}</p>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-secondary mt-0.5">${run.cost_usd.toFixed(4)}</p>
                )}
              </div>
            )}

          </div>
        </div>
      )}
    </div>
  )
}
