// Report flag: the reply footer's action that reports an agent's reply to
// the admins of this OtoDock (the stores ask for it: Google Play's AI
// content policy, Apple's guideline 1.2). Sits beside copy and the sound
// icon under a complete agent reply; filled once this person reported it
// (for this view of the chat).

import { useEffect, useId, useState } from 'react'

import {
  REPLY_REPORT_NOTE_MAX,
  REPLY_REPORT_REASONS,
  fetchRowsPage,
  reportReply,
  type ReplyReportReason,
} from '../../api/replyReports'
import type { DbMessageRow } from '../../api/wireEvents'
import { errorText } from '../../lib/http/apiJson'
import { findStoredReply, storedRowId } from '../../lib/messageBlocks'
import Button from '../ui/Button'
import Dialog from '../ui/Dialog'
import type { DisplayMessage } from './types'

const FLAG_PATH = 'M4 21V4m0 0h11l-1.5 4L15 12H4'
const NOT_SENT = 'The report was not sent. Try again.'
const NOT_FOUND = 'Reload the chat, then report this reply.'
// A streamed reply is looked up among the chat's newest stored pages; it may
// still be saving, so the lookup tries again before it gives up.
const PAGES = 5
const ATTEMPTS = 3
const RETRY_MS = 1500

/** The first stored row of `messages[index]`: its own id for a bubble the
 *  history built, else found among the chat's newest rows. Null when it is
 *  not there, or once the dialog closed (`alive` says so). */
async function storedReply(
  chatId: string, messages: DisplayMessage[], index: number, alive: () => boolean,
): Promise<number | null> {
  const own = storedRowId(messages[index].id)
  if (own !== null) return own
  for (let attempt = 0; attempt < ATTEMPTS && alive(); attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, RETRY_MS))
    let rows: DbMessageRow[] = []
    let before: number | undefined
    for (let page = 0; page < PAGES && alive(); page++) {
      const { messages: got, has_more: more } = await fetchRowsPage(chatId, before)
      rows = [...got, ...rows]
      const found = findStoredReply(messages, index, rows)
      if (found !== null) return found
      if (!more || !got.length) break
      before = got[0].id
    }
  }
  return null
}

function ReportReplyDialog({ chatId, messages, index, onClose, onReported }: {
  chatId: string
  messages: DisplayMessage[]
  index: number
  onClose: () => void
  onReported: () => void
}) {
  const id = useId()
  // The list as it stood when the dialog opened: the stream goes on beside it.
  const [opened] = useState(messages)
  const [target, setTarget] = useState<number | null | undefined>(() => storedRowId(messages[index].id) ?? undefined)
  const [reason, setReason] = useState<ReplyReportReason | null>(null)
  const [note, setNote] = useState('')
  const [lookupError, setLookupError] = useState('')
  const [sending, setSending] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')

  // A streamed reply's stored row, looked up once as the dialog opens.
  useEffect(() => {
    if (target !== undefined) return
    let alive = true
    storedReply(chatId, opened, index, () => alive)
      .then((found) => { if (alive) setTarget(found) })
      .catch((e) => {
        if (!alive) return
        setLookupError(errorText(e) || NOT_SENT)
        setTarget(null)
      })
    return () => { alive = false }
  }, [chatId, opened, index, target])

  const send = async () => {
    if (!reason || typeof target !== 'number') return
    setSending(true)
    setError('')
    try {
      await reportReply(chatId, { message_id: target, reason, note: note.trim() })
      setDone(true)
      onReported()
    } catch (e) {
      setError(errorText(e) || NOT_SENT)
    } finally {
      setSending(false)
    }
  }

  const finding = target === undefined
  const missing = target === null
  return (
    <Dialog onClose={onClose} title="Report this reply" placement="sheet" busy={sending}
      dismissible={done ? 'any' : 'escape'} aria-describedby={done ? undefined : `${id}-what`}
      data-testid="report-reply-dialog">
      {done ? (
        <div className="space-y-4 px-4 py-4">
          <p role="status" className="text-sm text-primary">Thank you. The admins of this OtoDock have your report.</p>
          <div className="flex justify-end">
            <Button variant="primary" onClick={onClose} autoFocus>Done</Button>
          </div>
        </div>
      ) : (
        <div className="space-y-4 px-4 py-4">
          <p id={`${id}-what`} className="text-sm text-secondary">
            The admins of this OtoDock will receive this report with your name.
          </p>
          <fieldset className="space-y-1.5" disabled={sending}>
            <legend className="mb-1.5 text-sm font-medium text-primary">Why are you reporting it?</legend>
            {REPLY_REPORT_REASONS.map(r => (
              <label key={r.value} className="flex cursor-pointer items-center gap-2 rounded-control px-2 py-1.5 text-sm text-primary hover:bg-surface-hover">
                <input type="radio" name={`${id}-reason`} value={r.value} checked={reason === r.value}
                  onChange={() => setReason(r.value)} className="accent-action" />
                {r.label}
              </label>
            ))}
          </fieldset>
          <div>
            <label htmlFor={`${id}-note`} className="mb-1 block text-sm font-medium text-primary">Note, optional</label>
            <textarea id={`${id}-note`} value={note} onChange={e => setNote(e.target.value)}
              maxLength={REPLY_REPORT_NOTE_MAX} rows={3} disabled={sending}
              placeholder="What is wrong with this reply?"
              className="w-full resize-y rounded-control border border-subtle bg-surface-card px-3 py-2 text-sm text-primary placeholder:text-tertiary focus:border-action focus:outline-none" />
            {note.length > REPLY_REPORT_NOTE_MAX - 100 && (
              <p className="mt-1 text-right text-xs text-tertiary">{note.length} of {REPLY_REPORT_NOTE_MAX}</p>
            )}
          </div>
          {finding && <p role="status" className="text-sm text-tertiary">Finding the reply…</p>}
          {(error || missing) && (
            <p role="alert" className="text-sm text-status-danger-fg">{error || lookupError || NOT_FOUND}</p>
          )}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose} disabled={sending}>Cancel</Button>
            <Button variant="primary" onClick={() => void send()} disabled={!reason || sending || finding || missing}>
              {sending ? 'Sending…' : 'Send report'}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  )
}

export function ReportReplyButton({ chatId, messages, index }: {
  chatId: string
  messages: DisplayMessage[]
  index: number
}) {
  const [open, setOpen] = useState(false)
  const [reported, setReported] = useState(false)
  const label = reported ? 'Reported' : 'Report this reply'
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={label}
        aria-label={label}
        className={`p-1 rounded-sm transition-colors ${reported ? 'text-link' : 'text-tertiary hover:text-link'}`}
      >
        <svg className="w-3.5 h-3.5" fill={reported ? 'currentColor' : 'none'} stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={FLAG_PATH} />
        </svg>
      </button>
      {open && (
        <ReportReplyDialog chatId={chatId} messages={messages} index={index}
          onClose={() => setOpen(false)} onReported={() => setReported(true)} />
      )}
    </>
  )
}
