import { useState, useEffect, useRef } from 'react'
import { pushEscHandler } from '../../../lib/escStack'
import type { ThreadGoal } from '../../../hooks/useDashboardWs.types'
import { GLASS, GLASS_HOVER } from '../../ui/glass'
import { POPOVER } from '../../ui/classes'

interface Props {
  goal: ThreadGoal | null
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(n % 1_000 ? 1 : 0)}k`
  return String(n)
}

function fmtDuration(secs: number): string {
  if (secs < 60) return `${secs}s`
  const m = Math.floor(secs / 60)
  if (m < 60) return `${m}m`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

// Non-active goal states worth surfacing in the dropdown header
// (camelCase wire values → readable label).
const STATUS_LABEL: Record<string, string> = {
  paused: 'paused',
  usageLimited: 'usage limited',
  budgetLimited: 'budget limited',
}

/**
 * Codex thread-goal panel — the pinned right-side sibling of TodoPanel.
 * Collapsed: a target icon with the objective as tooltip. Expanded: the
 * objective, a token-budget progress bar (hidden when the goal has no
 * budget), and humanized time used. Renders nothing when the chat has no
 * goal (most chats never set one) or the goal is COMPLETE — a model
 * "mark complete" arrives as a status update, and a finished goal
 * lingering in the corner reads as still-pending (TodoPanel precedent:
 * auto-hide when done). Stuck states (paused/budget-limited) stay
 * visible with a status label.
 */
export default function GoalPanel({ goal }: Props) {
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

  // Defensive: a malformed WS frame must not crash (and unmount) the chat tree.
  if (!goal || typeof goal.objective !== 'string' || !goal.objective) return null
  if (goal.status === 'complete') return null

  const statusLabel = STATUS_LABEL[goal.status ?? ''] ?? null
  const tokensUsed = Number(goal.tokens_used) || 0
  const budget = typeof goal.token_budget === 'number' && goal.token_budget > 0
    ? goal.token_budget : null
  const pct = budget ? Math.min(100, Math.round((tokensUsed / budget) * 100)) : 0
  const barColor = pct >= 100 ? 'bg-status-danger-solid' : pct >= 80 ? 'bg-status-warning-solid' : 'bg-status-info-solid'
  const timeUsed = Number(goal.time_used_seconds) || 0

  return (
    <div ref={panelRef} className="flex flex-col items-end">
      {/* Icon button */}
      <button
        onClick={() => setOpen(!open)}
        title={`Goal: ${goal.objective}`}
        className={`relative flex size-control-md items-center justify-center rounded-xl ${GLASS} ${GLASS_HOVER}
                   transition-colors duration-fast`}
      >
        {/* Target icon */}
        <svg className="w-5 h-5 text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <circle cx="12" cy="12" r="8" strokeWidth={1.5} />
          <circle cx="12" cy="12" r="4.5" strokeWidth={1.4} />
          <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
        </svg>
        {/* Budget badge — bottom-right */}
        {budget !== null && (
          <span className="absolute -bottom-1 -right-1 min-w-[18px] h-[18px] rounded-full bg-action text-action-fg text-3xs font-bold flex items-center justify-center px-1 border-2 border-surface-card">
            {pct}%
          </span>
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className={`mt-2 ${POPOVER} overflow-hidden
                        w-64 sm:w-72 max-w-[calc(100vw-2rem)]`}>
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-subtle">
            <span className="text-xs font-medium text-secondary">
              Goal
              {statusLabel && (
                <span className="ml-1.5 text-3xs font-normal text-status-warning-fg">({statusLabel})</span>
              )}
            </span>
            <span className="text-xs text-tertiary font-mono">
              {fmtDuration(timeUsed)}
            </span>
          </div>
          {/* Objective */}
          <div className="px-3 py-2 text-xs text-primary whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
            {goal.objective}
          </div>
          {/* Budget */}
          <div className="px-3 pb-2.5">
            {budget !== null ? (
              <>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-3xs text-tertiary">Token budget</span>
                  <span className="text-3xs text-tertiary font-mono">
                    {fmtTokens(tokensUsed)} / {fmtTokens(budget)}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-surface-muted overflow-hidden">
                  <div className={`h-full rounded-full ${barColor} transition-all`} style={{ width: `${pct}%` }} />
                </div>
              </>
            ) : tokensUsed > 0 ? (
              <div className="flex items-center justify-between">
                <span className="text-3xs text-tertiary">Tokens used</span>
                <span className="text-3xs text-tertiary font-mono">{fmtTokens(tokensUsed)}</span>
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}
