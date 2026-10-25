/**
 * Admin Pending MCP Requests page.
 *
 * Lists every assignment request across all agents. Pending rows get
 * inline Approve / Reject actions; failed-install rows get Retry. Resolved
 * rows (installed / rejected / cancelled) are kept visible for an audit
 * trail — toggleable via the "Open only" filter.
 *
 * Explicit-mode MCPs: a row whose only blocker is admin instance work
 * carries the derived `needs_instance` flag — rendered as an amber "Needs
 * instance" chip (it was never a failure), and its approve/retry dialog
 * offers an instance selector (or create-instance guidance when none
 * exist). The chosen instance rides the approve call as `instance_id`.
 */

import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  useAdminMcpRequests,
  useApproveMcpRequest,
  useRejectMcpRequest,
  McpRequest,
  RequestStatus,
} from '../../api/community'
import { useMcpInstances } from '../../api/mcps'
import { MCP_REQUEST_STATUS } from '../../lib/status/mcpRequest'
import Dialog from '../../components/ui/Dialog'
import Button from '../../components/ui/Button'
import { Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD, CARD_BORDERED, DIALOG_PANEL, FIELD, FIELD_BORDER, FIELD_REGULAR } from '../../components/ui/classes'
import { PAGE_TITLE } from '../../components/ui/classes'

const NEEDS_INSTANCE_TONE = BADGE_TONE.warning

const STATUS_TONE: Record<RequestStatus, string> = {
  pending:        BADGE_TONE.warning,
  approved:       BADGE_TONE.info,
  installing:     BADGE_TONE.info,
  installed:      BADGE_TONE.success,
  install_failed: BADGE_TONE.danger,
  rejected:       BADGE_TONE.neutral,
  cancelled:      BADGE_TONE.neutral,
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
  const [resolving, setResolving] = useState<{ req: McpRequest; action: 'approve' | 'reject' } | null>(null)
  const [note, setNote] = useState('')
  // null = "Automatic" (server picks: catch-all / already attached / lowest id)
  const [instanceId, setInstanceId] = useState<number | null>(null)
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

  const openResolve = (req: McpRequest, action: 'approve' | 'reject') => {
    setResolving({ req, action })
    setNote('')
    setInstanceId(null)
    setError(null)
  }

  const submitResolve = () => {
    if (!resolving) return
    setError(null)
    const opts = {
      onSuccess: () => setResolving(null),
      onError: (e: Error) => setError(e?.message || 'The request was not updated. Try again.'),
    }
    if (resolving.action === 'approve') {
      approve.mutate({ id: resolving.req.id, admin_note: note, instance_id: instanceId }, opts)
    } else {
      reject.mutate({ id: resolving.req.id, admin_note: note }, opts)
    }
  }

  const onRetry = (req: McpRequest) => {
    // A needs-instance row retried blind would just land back where it is —
    // route it through the dialog so the selector (or the create-instance
    // guidance) is in front of the admin. Plain failures keep 1-click retry.
    if (req.needs_instance) {
      openResolve(req, 'approve')
      return
    }
    setError(null)
    approve.mutate({ id: req.id, admin_note: '' }, {
      onError: e => setError((e as Error)?.message || 'The install was not retried. Try again.'),
    })
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 sm:gap-3 mb-4">
        <h1 className={PAGE_TITLE}>MCP Requests</h1>
        {data?.pending_count ? (
          <span className={`${BADGE_BASE} ${BADGE_TONE.warning}`}>
            {data.pending_count} pending
          </span>
        ) : null}
        <div className="flex-1 min-w-0" />
        <label className="text-xs text-secondary flex items-center gap-1.5">
          <input
            type="checkbox"
            checked={openOnly}
            onChange={e => setOpenOnly(e.target.checked)}
            className="w-3.5 h-3.5 accent-action"
          />
          Open only
        </label>
      </div>

      {error && (
        <div className="mb-3 rounded-card border border-status-danger-border bg-status-danger-bg p-3 text-sm text-status-danger-fg">
          {error}
        </div>
      )}

      {isLoading && <div className="text-sm text-tertiary">Loading requests...</div>}
      {!isLoading && requests.length === 0 && (
        <div className="text-sm text-tertiary">No requests.</div>
      )}

      <div className="space-y-2">
        {groups.map(group =>
          group.isBatch ? (
            <BatchCard
              key={group.key}
              rows={group.rows}
              onApprove={r => openResolve(r, 'approve')}
              onReject={r => openResolve(r, 'reject')}
              onRetry={r => onRetry(r)}
              pending={approve.isPending || reject.isPending}
            />
          ) : (
            <Row
              key={group.key}
              req={group.rows[0]}
              onApprove={() => openResolve(group.rows[0], 'approve')}
              onReject={() => openResolve(group.rows[0], 'reject')}
              onRetry={() => onRetry(group.rows[0])}
              pending={approve.isPending || reject.isPending}
            />
          ),
        )}
      </div>

      {resolving && (
        <ResolveModal
          req={resolving.req}
          action={resolving.action}
          note={note}
          onChangeNote={setNote}
          instanceId={instanceId}
          onChangeInstanceId={setInstanceId}
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
  onApprove: (req: McpRequest) => void
  onReject: (req: McpRequest) => void
  onRetry: (req: McpRequest) => void
  pending: boolean
}) {
  const [collapsed, setCollapsed] = useState(false)
  const total = rows.length
  const installed = rows.filter(r => r.status === MCP_REQUEST_STATUS.INSTALLED).length
  const needsInst = rows.filter(r => r.status === MCP_REQUEST_STATUS.INSTALL_FAILED && r.needs_instance).length
  const failed = rows.filter(r => r.status === MCP_REQUEST_STATUS.INSTALL_FAILED && !r.needs_instance).length
  const open = rows.filter(r => r.status === MCP_REQUEST_STATUS.PENDING).length
  const requester = rows[0]
  const agent = rows[0].agent_slug
  return (
    <div className={`${CARD} overflow-hidden`}>
      <button
        type="button"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed(c => !c)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-surface-hover"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-3xs uppercase font-semibold text-tertiary">Batch</span>
          <span className="text-sm font-medium text-primary">
            Community-agent install for{' '}
            <span className="text-primary">{agent}</span>
          </span>
          <span className="text-xs text-secondary">
            {installed}/{total} done
            {failed > 0 ? `, ${failed} failed` : ''}
            {needsInst > 0 ? `, ${needsInst} need instance` : ''}
            {open > 0 ? `, ${open} pending` : ''}
          </span>
          <span className="text-2xs text-tertiary">
            · by {requester.requested_by_name || requester.requested_by_email || requester.requested_by}
          </span>
        </div>
        <svg
          className={`w-4 h-4 text-tertiary transition-transform ${collapsed ? '' : 'rotate-180'}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {!collapsed && (
        <div className="border-t border-subtle p-2 space-y-2">
          {rows.map(r => (
            <Row
              key={r.id}
              req={r}
              inGroup
              onApprove={() => onApprove(r)}
              onReject={() => onReject(r)}
              onRetry={() => onRetry(r)}
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
  req, onApprove, onReject, onRetry, pending, inGroup = false,
}: {
  req: McpRequest
  inGroup?: boolean
  onApprove: () => void
  onReject: () => void
  onRetry: () => void
  pending: boolean
}) {
  const isPending = req.status === MCP_REQUEST_STATUS.PENDING
  const isFailed = req.status === MCP_REQUEST_STATUS.INSTALL_FAILED
  // install_failed + needs_instance is not a failure — the install went
  // fine, only admin instance work remains. Present it as its own state.
  const needsInstance = isFailed && !!req.needs_instance
  // Which catalog the request targets — rows created before the skills
  // feature have no `kind` and default to 'mcp'.
  const kind = req.kind ?? 'mcp'
  return (
    <div className={`${inGroup ? CARD_BORDERED : CARD} p-3`}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span
              className={`${BADGE_BASE} ${BADGE_TONE.info}`}
              title={kind === 'skill' ? 'Community skill package request' : 'Community MCP request'}
            >
              {kind === 'skill' ? 'Skill' : 'MCP'}
            </span>
            <span className="text-sm font-medium text-primary">{req.mcp_name}</span>
            <span className="text-xs text-tertiary">for</span>
            <span className="text-sm font-medium text-primary">{req.agent_slug}</span>
            <span className={`${BADGE_BASE} ${needsInstance ? NEEDS_INSTANCE_TONE : STATUS_TONE[req.status]}`}>
              {needsInstance ? 'Needs instance' : STATUS_LABEL[req.status]}
            </span>
            {isPending && !!req.needs_instance && (
              <span
                className={`${BADGE_BASE} ${NEEDS_INSTANCE_TONE}`}
                title="This MCP has no instance for this agent yet. Approving asks you to pick or create one."
              >
                Needs instance
              </span>
            )}
          </div>
          <p className="text-2xs text-tertiary mt-1">
            Requested by{' '}
            <span className="text-secondary" title={req.requested_by_email || req.requested_by}>
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
                    <span className="text-secondary" title={req.resolved_by_email || req.resolved_by || ''}>
                      {req.resolved_by_name}
                    </span>
                  </>
                )}
              </>
            )}
          </p>
          {req.reason && (
            <p className="text-xs text-secondary mt-1.5">
              <span className="text-tertiary">Reason:</span> {req.reason}
            </p>
          )}
          {req.admin_note && (
            <p className="text-xs text-secondary mt-1.5">
              <span className="text-tertiary">Admin note:</span> {req.admin_note}
            </p>
          )}
          {(isFailed || req.status === MCP_REQUEST_STATUS.INSTALLED) && req.install_log && (
            <details className="mt-1.5">
              <summary className="text-2xs text-tertiary cursor-pointer">Install log</summary>
              <pre className={`mt-1 text-3xs rounded-control p-2 overflow-x-auto max-h-40 whitespace-pre-wrap ${
                needsInstance
                  ? 'text-status-warning-fg bg-status-warning-bg'
                  : isFailed
                    ? 'text-status-danger-fg bg-status-danger-bg'
                    : 'text-secondary bg-surface-muted'
              }`}>{req.install_log}</pre>
            </details>
          )}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {isPending && (
            <>
              <Button variant="primary" size="xs" onClick={onApprove} disabled={pending}>
                Approve
              </Button>
              <Button size="xs" onClick={onReject} disabled={pending}>
                Reject
              </Button>
            </>
          )}
          {isFailed && (
            <Button variant="secondary" size="xs" onClick={onRetry} disabled={pending}>
              {needsInstance ? 'Assign instance…' : 'Retry install'}
            </Button>
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
  req, action, note, onChangeNote, instanceId, onChangeInstanceId, onSubmit, onClose, submitting,
}: {
  req: McpRequest
  action: 'approve' | 'reject'
  note: string
  onChangeNote: (s: string) => void
  instanceId: number | null
  onChangeInstanceId: (id: number | null) => void
  onSubmit: () => void
  onClose: () => void
  submitting: boolean
}) {
  // Explicit-mode MCPs get an instance selector on approve (or, with zero
  // instances, guidance instead — approving anyway is allowed and holds the
  // request as "Needs instance" until an instance covers the agent, at
  // which point the instance-save hook auto-completes it).
  const wantsInstanceUi =
    action === 'approve' && (req.kind ?? 'mcp') === 'mcp' && req.assignment_mode === 'explicit'
  const { data: instData } = useMcpInstances(wantsInstanceUi ? req.mcp_name : '')
  const instances = instData?.instances ?? []
  const title = action === 'approve' ? 'Approve request' : 'Reject request'
  const submitLabel = action === 'approve' ? 'Approve' : 'Reject'

  return (
    <Dialog
      onClose={onClose}
      busy={submitting}
      aria-label={title}
      panelClassName={`${DIALOG_PANEL} max-w-md`}
    >
        <div className="flex items-center justify-between px-5 py-4 border-b border-subtle">
          <h3 className="text-base font-semibold text-primary">{title}</h3>
          <button onClick={onClose} className="text-tertiary hover:text-primary text-lg leading-none">&times;</button>
        </div>
        <div className="p-5 space-y-3">
          <label className="block">
            <span className="text-xs text-secondary">
              {action === 'approve'
                ? 'Optional note shown to the requester.'
                : 'Optional reason shown to the requester.'}
            </span>
            <textarea
              value={note}
              onChange={e => onChangeNote(e.target.value)}
              rows={3}
              className={`${FIELD} ${FIELD_REGULAR} ${FIELD_BORDER} mt-1`}
              placeholder={action === 'approve' ? 'e.g. Enabled. The Google Maps API key is set under Integrations.' : 'e.g. Use the existing email-server instead. It does the same.'}
            />
          </label>
          {wantsInstanceUi && instances.length > 0 && (
            <label className="block">
              <span className="text-xs text-secondary">
                Instance to authorize <span className="font-medium">{req.agent_slug}</span> on
              </span>
              <Select
                value={instanceId === null ? '' : String(instanceId)}
                onChange={e => onChangeInstanceId(e.target.value === '' ? null : Number(e.target.value))}
                className="mt-1"
              >
                <option value="">Automatic: the catch-all or the first instance</option>
                {instances.map(i => (
                  <option key={i.id} value={String(i.id)}>
                    {i.instance_name}{i.assigned_to_all ? ' · all agents' : ''}
                  </option>
                ))}
              </Select>
            </label>
          )}
          {wantsInstanceUi && instances.length === 0 && (
            <p className={`text-2xs rounded-card p-2 ${NEEDS_INSTANCE_TONE}`}>
              <span className="font-medium">{req.mcp_name}</span> has no instances yet.
              You can approve now. The request then waits as “Needs instance”.
              Create an instance in Admin,{' '}
              <Link to="/admin/mcp-servers" className="underline">MCP Servers</Link>,{' '}
              {req.mcp_name}, Instances. The request completes once an instance covers the agent.
            </p>
          )}
          {action === 'approve' && !wantsInstanceUi && (
            <p className="text-2xs text-tertiary">
              Approval will install the MCP if needed and enable it for the requesting agent.
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2 px-5 py-3 border-t border-subtle">
          <Button onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant={action === 'approve' ? 'primary' : 'danger'} onClick={onSubmit} disabled={submitting}>
            {submitting ? '...' : submitLabel}
          </Button>
        </div>
    </Dialog>
  )
}
