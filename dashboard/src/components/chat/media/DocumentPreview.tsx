import { useState, useCallback, useEffect, useRef } from 'react'
import { safeHref } from '../../../lib/safeUrl'
import FilePreviewPortal from '../../workspace/FilePreviewPortal'
import { useCollaboraLiveReload } from '../../../hooks/useCollaboraLiveReload'
import { scrollToLivePreview } from '../../../lib/previewEngagement'
import type { PreviewChainMode } from '../../../lib/messageBlocks'

interface Props {
  wopiUrl: string
  filename: string
  fileId: string
  downloadUrl: string
  dbMessageId?: number
  /** Push-time snapshot backing this block's "previous version" render. */
  snapshotId?: string
  chatId?: string
  /** Scoped dismissal: 'instance' removes only this block (a frozen
   * "previous version" closing itself); 'file' removes the file's whole
   * preview trail (the live block's close). */
  onDismiss?: (scope: 'file' | 'instance') => void
  /** Render inside an interactive-CLI PiP window: the
   * floating window supplies the title + minimize + close chrome, so this hides
   * its own filename + close button (no duplicate), keeps the refresh/fullscreen/
   * download actions, and fills the window height instead of a fixed 60vh. */
  embedded?: boolean
  /** Render-time chain state (previewChainModes): the file's newest preview
   * is 'live', the one before it a view-only 'frozen' previous version,
   * anything older a 'chip'. Hosts without a chain (PiP) omit it → live. */
  mode?: PreviewChainMode
}

/** Viewport ratio above which a preview counts as "being read" — its
 * downgrade (live→frozen / frozen→chip) is deferred until the user scrolls
 * away or closes fullscreen. */
const ENGAGED_RATIO = 0.3

/** How long a just-sent Action_Save gets before the block swaps away from its
 * live iframe. The save keeps running server-side in Collabora once the
 * message lands — this only needs to cover message delivery. */
const SAVE_GRACE_MS = 1200

const MODE_RANK: Record<PreviewChainMode, number> = { live: 0, frozen: 1, chip: 2 }

function getFileExtBadge(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const labels: Record<string, string> = {
    pdf: 'PDF', docx: 'DOCX', doc: 'DOC', xlsx: 'XLSX', xls: 'XLS',
    pptx: 'PPTX', ppt: 'PPT', odt: 'ODT', ods: 'ODS', odp: 'ODP',
    csv: 'CSV', txt: 'TXT', html: 'HTML', rtf: 'RTF',
  }
  return labels[ext] || ext.toUpperCase()
}

export default function DocumentPreview({ wopiUrl, filename, fileId, downloadUrl, dbMessageId, snapshotId, chatId, onDismiss, embedded, mode }: Props) {
  const [fullscreen, setFullscreen] = useState(false)
  const [iframeLoaded, setIframeLoaded] = useState(false)
  const [refreshTs, setRefreshTs] = useState<number | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  // Chain target vs rendered state: the chain says what this block SHOULD be;
  // the block applies downgrades itself so it can defer them while the user
  // is engaged (per instance — a new live block's visibility never postpones
  // this block's transition) and flush unsaved edits first. Upgrades back to
  // 'live' are refused (ratchet): a transient rebuild gap must never flash
  // the CURRENT document into a block the user knows as a previous version —
  // only chip→frozen (a dismissed sibling) re-expands.
  const targetMode: PreviewChainMode = embedded ? 'live' : (mode ?? 'live')
  const [renderedMode, setRenderedMode] = useState<PreviewChainMode>(targetMode)

  // Engagement (per instance, local): substantially visible or fullscreen.
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    if (embedded || renderedMode === 'chip') return
    const el = containerRef.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    const io = new IntersectionObserver(
      (entries) => {
        const entry = entries[entries.length - 1]
        setVisible(entry.isIntersecting && entry.intersectionRatio >= ENGAGED_RATIO)
      },
      { threshold: [0, ENGAGED_RATIO] },
    )
    io.observe(el)
    return () => {
      io.disconnect()
      setVisible(false)
    }
  }, [embedded, renderedMode])
  const engaged = visible || fullscreen

  // Replace _t= timestamp to force iframe reload on refresh
  const effectiveUrl = refreshTs
    ? wopiUrl.replace(/&_t=\d+/, `&_t=${refreshTs}`)
    : wopiUrl

  const handleRefresh = useCallback(() => {
    setIframeLoaded(false)
    setRefreshTs(Date.now())
  }, [])

  // Live-reload when another user changes this same file. Live instances
  // only — a frozen block must never flash-reload on the agent's next write
  // (its snapshot is immutable), and a chip has no iframe.
  // Dirty-guarded: an agent/disk write auto-reloads only when there are no
  // unsaved edits; otherwise it surfaces a manual "Reload" affordance.
  const { iframeRef, reloadAvailable, doReload, modifiedRef } = useCollaboraLiveReload({
    fileId: embedded || renderedMode === 'live' ? fileId : undefined,
    reload: handleRefresh,
  })

  // Apply chain transitions: upgrades (chip→frozen only) immediately;
  // downgrades wait for disengagement, and leaving a live render with unsaved
  // edits sends Action_Save first — Collabora's own save-on-disconnect races
  // teardown and can drop the user's last keystrokes.
  useEffect(() => {
    if (embedded || targetMode === renderedMode) return
    if (MODE_RANK[targetMode] < MODE_RANK[renderedMode]) {
      if (targetMode === 'frozen' && renderedMode === 'chip') setRenderedMode('frozen')
      return
    }
    if (engaged) return // re-runs on disengage
    if (renderedMode === 'live' && modifiedRef.current) {
      try {
        iframeRef.current?.contentWindow?.postMessage(
          JSON.stringify({
            MessageId: 'Action_Save',
            Values: { DontTerminateEdit: true, DontSaveIfUnmodified: true, Notify: false },
          }),
          '*',
        )
      } catch { /* iframe gone — nothing to flush */ }
      const t = setTimeout(() => setRenderedMode(targetMode), SAVE_GRACE_MS)
      return () => clearTimeout(t)
    }
    setRenderedMode(targetMode)
  }, [embedded, targetMode, renderedMode, engaged, iframeRef, modifiedRef])

  // Frozen render: mint a view-only URL for THIS block's own push-time
  // snapshot at swap time (tokens are never persisted). Any failure —
  // no snapshot recorded, pruned, chat gone — degrades the block to a chip,
  // never a broken iframe.
  const [frozenUrl, setFrozenUrl] = useState<string | null>(null)
  const [frozenFailed, setFrozenFailed] = useState(false)
  useEffect(() => {
    if (renderedMode !== 'frozen' || embedded) return
    if (!chatId || !snapshotId) {
      setFrozenFailed(true)
      return
    }
    let alive = true
    setIframeLoaded(false)
    fetch(
      `/v1/documents/snapshot-wopi-url?chat_id=${encodeURIComponent(chatId)}&snapshot_id=${encodeURIComponent(snapshotId)}`,
      { credentials: 'include' },
    )
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => { if (alive) setFrozenUrl(j.wopi_url) })
      .catch(() => { if (alive) setFrozenFailed(true) })
    return () => { alive = false }
  }, [renderedMode, embedded, chatId, snapshotId])

  const handleDismiss = useCallback(async () => {
    // A frozen block dismisses only itself (its snapshot/row); the live block
    // closes the file's whole preview trail — chips and frozen versions point
    // at it and are meaningless without it. A frozen block with no scoping
    // key (nothing persisted to scope by) falls back to the full dismissal.
    const scoped = renderedMode === 'frozen' && (snapshotId || dbMessageId != null)
    if (chatId && fileId) {
      const params = !scoped ? ''
        : snapshotId ? `?snapshot_id=${encodeURIComponent(snapshotId)}`
        : `?message_id=${dbMessageId}`
      try {
        await fetch(`/v1/chats/${chatId}/dismiss-preview/${encodeURIComponent(fileId)}${params}`, {
          method: 'PATCH',
          credentials: 'include',
        })
      } catch { /* Best-effort */ }
    }
    onDismiss?.(scoped ? 'instance' : 'file')
  }, [renderedMode, chatId, fileId, snapshotId, dbMessageId, onDismiss])

  const extBadge = getFileExtBadge(filename)
  const dlUrl = `${downloadUrl}${downloadUrl.includes('?') ? '&' : '?'}fn=${encodeURIComponent(filename)}`
  const frozen = !embedded && renderedMode === 'frozen' && !frozenFailed

  // A preview URL whose origin differs from the page can never load — the
  // browser (and Collabora's frame-ancestors check) refuses the frame. The
  // classic first-run miss: DASHBOARD_PUBLIC_URL left at its localhost
  // default while browsing the server from another machine. Detectable right
  // here, so explain the fix instead of rendering a dead frame.
  const previewOrigin = (() => {
    try { return new URL(wopiUrl, window.location.href).origin } catch { return '' }
  })()
  const originMismatch = !!previewOrigin && previewOrigin !== window.location.origin

  if (!embedded && (renderedMode === 'chip' || (renderedMode === 'frozen' && frozenFailed))) {
    // Superseded preview — a chip explains where the live one went instead of
    // the block silently vanishing under the reader. Also the degraded form
    // of a previous version whose snapshot is gone.
    return (
      <button
        onClick={() => scrollToLivePreview(fileId)}
        className="my-1 flex items-center gap-2 px-3 py-1.5 rounded-full border border-p-border-light bg-p-surface/50 hover:bg-p-surface transition-colors text-xs text-p-text-secondary"
        title="This document has a newer preview further down"
      >
        <svg className="w-3.5 h-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
        </svg>
        <span className="truncate max-w-[16rem]">{filename}</span>
        <span className="shrink-0">— preview moved to latest turn ↓</span>
      </button>
    )
  }

  const mismatchNotice = (
    <div className="h-full flex items-center justify-center p-6">
      <div className="max-w-md text-sm text-p-text-secondary space-y-2">
        <p className="font-medium text-p-text">Document preview can't load from this address.</p>
        <p>
          The preview is configured for <code className="text-xs">{previewOrigin}</code>, but
          you're browsing from <code className="text-xs">{window.location.origin}</code>.
        </p>
        <p>
          An administrator should set <code className="text-xs">DASHBOARD_PUBLIC_URL={window.location.origin}</code> in
          the install's <code className="text-xs">.env</code> (or <code className="text-xs">config.env</code>) and
          run <code className="text-xs">docker compose up -d</code> to apply it.
        </p>
      </div>
    </div>
  )

  const frameSrc = frozen ? frozenUrl : effectiveUrl

  return (
    <>
      <div
        ref={containerRef}
        data-preview-anchor={!embedded && targetMode === 'live' ? fileId : undefined}
        className={embedded
          ? 'flex h-full flex-col overflow-hidden bg-white dark:bg-p-surface'
          : 'my-2 rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden w-full'}
      >
        {/* Header (in embedded/PiP mode the window supplies the title + close, so
            only the action buttons show). */}
        <div className="flex items-center justify-between px-3 py-2 bg-p-surface/50 border-b border-p-border-light">
          {!embedded && (
            <div className="flex items-center gap-2 min-w-0">
              <svg className="w-4 h-4 text-p-text-secondary shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
              </svg>
              <span className="text-sm font-medium text-p-text truncate">{filename}</span>
              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-sm bg-brand/10 text-brand shrink-0">
                {extBadge}
              </span>
              {frozen && (
                <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-sm bg-p-text-light/15 text-p-text-secondary shrink-0">
                  Previous version
                </span>
              )}
            </div>
          )}
          <div className="flex items-center gap-1 shrink-0 ml-2">
            {frozen ? (
              /* Previous-version indicator — jumps to the live preview */
              <button
                onClick={() => scrollToLivePreview(fileId)}
                title="Go to the latest preview"
                className="flex items-center gap-1 px-2 py-1 rounded-sm hover:bg-p-surface transition-colors text-xs text-p-text-secondary"
              >
                <span>Latest version</span>
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 13.5L12 21m0 0l-7.5-7.5M12 21V3" />
                </svg>
              </button>
            ) : (
              /* Refresh button — live document only */
              <button
                onClick={handleRefresh}
                title="Refresh preview"
                className="p-1.5 rounded-sm hover:bg-p-surface transition-colors"
              >
                <svg className="w-4 h-4 text-p-text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M20.016 4.66v4.993" />
                </svg>
              </button>
            )}
            {/* Fullscreen button */}
            <button
              onClick={() => setFullscreen(true)}
              title="Fullscreen"
              className="p-1.5 rounded-sm hover:bg-p-surface transition-colors"
            >
              <svg className="w-4 h-4 text-p-text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" />
              </svg>
            </button>
            {/* Download button — live document only (the frozen block shows a
                pinned older version; the download token serves current bytes). */}
            {!frozen && (
              <a
                href={safeHref(dlUrl)}
                download={filename}
                title="Download"
                className="p-1.5 rounded-sm hover:bg-p-surface transition-colors"
              >
                <svg className="w-4 h-4 text-p-text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                </svg>
              </a>
            )}
            {/* Close button — hidden in embedded/PiP mode (the window's × closes it). */}
            {!embedded && (
              <button
                onClick={handleDismiss}
                title={frozen ? 'Close this previous version' : 'Close preview'}
                className="p-1.5 rounded-sm hover:bg-p-surface transition-colors"
              >
                <svg className="w-4 h-4 text-p-text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* Collabora iframe — wrapper acts as scroll boundary to prevent chaining to chat */}
        <div className={`relative ${embedded ? 'flex-1 min-h-0' : ''}`} style={{ overflow: 'auto', overscrollBehavior: 'contain', height: embedded ? '100%' : '60vh' }}>
          {reloadAvailable && !frozen && (
            <div className="absolute top-0 inset-x-0 z-20 flex items-center justify-between gap-2 px-3 py-1.5 bg-amber-500/95 text-white text-xs">
              <span>This document changed. Reload to see the latest — your unsaved edits will be discarded.</span>
              <button
                onClick={doReload}
                className="shrink-0 px-2 py-0.5 rounded-sm bg-white/20 hover:bg-white/30 font-medium"
              >
                Reload
              </button>
            </div>
          )}
          {originMismatch && !frozen ? mismatchNotice : (
            <>
              {(!iframeLoaded || (frozen && !frozenUrl)) && (
                <div className="absolute inset-0 flex items-center justify-center bg-p-surface/80 z-10">
                  <div className="flex items-center gap-2 text-sm text-p-text-secondary">
                    <div className="w-4 h-4 border-2 border-brand/30 border-t-brand rounded-full animate-spin" />
                    Loading preview...
                  </div>
                </div>
              )}
              {frameSrc && (
                <iframe
                  ref={iframeRef}
                  key={frameSrc}
                  src={frameSrc}
                  className="w-full border-0"
                  style={{ height: '100%' }}
                  sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
                  allow="clipboard-read; clipboard-write"
                  onLoad={() => setIframeLoaded(true)}
                />
              )}
            </>
          )}
        </div>
      </div>

      {/* Fullscreen portal */}
      {fullscreen && (
        <FilePreviewPortal
          filename={frozen ? `${filename} (previous version)` : filename}
          onClose={() => setFullscreen(false)}
          downloadUrl={frozen ? undefined : dlUrl}
          onReload={frozen ? undefined : handleRefresh}
        >
          {originMismatch && !frozen ? mismatchNotice : frameSrc ? (
            <iframe
              src={frameSrc}
              className="w-full h-full border-0"
              sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
              allow="clipboard-read; clipboard-write"
            />
          ) : null}
        </FilePreviewPortal>
      )}
    </>
  )
}
