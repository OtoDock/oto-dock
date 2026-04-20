import { useState, useEffect, useRef } from 'react'
import type { Run } from '../../api/runs'
import { getTaskTypeLabel, getTaskTypeStyle, formatTrigger } from '../../lib/runs'
import { formatDuration, formatRelativeTime } from '../../lib/format'
import StatusBadge from '../StatusBadge'
import { pushEscHandler } from '../../lib/escStack'

interface Props {
  run: Run
}

export default function TaskMetadata({ run }: Props) {
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
    run.status === 'completed' ? 'bg-green-500' :
    run.status === 'running' || run.status === 'pending' ? 'bg-yellow-500' :
    run.status === 'failed' ? 'bg-red-500' :
    'bg-gray-400'

  return (
    <div ref={panelRef}>
      {/* Icon button */}
      <button
        onClick={() => setOpen(!open)}
        title={`Task run: ${run.status}`}
        className="relative w-10 h-10 rounded-xl bg-white/80 dark:bg-gray-900/80 backdrop-blur-xs border border-p-border-light shadow-xs
                   hover:shadow-md hover:bg-white dark:hover:bg-p-surface transition-all flex items-center justify-center"
      >
        {/* Clipboard/task icon */}
        <svg className="w-5 h-5 text-p-text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" strokeWidth={1.5} strokeLinecap="round" />
          <rect x="9" y="3" width="6" height="4" rx="1" strokeWidth={1.5} />
          <path d="M9 12h6M9 16h4" strokeWidth={1.3} strokeLinecap="round" />
        </svg>
        {/* Status badge -- bottom-right corner */}
        <span className={`absolute -bottom-0.5 -right-0.5 w-3.5 h-3.5 rounded-full ${statusColor} border-2 border-white dark:border-gray-900`}>
          {run.status === 'running' && (
            <span className="absolute inset-0 rounded-full animate-ping bg-yellow-400 opacity-50" />
          )}
        </span>
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="mt-2 bg-white/95 dark:bg-gray-900/95 backdrop-blur-xs border border-p-border-light shadow-lg rounded-xl overflow-hidden
                        w-72 sm:w-80 max-w-[calc(100vw-2rem)]">
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-p-border-light bg-p-bg/50">
            <span className="text-xs font-medium text-p-text-secondary uppercase tracking-wide">Task Run</span>
            <StatusBadge status={run.status} />
          </div>

          {/* Content */}
          <div className="px-4 py-3 space-y-3">
            {/* Task name */}
            <div>
              <p className="text-xs text-p-text-light uppercase">Task</p>
              <p className="text-sm font-semibold text-p-text mt-0.5">{run.task_id}</p>
            </div>

            {/* Type + Trigger row */}
            <div className="flex gap-4">
              <div>
                <p className="text-xs text-p-text-light uppercase">Type</p>
                <p className="mt-0.5">
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${getTaskTypeStyle(run.task_type)}`}>
                    {getTaskTypeLabel(run.task_type)}
                  </span>
                </p>
              </div>
              <div className="flex-1">
                <p className="text-xs text-p-text-light uppercase">Trigger</p>
                <p className="text-sm text-p-text mt-0.5">{formatTrigger(run.trigger_type, run.trigger_source)}</p>
              </div>
            </div>

            {/* Started + Duration row */}
            <div className="flex gap-4">
              <div>
                <p className="text-xs text-p-text-light uppercase">Started</p>
                <p className="text-sm text-p-text-secondary mt-0.5">
                  {run.started_at ? formatRelativeTime(run.started_at) : '\u2014'}
                </p>
              </div>
              <div>
                <p className="text-xs text-p-text-light uppercase">Duration</p>
                <p className="text-sm text-p-text-secondary mt-0.5">
                  {run.duration_ms ? formatDuration(run.duration_ms) : '\u2014'}
                </p>
              </div>
            </div>

            {/* Cost */}
            {(run.cost_usd > 0 || (run.session_cost_usd && run.session_cost_usd > 0)) && (
              <div>
                <p className="text-xs text-p-text-light uppercase">Cost</p>
                {run.session_cost_usd && run.session_turn_count && run.session_turn_count > 1 ? (
                  <div className="mt-0.5">
                    <p className="text-sm text-p-text-secondary">
                      ${run.session_cost_usd.toFixed(4)}
                      <span className="text-xs text-p-text-light ml-1">
                        ({run.session_turn_count} turns)
                      </span>
                    </p>
                    {run.cost_usd > 0 && (
                      <p className="text-xs text-p-text-light">this turn: ${run.cost_usd.toFixed(4)}</p>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-p-text-secondary mt-0.5">${run.cost_usd.toFixed(4)}</p>
                )}
              </div>
            )}

          </div>
        </div>
      )}
    </div>
  )
}
