import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { useAgents } from '../../api/agents'
import {
  REPLY_REPORT_REASONS, REPORT_LIST, REPORT_STATE, useDeleteReport, useReplyReports, useSetReportState,
  type ReplyReport, type ReplyReportState,
} from '../../api/replyReports'
import { formatRelativeTime } from '../../lib/format'
import { isTaskChatId } from '../../lib/session/kind'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, BUTTON_DANGER_QUIET, CARD } from '../../components/ui/classes'
import { PAGE_TITLE } from '../../components/ui/classes'

/**
 * Admin → Reports: the replies people reported from the flag under an agent's
 * reply, newest first, 50 at a time. Who reported, which agent spoke and when,
 * the reason, the note, the reply's text as the server read it (folded until
 * opened) and a link to the chat at the reply. An admin marks a report
 * handled or dismisses it into the collapsed list at the bottom ("Closed ·
 * N"), restores it from there, or deletes it after a confirm. A report
 * outlives its chat ("The chat was deleted.") and goes a year after it was
 * filed. Acting on a report happens in the chat, the agent's settings or
 * Users.
 */

const REASON_LABEL: Record<string, string> = Object.fromEntries(
  REPLY_REPORT_REASONS.map((r) => [r.value, r.label]),
)

/** A reply's time as its bubble header shows it: "21 Oct, 14:32". */
function replyTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const mon = d.toLocaleString('en', { month: 'short' })
  return `${d.getDate()} ${mon}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/** The chat the report is about, opened at the reply, the way its
 *  notification opens it. */
function chatPath(r: ReplyReport): string {
  const base = `/chat/${r.chat_agent}/${r.chat_id}`
  return isTaskChatId(r.chat_id) ? `${base}?tasks=1&message=${r.message_id}` : `${base}?message=${r.message_id}`
}

/** How a closed report's line names its state ("handled by Ana"). */
const CLOSED_AS: Record<ReplyReportState, string> = {
  [REPORT_STATE.OPEN]: '', [REPORT_STATE.HANDLED]: 'handled', [REPORT_STATE.DISMISSED]: 'dismissed',
}

export default function ReportsPage() {
  const open = useReplyReports(REPORT_LIST.OPEN)
  const [closedOpen, setClosedOpen] = useState(false)
  const closedList = useReplyReports(REPORT_LIST.CLOSED, closedOpen)
  const setState = useSetReportState()
  const remove = useDeleteReport()
  const [actionError, setActionError] = useState('')
  const { data: agents } = useAgents({ all: true })
  const agentName = useMemo(() => {
    const out: Record<string, string> = {}
    for (const a of agents || []) out[a.name] = a.display_name || a.name
    return out
  }, [agents])
  const reports = open.data?.pages.flatMap((p) => p.reports) ?? []
  const closed = closedList.data?.pages.flatMap((p) => p.reports) ?? []
  const closedCount = open.data?.pages[0]?.closed_count ?? 0

  const onError = (e: unknown, fallback: string) =>
    setActionError(e instanceof Error && e.message ? e.message : fallback)
  const move = (r: ReplyReport, state: ReplyReportState) => {
    setActionError('')
    setState.mutate({ id: r.id, state }, { onError: (e) => onError(e, 'The report was not changed.') })
  }
  const del = (r: ReplyReport) => {
    setActionError('')
    remove.mutate({ id: r.id }, { onError: (e) => onError(e, 'The report was not deleted.') })
  }
  const busy = (r: ReplyReport) => (setState.isPending && setState.variables?.id === r.id)
    || (remove.isPending && remove.variables?.id === r.id)
  const row = (r: ReplyReport) => (
    <ReportRow key={r.id} r={r} agentName={agentName[r.agent] || r.agent} busy={busy(r)}
      onMove={(state) => move(r, state)} onDelete={() => del(r)} />
  )

  return (
    <div className="space-y-4">
      <div>
        <h1 className={PAGE_TITLE}>Reports</h1>
        <p className="text-sm text-secondary mt-1">
          Replies people reported from the flag under an agent&apos;s reply. Admins get a notification.
        </p>
      </div>
      {actionError && <p role="alert" className="text-sm text-status-danger-fg">{actionError}</p>}
      {open.isLoading ? (
        <p className="text-sm text-tertiary">Loading…</p>
      ) : open.error ? (
        <p className="text-sm text-status-danger-fg">{(open.error as Error).message}</p>
      ) : !reports.length ? (
        <p className="text-sm text-tertiary">{closedCount ? 'No open reports.' : 'No reports yet.'}</p>
      ) : (
        <ul className="space-y-2" aria-label="Reports">{reports.map(row)}</ul>
      )}
      {open.hasNextPage && (
        <ShowMore loading={open.isFetchingNextPage} onClick={() => void open.fetchNextPage()} />
      )}
      {(closedCount > 0 || closedOpen) && (
        <section className="border-t border-subtle pt-3" aria-label="Closed reports">
          <Button variant="secondary" size="xs" type="button" onClick={() => setClosedOpen((o) => !o)} aria-expanded={closedOpen}>
            Closed · {closedCount}
            <svg className={`h-3 w-3 transition-transform ${closedOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </Button>
          {closedOpen && (
            <div className="mt-3 space-y-2">
              {closedList.isLoading ? (
                <p className="text-sm text-tertiary">Loading…</p>
              ) : closedList.error ? (
                <p className="text-sm text-status-danger-fg">{(closedList.error as Error).message}</p>
              ) : !closed.length ? (
                <p className="text-sm text-tertiary">No closed reports.</p>
              ) : (
                <ul className="space-y-2" aria-label="Closed reports">{closed.map(row)}</ul>
              )}
              {closedList.hasNextPage && (
                <ShowMore loading={closedList.isFetchingNextPage} onClick={() => void closedList.fetchNextPage()} />
              )}
            </div>
          )}
        </section>
      )}
    </div>
  )
}

function ShowMore({ loading, onClick }: { loading: boolean; onClick: () => void }) {
  return (
    <Button variant="secondary" size="sm" type="button" onClick={onClick} disabled={loading}>
      {loading ? 'Loading…' : 'Show more'}
    </Button>
  )
}

const ACTION = 'rounded-control px-2 py-0.5 font-medium text-secondary transition-colors hover:bg-surface-hover disabled:opacity-50'

function ReportRow({ r, agentName, busy, onMove, onDelete }: {
  r: ReplyReport; agentName: string; busy: boolean
  onMove: (state: ReplyReportState) => void; onDelete: () => void
}) {
  const [open, setOpen] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const when = r.reply_at ? replyTime(r.reply_at) : ''
  return (
    <li className={`${CARD} px-4 py-3`} data-testid="reply-report">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className={`${BADGE_BASE} ${BADGE_TONE.warning}`}>
          {REASON_LABEL[r.reason] || r.reason}
        </span>
        <span className="text-sm text-primary">
          <span className="font-medium">{r.reporter_name}</span> reported a reply of{' '}
          <span className="font-medium">{agentName}</span>{when ? ` from ${when}` : ''}
        </span>
        <span className="text-xs text-tertiary">{formatRelativeTime(r.created_at)}</span>
      </div>
      <p className="mt-1 text-xs text-tertiary">
        In “{r.chat_title || 'an untitled chat'}”
        {r.state !== REPORT_STATE.OPEN && r.closed_at
          && ` · ${CLOSED_AS[r.state]} by ${r.closed_by_name || 'an admin'} ${formatRelativeTime(r.closed_at)}`}
      </p>
      {r.note && <p className="mt-2 whitespace-pre-wrap break-words text-sm text-secondary">“{r.note}”</p>}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
          className="font-medium text-link hover:underline">
          {open ? 'Hide the reply' : 'Show the reply'}
        </button>
        {r.chat_id
          ? <Link to={chatPath(r)} className="font-medium text-link hover:underline">Open chat</Link>
          : <span className="text-tertiary">The chat was deleted.</span>}
        <span className="ml-auto flex flex-wrap items-center gap-1">
          {r.state === REPORT_STATE.OPEN ? (
            <>
              <button type="button" onClick={() => onMove(REPORT_STATE.HANDLED)} disabled={busy} className={ACTION}>Handled</button>
              <button type="button" onClick={() => onMove(REPORT_STATE.DISMISSED)} disabled={busy} className={ACTION}>Dismiss</button>
            </>
          ) : (
            <button type="button" onClick={() => onMove(REPORT_STATE.OPEN)} disabled={busy} className={ACTION}>Restore</button>
          )}
          <button type="button" onClick={() => setConfirming(true)} disabled={busy} aria-expanded={confirming}
            className={BUTTON_DANGER_QUIET}>Delete</button>
        </span>
      </div>
      {confirming && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-card bg-surface-muted px-3 py-2.5 text-xs" data-testid="report-delete-confirm">
          <span className="text-primary">Delete the report and its copy of the reply? This cannot be undone.</span>
          <div className="ml-auto flex shrink-0 items-center gap-2">
            <Button variant="danger" size="sm" type="button" onClick={() => { setConfirming(false); onDelete() }}>
              Delete the report
            </Button>
            <Button variant="ghost" size="sm" type="button" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      {open && (
        <p className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded-card bg-surface-sunken px-3 py-2 text-sm text-primary">
          {r.reply_text}
        </p>
      )}
    </li>
  )
}
