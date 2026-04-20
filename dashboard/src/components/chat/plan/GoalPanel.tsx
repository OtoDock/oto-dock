import { useState, useEffect, useRef } from 'react'
import { pushEscHandler } from '../../../lib/escStack'
import type { ThreadGoal } from '../../../hooks/useDashboardWs.types'

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
  const barColor = pct >= 100 ? 'bg-p-accent-red' : pct >= 80 ? 'bg-[#f4b206]' : 'bg-brand'
  const timeUsed = Number(goal.time_used_seconds) || 0

  return (
    <div ref={panelRef} className="flex flex-col items-end">
      {/* Icon button */}
      <button
        onClick={() => setOpen(!open)}
        title={`Goal: ${goal.objective}`}
        className={`relative w-10 h-10 rounded-xl bg-white/80 dark:bg-gray-900/80 backdrop-blur-xs border border-p-border-light shadow-xs
                   hover:shadow-md hover:bg-white dark:hover:bg-p-surface transition-all flex items-center justify-center`}
      >
        {/* Target icon */}
        <svg className="w-5 h-5 text-p-text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor">
          <circle cx="12" cy="12" r="8" strokeWidth={1.5} />
          <circle cx="12" cy="12" r="4.5" strokeWidth={1.4} />
          <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
        </svg>
        {/* Budget badge — bottom-right */}
        {budget !== null && (
          <span className="absolute -bottom-1 -right-1 min-w-[18px] h-[18px] rounded-full bg-brand text-white text-[9px] font-bold flex items-center justify-center px-1 border-2 border-white dark:border-gray-900">
            {pct}%
          </span>
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="mt-2 bg-white/95 dark:bg-gray-900/95 backdrop-blur-xs border border-p-border-light shadow-lg rounded-xl overflow-hidden
                        w-64 sm:w-72 max-w-[calc(100vw-2rem)]">
          {/* Header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-p-border-light bg-p-bg/50">
            <span className="text-xs font-medium text-p-text-secondary">
              Goal
              {statusLabel && (
                <span className="ml-1.5 text-[10px] font-normal text-[#b8860b]">({statusLabel})</span>
              )}
            </span>
            <span className="text-xs text-p-text-light font-mono">
              {fmtDuration(timeUsed)}
            </span>
          </div>
          {/* Objective */}
          <div className="px-3 py-2 text-xs text-p-text whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
            {goal.objective}
          </div>
          {/* Budget */}
          <div className="px-3 pb-2.5">
            {budget !== null ? (
              <>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] text-p-text-light">Token budget</span>
                  <span className="text-[10px] text-p-text-light font-mono">
                    {fmtTokens(tokensUsed)} / {fmtTokens(budget)}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-p-border/60 overflow-hidden">
                  <div className={`h-full rounded-full ${barColor} transition-all`} style={{ width: `${pct}%` }} />
                </div>
              </>
            ) : tokensUsed > 0 ? (
              <div className="flex items-center justify-between">
                <span className="text-[10px] text-p-text-light">Tokens used</span>
                <span className="text-[10px] text-p-text-light font-mono">{fmtTokens(tokensUsed)}</span>
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}
