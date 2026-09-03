// Per-route call log viewer — opened from the small icon on a route row.
// A popup (not a page/menu item): route-scoped detail, following the
// AgentConversations table + McpRequestsPage outcome-tone conventions.
// Shows every reported call including the ones that never reached the agent
// (failed/locked-out PIN entries, capacity rejects) with the caller's number.

import { useState } from 'react'
import { usePhoneRouteCallLog, type PhoneRoute } from '@/api/phone'
import { formatRelativeTime } from '@/lib/format'
import { Badge } from '@/components/ui/SettingsControls'

const PAGE_SIZE = 25

const OUTCOME_TONE: Record<string, 'default' | 'green' | 'amber' | 'blue' | 'red'> = {
  completed: 'green',
  hangup: 'default',
  pin_failed: 'red',
  pin_cooldown: 'amber',
  pin_timeout: 'amber',
  rejected_capacity: 'amber',
  no_answer: 'default',
  busy: 'default',
  failed: 'red',
  error: 'red',
}

const OUTCOME_LABEL: Record<string, string> = {
  completed: 'Completed',
  hangup: 'Hung up',
  pin_failed: 'Wrong PIN',
  pin_cooldown: 'PIN locked out',
  pin_timeout: 'PIN timeout',
  rejected_capacity: 'At capacity',
  no_answer: 'No answer',
  busy: 'Busy',
  failed: 'Failed',
  error: 'Error',
}

function fmtDuration(s: number | null): string {
  if (s == null) return '—'
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

export default function CallLogModal({ route, onClose }: {
  route: PhoneRoute
  onClose: () => void
}) {
  const [offset, setOffset] = useState(0)
  const { data, isLoading } = usePhoneRouteCallLog(route.id, true, offset, PAGE_SIZE)
  const calls = data?.calls ?? []
  const total = data?.total ?? 0

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4" onClick={onClose}>
      <div
        className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light max-w-3xl w-full max-h-[85vh] overflow-auto"
        onClick={e => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-p-border-light flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-p-text">Call log</h2>
            <p className="text-xs text-p-text-light">
              {route.name || route.id} — last 30 days, refreshes automatically
            </p>
          </div>
          <button onClick={onClose} className="text-p-text-secondary hover:text-p-text text-xl leading-none">&times;</button>
        </div>

        <div className="p-4">
          {isLoading ? (
            <div className="text-center py-10 text-p-text-light text-sm">Loading…</div>
          ) : calls.length === 0 ? (
            <div className="text-center py-10 text-p-text-light text-sm">
              No calls recorded yet. Rows appear here as calls end — including
              callers who failed the PIN.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-sm">
                <thead className="bg-p-bg text-left text-xs text-p-text-secondary uppercase tracking-wide border-b border-p-border-light">
                  <tr>
                    <th className="px-3 py-2">When</th>
                    <th className="px-3 py-2">Direction</th>
                    <th className="px-3 py-2">Number</th>
                    <th className="px-3 py-2">Outcome</th>
                    <th className="px-3 py-2 text-right">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.map(c => (
                    <tr key={c.id} className="border-b border-p-border-light last:border-0 hover:bg-p-surface-hover transition-colors">
                      <td className="px-3 py-2 whitespace-nowrap text-p-text-secondary" title={c.started_at}>
                        {formatRelativeTime(c.started_at)}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant={c.direction === 'inbound' ? 'blue' : 'default'}>{c.direction}</Badge>
                      </td>
                      <td className="px-3 py-2 font-mono text-p-text">
                        {(c.direction === 'inbound' ? c.from_number : c.to_number) || '(unknown)'}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant={OUTCOME_TONE[c.outcome] ?? 'default'}>
                          {OUTCOME_LABEL[c.outcome] ?? c.outcome}
                        </Badge>
                        {c.pin_attempts > 0 && c.outcome.startsWith('pin_') && (
                          <span className="ml-1.5 text-xs text-p-text-light">
                            {c.pin_attempts} {c.pin_attempts === 1 ? 'attempt' : 'attempts'}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-right text-p-text-secondary whitespace-nowrap">
                        {fmtDuration(c.duration_s)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {total > PAGE_SIZE && (
            <div className="flex items-center justify-between mt-3 text-xs text-p-text-secondary">
              <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
              <div className="space-x-2">
                <button
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  className="px-2 py-1 rounded-sm border border-p-border-light disabled:opacity-40 hover:bg-p-surface-hover"
                >Previous</button>
                <button
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                  className="px-2 py-1 rounded-sm border border-p-border-light disabled:opacity-40 hover:bg-p-surface-hover"
                >Next</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
