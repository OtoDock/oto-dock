// Per-route call log viewer — opened from the small icon on a route row.
// A popup (not a page/menu item): route-scoped detail, following the
// AgentConversations table + McpRequestsPage outcome-tone conventions.
// Shows every reported call including the ones that never reached the agent
// (failed/locked-out PIN entries, capacity rejects) with the caller's number.

import { useState } from 'react'
import { usePhoneRouteCallLog, PHONE_INBOUND, type PhoneRoute } from '@/api/phone'
import { formatRelativeTime } from '@/lib/format'
import { Badge } from '@/components/ui/SettingsControls'
import Dialog from '../ui/Dialog'
import { DIALOG_PANEL } from '../ui/classes'
import Button from '../ui/Button'

const PAGE_SIZE = 25

const OUTCOME_TONE: Record<string, 'neutral' | 'success' | 'warning' | 'info' | 'danger'> = {
  completed: 'success',
  hangup: 'neutral',
  pin_failed: 'danger',
  pin_cooldown: 'warning',
  pin_timeout: 'warning',
  rejected_capacity: 'warning',
  no_answer: 'neutral',
  busy: 'neutral',
  failed: 'danger',
  error: 'danger',
  transferred: 'info',
  transfer_failed: 'danger',
}

const OUTCOME_LABEL: Record<string, string> = {
  completed: 'Completed',
  transferred: 'Transferred',
  transfer_failed: 'Transfer failed',
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

// The identity label stamped at warmup (`caller:<id>` / `caller-pin:<id>` /
// `ephemeral` / `shared` / `user:<username>`); the number itself is in the
// Number column, so the cell says WHO the session ran as.
function fmtIdentity(label: string): string {
  if (!label) return '—'
  const [kind, rest] = label.includes(':') ? [label.slice(0, label.indexOf(':')), label.slice(label.indexOf(':') + 1)] : [label, '']
  switch (kind) {
    case 'caller': return 'Caller'
    case 'caller-pin': return 'Caller with PIN'
    case 'ephemeral': return 'Anonymous'
    case 'shared': return 'Shared'
    case 'user': return rest ? `User ${rest}` : 'User'
    default: return label
  }
}

// `mcp__memory-mcp__memory` → `memory-mcp/memory`; built-ins stay as-is.
function fmtTool(name: string): string {
  const m = /^mcp__(.+?)__(.+)$/.exec(name)
  return m ? `${m[1]}/${m[2]}` : name
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
    <Dialog
      onClose={onClose}
      aria-label="Call log"
      panelClassName={`${DIALOG_PANEL} max-w-3xl max-h-[85vh] overflow-auto`}
    >
        <div className="px-4 py-3 border-b border-subtle flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-primary">Call log</h2>
            <p className="text-xs text-tertiary">
              {route.name || route.id} · Kept as long as caller data. Refreshes on its own.
            </p>
          </div>
          <button onClick={onClose} className="text-secondary hover:text-primary text-xl leading-none">&times;</button>
        </div>

        <div className="p-4">
          {isLoading ? (
            <div className="text-center py-10 text-tertiary text-sm">Loading…</div>
          ) : calls.length === 0 ? (
            <div className="text-center py-10 text-tertiary text-sm">
              No calls recorded yet. A row appears here when a call ends.
              Callers who failed the PIN show too.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead className="bg-surface-sunken text-left text-xs text-secondary uppercase tracking-wide border-b border-subtle">
                  <tr>
                    <th className="px-3 py-2">When</th>
                    <th className="px-3 py-2">Direction</th>
                    <th className="px-3 py-2">Number</th>
                    <th className="px-3 py-2">Identity</th>
                    <th className="px-3 py-2">Outcome</th>
                    <th className="px-3 py-2">Tools</th>
                    <th className="px-3 py-2 text-right">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.map(c => (
                    <tr key={c.id} className="border-b border-subtle last:border-0 hover:bg-surface-hover transition-colors">
                      <td className="px-3 py-2 whitespace-nowrap text-secondary" title={c.started_at}>
                        {formatRelativeTime(c.started_at)}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant={c.direction === PHONE_INBOUND ? 'info' : 'neutral'}>{c.direction}</Badge>
                      </td>
                      <td className="px-3 py-2 font-mono text-primary">
                        {(c.direction === PHONE_INBOUND ? c.from_number : c.to_number) || 'Unknown'}
                      </td>
                      <td className="px-3 py-2 text-secondary whitespace-nowrap" title={c.identity || undefined}>
                        {fmtIdentity(c.identity)}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant={OUTCOME_TONE[c.outcome] ?? 'neutral'}>
                          {OUTCOME_LABEL[c.outcome] ?? c.outcome}
                        </Badge>
                        {c.pin_attempts > 0 && c.outcome.startsWith('pin_') && (
                          <span className="ml-1.5 text-xs text-tertiary">
                            {c.pin_attempts} {c.pin_attempts === 1 ? 'attempt' : 'attempts'}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-xs text-secondary max-w-[220px] truncate"
                        title={(c.tools_run ?? []).map(fmtTool).join(', ') || undefined}>
                        {(c.tools_run ?? []).length ? (c.tools_run ?? []).map(fmtTool).join(', ') : <span className="text-tertiary">—</span>}
                      </td>
                      <td className="px-3 py-2 text-right text-secondary whitespace-nowrap">
                        {fmtDuration(c.duration_s)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {total > PAGE_SIZE && (
            <div className="flex items-center justify-between mt-3 text-xs text-secondary">
              <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
              <div className="space-x-2">
                <Button
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >Previous</Button>
                <Button
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >Next</Button>
              </div>
            </div>
          )}
        </div>
    </Dialog>
  )
}
