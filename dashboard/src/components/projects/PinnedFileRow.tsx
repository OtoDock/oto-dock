import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/auth'
import MarkdownContent from '../chat/MarkdownContent'
import { onFileUpdate } from '../../lib/fileUpdates'
import type { PinnedFileRef } from '../../api/apps'

// A Dock file pin — the board-file interaction lifted to any pinned file:
// collapsed <details> row, expand → rich markdown (.md) or plain code text,
// READ-ONLY, live while open (the board's 10s poll + an instant refetch on
// the file's own file_updated broadcast). Content is fetched through the
// files API only when expanded, so a Dock full of pins costs nothing until
// the user opens one — and the viewer's own path-policy role decides what
// renders (a 403 shows as the access message, never the content).

/** Render cap — the files API has no text size limit; a huge log pin must
 * not freeze the Dock. First N chars + an honest truncation note. */
const RENDER_CAP = 200_000

export default function PinnedFileRow({ pin }: { pin: PinnedFileRef }) {
  const [open, setOpen] = useState(false)
  const queryClient = useQueryClient()
  const { data, error, isLoading } = useQuery({
    queryKey: ['pinned-file', pin.agent, pin.rel_path],
    queryFn: async (): Promise<string> => {
      const path = pin.rel_path.split('/').map(encodeURIComponent).join('/')
      const res = await apiFetch(`/v1/agents/${encodeURIComponent(pin.agent)}/files/${path}`)
      if (!res.ok) {
        throw new Error(
          res.status === 403
            ? 'You do not have access to this file.'
            : res.status === 404
              ? 'File not found — moved or deleted since it was pinned.'
              : 'Failed to load the file.',
        )
      }
      const d = await res.json()
      return typeof d?.content === 'string' ? d.content : ''
    },
    enabled: open,
    refetchInterval: open ? 10_000 : false,
  })

  // Instant refresh between polls: agent/Collabora writes broadcast
  // file_updated for the path — refetch the open row right away.
  useEffect(() => onFileUpdate((u) => {
    if (u.agent_slug === pin.agent && u.rel_path === pin.rel_path) {
      queryClient.invalidateQueries({ queryKey: ['pinned-file', pin.agent, pin.rel_path] })
    }
  }), [pin.agent, pin.rel_path, queryClient])

  const isMd = pin.rel_path.toLowerCase().endsWith('.md')
  const truncated = (data?.length ?? 0) > RENDER_CAP
  const body = truncated ? (data as string).slice(0, RENDER_CAP) : (data ?? '')

  return (
    <details
      className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface px-3 py-2"
      data-testid="dock-file-pin"
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer select-none flex items-center gap-2 min-w-0">
        <svg className="w-3.5 h-3.5 text-p-text-light shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
        </svg>
        <span className="text-xs font-medium text-p-text truncate">{pin.title || pin.rel_path.split('/').pop()}</span>
        <span className="ml-auto text-[10px] text-p-text-light truncate max-w-[45%]" title={pin.rel_path}>
          {pin.rel_path.replace(/^(users\/[^/]+\/)?workspace\//, '')}
        </span>
      </summary>
      <div className="mt-2 border-t border-p-border-light/60 pt-2">
        {error ? (
          <p className="text-xs text-p-text-light">{(error as Error).message}</p>
        ) : isLoading ? (
          <p className="text-xs text-p-text-light">Loading…</p>
        ) : isMd ? (
          <div className="text-sm">
            <MarkdownContent>{body}</MarkdownContent>
          </div>
        ) : (
          <pre className="text-xs text-p-text-secondary whitespace-pre-wrap break-words overflow-x-auto max-h-[60vh] overflow-y-auto">{body}</pre>
        )}
        {truncated && (
          <p className="mt-2 text-[10px] text-p-text-light">
            Truncated preview — first {RENDER_CAP.toLocaleString()} characters.
          </p>
        )}
      </div>
    </details>
  )
}
