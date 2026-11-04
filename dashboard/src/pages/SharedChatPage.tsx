import { Fragment } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { BUTTON_BASE, BUTTON_SIZE, BUTTON_VARIANT } from '../components/ui/classes'
import { useChatSnapshot, type SnapshotMessage } from '../api/shares'
import MarkdownContent from '../components/chat/MarkdownContent'
import { ReportSharedReplyButton } from '../components/chat/ReportReplyButton'
import { formatRelativeTime } from '../lib/format'
import { WIRE } from '../api/wireEvents'
import { attachmentsLine, cardPlaceholder, listPlaceholder, settingsPlaceholder } from '../sharehost/host'

/**
 * /shared/:shareId — a chat someone shared with the signed-in user, as it
 * stood when it was shared (SHARING.md "Chat shares"). Read-only: text as
 * markdown, artifacts in the same scripts-only sandbox as any artifact,
 * images, clips and files from the snapshot's own copies. Nothing here
 * talks to the live chat, so the page stays the same after the chat moves
 * on or is deleted. Under each run of the agent's blocks a flag reports the
 * reply through the share (a copy made before 1.9.1 has no row ids and no
 * flag).
 */

/** A person's message in the copy (it carries no row). */
function personsMessage(m: SnapshotMessage | undefined): boolean {
  return m?.role === 'user'
}

/** The flag's place: the last block of each reply (the agent's blocks
 *  between two of the person's messages; a thinking block keeps no row and
 *  goes on with the reply), keyed to the reply's first row, the one the
 *  report names. A reply with no row (a copy made before 1.9.1) has none. */
function reportRuns(messages: SnapshotMessage[]): Map<number, number> {
  const out = new Map<number, number>()
  let first: number | null = null
  messages.forEach((m, i) => {
    if (personsMessage(m)) return
    if (first === null && typeof m.id === 'number') first = m.id
    const next = messages[i + 1]
    if (!next || personsMessage(next)) {
      if (first !== null) out.set(i, first)
      first = null
    }
  })
  return out
}
export default function SharedChatPage() {
  const { shareId } = useParams<{ shareId: string }>()
  const navigate = useNavigate()
  const { data, isLoading, error } = useChatSnapshot(shareId)

  if (isLoading) {
    return (
      <div className="flex h-screen-safe items-center justify-center bg-surface-page text-sm text-tertiary">Loading…</div>
    )
  }
  if (!data || error) {
    return (
      <div className="flex h-screen-safe flex-col items-center justify-center gap-3 bg-surface-page px-6 text-center">
        <p className="text-sm font-medium text-secondary">This shared chat is not available.</p>
        <p className="max-w-sm text-xs text-tertiary">The share may have been withdrawn, or it is not shared with you.</p>
        <button onClick={() => navigate('/')} className={`${BUTTON_BASE} ${BUTTON_VARIANT.secondary} ${BUTTON_SIZE.xs}`}>
          Back to OtoDock
        </button>
      </div>
    )
  }

  const base = `/v1/shares/${data.id}`
  const runs = reportRuns(data.messages)
  return (
    <div className="flex h-screen-safe flex-col overflow-hidden bg-surface-page">
      <header className="flex shrink-0 items-center gap-2 border-b border-subtle bg-surface-card px-3 py-2" style={{ paddingTop: 'max(0.5rem, env(safe-area-inset-top))' }}>
        <button onClick={() => navigate('/')} aria-label="Back" title="Back"
          className="flex h-7 w-7 items-center justify-center rounded-full text-secondary hover:bg-surface-hover hover:text-primary">
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" /></svg>
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-medium text-primary">{data.title || 'Shared conversation'}</h1>
          <p className="truncate text-2xs text-tertiary">
            Shared by {data.shared_by_name || 'a colleague'} · a copy from {formatRelativeTime(data.created_at)} · read-only
          </p>
        </div>
      </header>
      <main className="flex-1 overflow-y-auto px-3 py-4">
        <div className="mx-auto flex max-w-3xl flex-col gap-3">
          {data.messages.map((m, i) => {
            const reportOf = runs.get(i)
            return (
              <Fragment key={i}>
                <SnapshotBlock m={m} base={base} />
                {reportOf !== undefined && (
                  <div className="-mt-2 flex justify-start text-2xs">
                    <ReportSharedReplyButton shareId={data.id} messageId={reportOf} />
                  </div>
                )}
              </Fragment>
            )
          })}
          {!data.messages.length && <p className="text-center text-xs text-tertiary">Nothing to show.</p>}
        </div>
      </main>
    </div>
  )
}

export function SnapshotBlock({ m, base }: { m: SnapshotMessage; base: string }) {
  const mine = personsMessage(m)
  if (mine || m.role === 'assistant') {
    return (
      <div className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
        <div className={`max-w-[85%] rounded-card px-3 py-2 text-sm ${mine ? 'bg-action text-action-fg' : 'bg-surface-card text-primary shadow-elevation-1'}`}>
          {mine && attachmentsLine(m.attachments) && (
            <p className="text-xs opacity-80" data-testid="snapshot-attachments">{attachmentsLine(m.attachments)}</p>
          )}
          {mine ? (m.content ? <p className="whitespace-pre-wrap break-words">{m.content}</p> : null) : <MarkdownContent>{m.content || ''}</MarkdownContent>}
        </div>
      </div>
    )
  }
  const d: Record<string, unknown> = m.data ?? {}
  // A row's field as text ('' when absent): the snapshot's data is open JSON.
  const text = (key: string): string => (d[key] == null ? '' : String(d[key]))
  switch (m.event_type) {
    case WIRE.UI:
      return (
        <div className="overflow-hidden rounded-card bg-surface-card shadow-elevation-1">
          {text('title') && <p className="border-b border-subtle px-3 py-1.5 text-xs font-medium text-primary">{text('title')}</p>}
          <iframe title={text('title') || 'Artifact'} sandbox="allow-scripts" referrerPolicy="no-referrer"
            src={`${base}/ui/${encodeURIComponent(String(d.token || ''))}`}
            style={{ height: Math.min(Math.max(Number(d.height) || 320, 120), 900) }}
            className="w-full border-0 bg-transparent" />
        </div>
      )
    case WIRE.IMAGES:
      return (
        <div className="flex flex-wrap gap-2">
          {(d.images as Array<Record<string, string>> | undefined)?.map((img, i) => (
            <figure key={i} className="max-w-xs overflow-hidden rounded-card bg-surface-card shadow-elevation-1">
              <img src={img.token ? `${base}/media/${encodeURIComponent(img.token)}` : img.url} alt={img.caption || ''} className="block max-h-72 w-full object-contain" />
              {img.caption && <figcaption className="px-2 py-1 text-2xs text-tertiary">{img.caption}</figcaption>}
            </figure>
          ))}
        </div>
      )
    case WIRE.VIDEO:
    case WIRE.AUDIO: {
      const src = d.token ? `${base}/media/${encodeURIComponent(String(d.token))}` : String(d.url || '')
      return (
        <div className="overflow-hidden rounded-card bg-surface-card shadow-elevation-1 p-2">
          {m.event_type === WIRE.VIDEO
            ? <video controls src={src} className="max-h-96 w-full rounded-card" />
            : <audio controls src={src} className="w-full" />}
          {(text('caption') || text('title')) && <p className="mt-1 text-2xs text-tertiary">{text('caption') || text('title')}</p>}
        </div>
      )
    }
    case WIRE.FILE:
      return (
        <a href={`${base}/media/${encodeURIComponent(String(d.token || ''))}`} download
          className="inline-flex items-center gap-2 rounded-card bg-surface-card shadow-elevation-1 px-3 py-2 text-sm text-primary hover:bg-surface-hover">
          <span className="font-medium">{text('filename') || 'file'}</span>
          {text('description') && <span className="text-xs text-tertiary">{text('description')}</span>}
        </a>
      )
    case WIRE.SETTINGS_CARD:
      return (
        <p data-testid="snapshot-setting" className="rounded-card border border-dashed border-default px-3 py-2 text-sm text-secondary">
          {settingsPlaceholder(d.label)}
        </p>
      )
    case WIRE.ACTION_CARD:
      return (
        <p data-testid="snapshot-card" className="rounded-card border border-dashed border-default px-3 py-2 text-sm text-secondary">
          {cardPlaceholder(d.title)}
        </p>
      )
    case WIRE.LIST_CARD:
      return (
        <p data-testid="snapshot-list" className="rounded-card border border-dashed border-default px-3 py-2 text-sm text-secondary">
          {listPlaceholder(d.title)}
        </p>
      )
    case WIRE.URL:
      return (
        <a href={String(d.url || '')} target="_blank" rel="noreferrer noopener"
          className="block rounded-card bg-surface-card shadow-elevation-1 px-3 py-2 text-sm text-link hover:bg-surface-hover">
          {text('title') || text('url')}
          {text('description') && <span className="block text-xs text-tertiary">{text('description')}</span>}
        </a>
      )
    default:
      return (
        <details className="rounded-card border border-dashed border-default px-3 py-1.5 text-2xs text-tertiary">
          <summary className="cursor-pointer">{m.event_type}</summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-3xs">{m.event_data || ''}</pre>
        </details>
      )
  }
}
