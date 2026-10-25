import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { useAgents } from '../../api/agents'
import {
  REPLY_REPORT_REASONS, REPORT_STATE, useReplyReports, useSetReportDismissed, type ReplyReport,
} from '../../api/replyReports'
import { formatRelativeTime } from '../../lib/format'
import { isTaskChatId } from '../../lib/session/kind'
import Button from '../../components/ui/Button'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'
import { PAGE_TITLE } from '../../components/ui/classes'

/**
 * Admin → Reports: the replies people reported from the flag under an agent's
 * reply, newest first, 50 at a time. Who reported, which agent spoke and when,
 * the reason, the note, the reply's text as the server read it (folded until
 * opened) and a link to the chat. An admin dismisses a report into the
 * collapsed history at the bottom ("Dismissed · N") and restores it from
 * there; nothing is deleted. Acting on a report happens in the chat, the
 * agent's settings or Users.
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

/** The chat the report is about, the way a notification opens it. */
function chatPath(r: ReplyReport): string {
  return isTaskChatId(r.chat_id)
    ? `/chat/${r.chat_agent}/${r.chat_id}?tasks=1`
    : `/chat/${r.chat_agent}/${r.chat_id}`
}

export default function ReportsPage() {
  const open = useReplyReports(REPORT_STATE.OPEN)
  const [historyOpen, setHistoryOpen] = useState(false)
  const history = useReplyReports(REPORT_STATE.DISMISSED, historyOpen)
  const setDismissed = useSetReportDismissed()
  const [actionError, setActionError] = useState('')
  const { data: agents } = useAgents({ all: true })
  const agentName = useMemo(() => {
    const out: Record<string, string> = {}
    for (const a of agents || []) out[a.name] = a.display_name || a.name
    return out
  }, [agents])
  const reports = open.data?.pages.flatMap((p) => p.reports) ?? []
  const dismissed = history.data?.pages.flatMap((p) => p.reports) ?? []
  const dismissedCount = open.data?.pages[0]?.dismissed_count ?? 0

  const act = (r: ReplyReport, dismiss: boolean) => {
    setActionError('')
    setDismissed.mutate({ id: r.id, dismiss }, {
      onError: (e) => setActionError(e instanceof Error ? e.message : 'The change was not saved.'),
    })
  }
  const busy = (r: ReplyReport) => setDismissed.isPending && setDismissed.variables?.id === r.id

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
        <p className="text-sm text-tertiary">{dismissedCount ? 'No open reports.' : 'No reports yet.'}</p>
      ) : (
        <ul className="space-y-2" aria-label="Reports">
          {reports.map((r) => (
            <ReportRow key={r.id} r={r} agentName={agentName[r.agent] || r.agent}
              action="Dismiss" busy={busy(r)} onAction={() => act(r, true)} />
          ))}
        </ul>
      )}
      {open.hasNextPage && (
        <ShowMore loading={open.isFetchingNextPage} onClick={() => void open.fetchNextPage()} />
      )}
      {(dismissedCount > 0 || historyOpen) && (
        <section className="border-t border-subtle pt-3" aria-label="Dismissed reports">
          <Button variant="secondary" size="xs" type="button" onClick={() => setHistoryOpen((o) => !o)} aria-expanded={historyOpen}>
            Dismissed · {dismissedCount}
            <svg className={`h-3 w-3 transition-transform ${historyOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden>
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </Button>
          {historyOpen && (
            <div className="mt-3 space-y-2">
              {history.isLoading ? (
                <p className="text-sm text-tertiary">Loading…</p>
              ) : history.error ? (
                <p className="text-sm text-status-danger-fg">{(history.error as Error).message}</p>
              ) : !dismissed.length ? (
                <p className="text-sm text-tertiary">Nothing is dismissed.</p>
              ) : (
                <ul className="space-y-2" aria-label="Dismissed reports">
                  {dismissed.map((r) => (
                    <ReportRow key={r.id} r={r} agentName={agentName[r.agent] || r.agent}
                      action="Restore" busy={busy(r)} onAction={() => act(r, false)} />
                  ))}
                </ul>
              )}
              {history.hasNextPage && (
                <ShowMore loading={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()} />
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

function ReportRow({ r, agentName, action, busy, onAction }: {
  r: ReplyReport; agentName: string; action: 'Dismiss' | 'Restore'; busy: boolean; onAction: () => void
}) {
  const [open, setOpen] = useState(false)
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
        {r.dismissed_at && ` · dismissed by ${r.dismissed_by_name || 'an admin'} ${formatRelativeTime(r.dismissed_at)}`}
      </p>
      {r.note && <p className="mt-2 whitespace-pre-wrap break-words text-sm text-secondary">“{r.note}”</p>}
      <div className="mt-2 flex flex-wrap items-center gap-3 text-xs">
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
          className="font-medium text-link hover:underline">
          {open ? 'Hide the reply' : 'Show the reply'}
        </button>
        <Link to={chatPath(r)} className="font-medium text-link hover:underline">Open chat</Link>
        <button type="button" onClick={onAction} disabled={busy}
          className="ml-auto rounded-control px-2 py-0.5 font-medium text-secondary transition-colors hover:bg-surface-hover disabled:opacity-50">
          {action}
        </button>
      </div>
      {open && (
        <p className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded-card bg-surface-sunken px-3 py-2 text-sm text-primary">
          {r.reply_text}
        </p>
      )}
    </li>
  )
}
