/**
 * Admin Pending MCP Requests page.
 *
 * Lists every assignment request across all agents. Pending rows get
 * inline Approve / Reject actions; failed-install rows get Retry. Resolved
 * rows (installed / rejected / cancelled) are kept visible for an audit
 * trail — toggleable via the "Open only" filter.
 */

import { useMemo, useState } from 'react'
import {
  useAdminMcpRequests,
  useApproveMcpRequest,
  useRejectMcpRequest,
  McpRequest,
  RequestStatus,
} from '../../api/community'

const STATUS_TONE: Record<RequestStatus, string> = {
  pending:        'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400',
  approved:       'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400',
  installing:     'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400',
  installed:      'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400',
  install_failed: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
  rejected:       'bg-gray-200 dark:bg-gray-800 text-p-text-light',
  cancelled:      'bg-gray-200 dark:bg-gray-800 text-p-text-light',
}

const STATUS_LABEL: Record<RequestStatus, string> = {
  pending:        'Pending',
  approved:       'Approved',
  installing:     'Installing',
  installed:      'Installed',
  install_failed: 'Install failed',
  rejected:       'Rejected',
  cancelled:      'Cancelled',
}

export default function McpRequestsPage() {
  const [openOnly, setOpenOnly] = useState(true)
  const { data, isLoading } = useAdminMcpRequests(openOnly)
  const approve = useApproveMcpRequest()
  const reject = useRejectMcpRequest()
  const [resolving, setResolving] = useState<{ id: number; action: 'approve' | 'reject' } | null>(null)
  const [note, setNote] = useState('')
  const [error, setError] = useState<string | null>(null)

  const requests = useMemo(() => data?.requests ?? [], [data])

  // Group rows by ``batch_id``. Singleton requests (batch_id=null)
  // get a synthetic group of one row each so the rendering loop stays
  // uniform. Batches preserve creation order via ``id`` ascending.
  type Group = { key: string; isBatch: boolean; rows: McpRequest[] }
  const groups: Group[] = useMemo(() => {
    const byBatch = new Map<string, McpRequest[]>()
    const singles: McpRequest[] = []
    for (const r of requests) {
      if (r.batch_id) {
        const arr = byBatch.get(r.batch_id) ?? []
        arr.push(r)
        byBatch.set(r.batch_id, arr)
      } else {
        singles.push(r)
      }
    }
    const out: Group[] = []
    // Order batches by the most recent row (descending). Singles interleaved
    // by their created_at; keep already-newest-first ordering from API.
    const merged: { sortKey: string; group: Group }[] = []
    byBatch.forEach((rows, key) => {
      const sorted = [...rows].sort((a, b) => a.id - b.id)
      const newest = sorted.reduce((m, r) => (r.created_at > m ? r.created_at : m), '')
      merged.push({ sortKey: newest, group: { key: `batch:${key}`, isBatch: true, rows: sorted } })
    })
    singles.forEach(r => {
      merged.push({ sortKey: r.created_at, group: { key: `single:${r.id}`, isBatch: false, rows: [r] } })
    })
    merged.sort((a, b) => b.sortKey.localeCompare(a.sortKey))
    return merged.map(m => m.group)
  }, [requests])

  const openResolve = (id: number, action: 'approve' | 'reject') => {
    setResolving({ id, action })
    setNote('')
    setError(null)
  }

  const submitResolve = () => {
    if (!resolving) return
    setError(null)
    const args = { id: resolving.id, admin_note: note }
    const mut = resolving.action === 'approve' ? approve : reject
    mut.mutate(args, {
      onSuccess: () => setResolving(null),
      onError: e => setError((e as Error)?.message || 'Action failed'),
    })
  }

  const onRetry = (id: number) => {
    setError(null)
    approve.mutate({ id, admin_note: '' }, {
      onError: e => setError((e as Error)?.message || 'Retry failed'),
    })
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4">
        <h1 className="text-lg font-bold text-p-text">MCP Requests</h1>
        {data?.pending_count ? (
          <span className="text-xs px-2 py-0.5 rounded-lg bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400 font-medium">
            {data.pending_count} pending
          </span>
        ) : null}
        <div className="flex-1 min-w-0" />
        <label className="text-xs text-p-text-secondary flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={openOnly}
            onChange={e => setOpenOnly(e.target.checked)}
            className="w-3.5 h-3.5 rounded-sm border-gray-300 text-brand focus:ring-brand accent-brand"
          />
          Open only
        </label>
      </div>

      {error && (
        <div className="mb-3 rounded-lg border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {isLoading && <div className="text-sm text-p-text-light">Loading requests...</div>}
      {!isLoading && requests.length === 0 && (
        <div className="text-sm text-p-text-light">No requests.</div>
      )}

      <div className="space-y-2">
        {groups.map(group =>
          group.isBatch ? (
            <BatchCard
              key={group.key}
              rows={group.rows}
              onApprove={id => openResolve(id, 'approve')}
              onReject={id => openResolve(id, 'reject')}
              onRetry={id => onRetry(id)}
              pending={approve.isPending || reject.isPending}
            />
          ) : (
            <Row
              key={group.key}
              req={group.rows[0]}
              onApprove={() => openResolve(group.rows[0].id, 'approve')}
              onReject={() => openResolve(group.rows[0].id, 'reject')}
              onRetry={() => onRetry(group.rows[0].id)}
              pending={approve.isPending || reject.isPending}
            />
          ),
        )}
      </div>

      {resolving && (
        <ResolveModal
          action={resolving.action}
          note={note}
          onChangeNote={setNote}
          onSubmit={submitResolve}
          onClose={() => setResolving(null)}
          submitting={approve.isPending || reject.isPending}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// BatchCard — community-agent install cascade groups N rows together
// ---------------------------------------------------------------------------

function BatchCard({
  rows, onApprove, onReject, onRetry, pending,
}: {
  rows: McpRequest[]
  onApprove: (id: number) => void
  onReject: (id: number) => void
  onRetry: (id: number) => void
  pending: boolean
}) {
  const [collapsed, setCollapsed] = useState(false)
  const total = rows.length
  const installed = rows.filter(r => r.status === 'installed').length
  const failed = rows.filter(r => r.status === 'install_failed').length
  const open = rows.filter(r => r.status === 'pending').length
  const requester = rows[0]
  const agent = rows[0].agent_slug
  return (
    <div className="rounded-lg border border-brand/30 bg-brand/5 dark:bg-brand/10 overflow-hidden">
      <button
        onClick={() => setCollapsed(c => !c)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-brand/10"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[10px] uppercase font-semibold text-brand">Batch</span>
          <span className="text-sm font-medium text-p-text">
            Community-agent install for{' '}
            <span className="text-brand">{agent}</span>
          </span>
          <span className="text-xs text-p-text-secondary">
            ({installed}/{total} done
            {failed > 0 ? `, ${failed} failed` : ''}
            {open > 0 ? `, ${open} pending` : ''})
          </span>
          <span className="text-[11px] text-p-text-light">
            · by {requester.requested_by_name || requester.requested_by_email || requester.requested_by}
          </span>
        </div>
        <svg
          className={`w-4 h-4 text-p-text-light transition-transform ${collapsed ? '' : 'rotate-180'}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {!collapsed && (
        <div className="border-t border-brand/20 p-2 space-y-2 bg-white/40 dark:bg-p-surface/40">
          {rows.map(r => (
            <Row
              key={r.id}
              req={r}
              onApprove={() => onApprove(r.id)}
              onReject={() => onReject(r.id)}
              onRetry={() => onRetry(r.id)}
              pending={pending}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

function Row({
  req, onApprove, onReject, onRetry, pending,
}: {
  req: McpRequest
  onApprove: () => void
  onReject: () => void
  onRetry: () => void
  pending: boolean
}) {
  const isPending = req.status === 'pending'
  const isFailed = req.status === 'install_failed'
  return (
    <div className="rounded-lg border border-p-border-light bg-white dark:bg-p-surface p-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-sm font-medium text-p-text">{req.mcp_name}</span>
            <span className="text-xs text-p-text-light">for</span>
            <span className="text-sm font-medium text-brand">{req.agent_slug}</span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded-sm ${STATUS_TONE[req.status]}`}>
              {STATUS_LABEL[req.status]}
            </span>
          </div>
          <p className="text-[11px] text-p-text-light mt-1">
            Requested by{' '}
            <span className="text-p-text-secondary" title={req.requested_by_email || req.requested_by}>
              {req.requested_by_name || req.requested_by_email || req.requested_by}
            </span>
            {' · '}{new Date(req.created_at).toLocaleString()}
            {req.resolved_at && (
              <>
                {' · resolved '}
                {new Date(req.resolved_at).toLocaleString()}
                {req.resolved_by_name && (
                  <>
                    {' by '}
                    <span className="text-p-text-secondary" title={req.resolved_by_email || req.resolved_by || ''}>
                      {req.resolved_by_name}
                    </span>
                  </>
                )}
              </>
            )}
          </p>
          {req.reason && (
            <p className="text-xs text-p-text-secondary mt-1.5">
              <span className="text-p-text-light">Reason:</span> {req.reason}
            </p>
          )}
          {req.admin_note && (
            <p className="text-xs text-p-text-secondary mt-1.5">
              <span className="text-p-text-light">Admin note:</span> {req.admin_note}
            </p>
          )}
          {isFailed && req.install_log && (
            <details className="mt-1.5">
              <summary className="text-[11px] text-p-text-light cursor-pointer">Install log</summary>
              <pre className="mt-1 text-[10px] text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded-sm p-2 overflow-x-auto max-h-40 whitespace-pre-wrap">{req.install_log}</pre>
            </details>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {isPending && (
            <>
              <button
                onClick={onApprove}
                disabled={pending}
                className="text-xs px-2 py-1 rounded-sm bg-brand text-white hover:bg-brand-hover disabled:opacity-40 transition-colors"
              >
                Approve
              </button>
              <button
                onClick={onReject}
                disabled={pending}
                className="text-xs px-2 py-1 rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover disabled:opacity-40 transition-colors"
              >
                Reject
              </button>
            </>
          )}
          {isFailed && (
            <button
              onClick={onRetry}
              disabled={pending}
              className="text-xs px-2 py-1 rounded-sm border border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-900/20 disabled:opacity-40 transition-colors"
            >
              Retry install
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Resolve modal
// ---------------------------------------------------------------------------

function ResolveModal({
  action, note, onChangeNote, onSubmit, onClose, submitting,
}: {
  action: 'approve' | 'reject'
  note: string
  onChangeNote: (s: string) => void
  onSubmit: () => void
  onClose: () => void
  submitting: boolean
}) {
  const title = action === 'approve' ? 'Approve request' : 'Reject request'
  const submitLabel = action === 'approve' ? 'Approve' : 'Reject'
  const submitColor = action === 'approve'
    ? 'bg-brand text-white hover:bg-brand-hover'
    : 'bg-red-600 text-white hover:bg-red-700'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="bg-white dark:bg-gray-800 rounded-xl shadow-xl border border-p-border-light w-full max-w-md mx-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-p-border-light">
          <h3 className="text-base font-semibold text-p-text">{title}</h3>
          <button onClick={onClose} className="text-p-text-light hover:text-p-text text-lg leading-none">&times;</button>
        </div>
        <div className="p-5 space-y-3">
          <label className="block">
            <span className="text-xs text-p-text-secondary">
              {action === 'approve'
                ? 'Optional note shown to the requester.'
                : 'Optional reason shown to the requester.'}
            </span>
            <textarea
              value={note}
              onChange={e => onChangeNote(e.target.value)}
              rows={3}
              className="mt-1 w-full text-sm px-3 py-2 rounded-lg border border-p-border-light bg-white dark:bg-gray-900 text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/50"
              placeholder={action === 'approve' ? 'e.g. Enabled — Google Maps API key is set under Integrations.' : 'e.g. Use the existing email-server instead — same functionality.'}
            />
          </label>
          {action === 'approve' && (
            <p className="text-[11px] text-p-text-light">
              Approval will install the MCP if needed and enable it for the requesting agent.
              For MCPs that need admin instance config (URL/token), enable here and then configure the instance under MCP Servers.
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 px-5 py-3 border-t border-p-border-light bg-gray-50/50 dark:bg-gray-900/30">
          <button
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-sm rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onSubmit}
            disabled={submitting}
            className={`px-3 py-1.5 text-sm rounded-lg transition-colors disabled:opacity-40 ${submitColor}`}
          >
            {submitting ? '...' : submitLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
