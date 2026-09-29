import { useNavigate, useParams } from 'react-router-dom'
import { useChatSnapshot, type SnapshotMessage } from '../api/shares'
import MarkdownContent from '../components/chat/MarkdownContent'
import { formatRelativeTime } from '../lib/format'
import { WIRE } from '../api/wireEvents'

/**
 * /shared/:shareId — a chat someone shared with the signed-in user, as it
 * stood when it was shared (SHARING.md "Chat shares"). Read-only: text as
 * markdown, artifacts in the same scripts-only sandbox as any artifact,
 * images, clips and files from the snapshot's own copies. Nothing here
 * talks to the live chat, so the page stays the same after the chat moves
 * on or is deleted.
 */
export default function SharedChatPage() {
  const { shareId } = useParams<{ shareId: string }>()
  const navigate = useNavigate()
  const { data, isLoading, error } = useChatSnapshot(shareId)

  if (isLoading) {
    return (
      <div className="flex h-screen-safe items-center justify-center bg-p-bg text-sm text-p-text-light">Loading…</div>
    )
  }
  if (!data || error) {
    return (
      <div className="flex h-screen-safe flex-col items-center justify-center gap-3 bg-p-bg px-6 text-center">
        <p className="text-sm font-medium text-p-text-secondary">This shared chat is not available.</p>
        <p className="max-w-sm text-xs text-p-text-light">The share may have been withdrawn, or it is not shared with you.</p>
        <button onClick={() => navigate('/')} className="rounded-md border border-p-border-light px-3 py-1.5 text-xs font-medium text-p-text-secondary hover:bg-p-surface-hover">
          Back to OtoDock
        </button>
      </div>
    )
  }

  const base = `/v1/shares/${data.id}`
  return (
    <div className="flex h-screen-safe flex-col overflow-hidden bg-p-bg">
      <header className="flex shrink-0 items-center gap-2 border-b border-p-border-light/60 px-3 py-2" style={{ paddingTop: 'max(0.5rem, env(safe-area-inset-top))' }}>
        <button onClick={() => navigate('/')} aria-label="Back" title="Back"
          className="flex h-7 w-7 items-center justify-center rounded-full text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text">
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" /></svg>
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-medium text-p-text">{data.title || 'Shared conversation'}</h1>
          <p className="truncate text-[11px] text-p-text-light">
            Shared by {data.shared_by_name || 'a colleague'} · a copy from {formatRelativeTime(data.created_at)} · read-only
          </p>
        </div>
      </header>
      <main className="flex-1 overflow-y-auto px-3 py-4">
        <div className="mx-auto flex max-w-3xl flex-col gap-3">
          {data.messages.map((m, i) => <SnapshotBlock key={i} m={m} base={base} />)}
          {!data.messages.length && <p className="text-center text-xs text-p-text-light">Nothing to show.</p>}
        </div>
      </main>
    </div>
  )
}

export function SnapshotBlock({ m, base }: { m: SnapshotMessage; base: string }) {
  if (m.role === 'user' || m.role === 'assistant') {
    const mine = m.role === 'user'
    return (
      <div className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
        <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm ${mine ? 'bg-brand text-white' : 'bg-p-surface text-p-text border border-p-border-light/60'}`}>
          {mine ? <p className="whitespace-pre-wrap break-words">{m.content}</p> : <MarkdownContent>{m.content || ''}</MarkdownContent>}
        </div>
      </div>
    )
  }
  const d = (m.data ?? {}) as Record<string, any>
  switch (m.event_type) {
    case WIRE.UI:
      return (
        <div className="overflow-hidden rounded-xl border border-p-border-light/60 bg-p-surface">
          {d.title && <p className="border-b border-p-border-light/60 px-3 py-1.5 text-xs font-medium text-p-text">{d.title}</p>}
          <iframe title={d.title || 'Artifact'} sandbox="allow-scripts" referrerPolicy="no-referrer"
            src={`${base}/ui/${encodeURIComponent(String(d.token || ''))}`}
            style={{ height: Math.min(Math.max(Number(d.height) || 320, 120), 900) }}
            className="w-full border-0 bg-transparent" />
        </div>
      )
    case WIRE.IMAGES:
      return (
        <div className="flex flex-wrap gap-2">
          {(d.images as Array<Record<string, string>> | undefined)?.map((img, i) => (
            <figure key={i} className="max-w-xs overflow-hidden rounded-xl border border-p-border-light/60">
              <img src={img.token ? `${base}/media/${encodeURIComponent(img.token)}` : img.url} alt={img.caption || ''} className="block max-h-72 w-full object-contain" />
              {img.caption && <figcaption className="px-2 py-1 text-[11px] text-p-text-light">{img.caption}</figcaption>}
            </figure>
          ))}
        </div>
      )
    case WIRE.VIDEO:
    case WIRE.AUDIO: {
      const src = d.token ? `${base}/media/${encodeURIComponent(String(d.token))}` : String(d.url || '')
      return (
        <div className="overflow-hidden rounded-xl border border-p-border-light/60 bg-p-surface p-2">
          {m.event_type === WIRE.VIDEO
            ? <video controls src={src} className="max-h-96 w-full rounded-lg" />
            : <audio controls src={src} className="w-full" />}
          {(d.caption || d.title) && <p className="mt-1 text-[11px] text-p-text-light">{d.caption || d.title}</p>}
        </div>
      )
    }
    case WIRE.FILE:
      return (
        <a href={`${base}/media/${encodeURIComponent(String(d.token || ''))}`} download
          className="inline-flex items-center gap-2 rounded-xl border border-p-border-light/60 bg-p-surface px-3 py-2 text-sm text-p-text hover:bg-p-surface-hover">
          <span className="font-medium">{d.filename || 'file'}</span>
          {d.description && <span className="text-xs text-p-text-light">{d.description}</span>}
        </a>
      )
    case WIRE.URL:
      return (
        <a href={String(d.url || '')} target="_blank" rel="noreferrer noopener"
          className="block rounded-xl border border-p-border-light/60 bg-p-surface px-3 py-2 text-sm text-brand hover:bg-p-surface-hover">
          {d.title || d.url}
          {d.description && <span className="block text-xs text-p-text-light">{d.description}</span>}
        </a>
      )
    default:
      return (
        <details className="rounded-lg border border-dashed border-p-border-light/60 px-3 py-1.5 text-[11px] text-p-text-light">
          <summary className="cursor-pointer">{m.event_type}</summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[10px]">{m.event_data || ''}</pre>
        </details>
      )
  }
}
