import { useState, useCallback } from 'react'
import { safeHref } from '../../../lib/safeUrl'
import FilePreviewPortal from '../../workspace/FilePreviewPortal'
import { useCollaboraLiveReload } from '../../../hooks/useCollaboraLiveReload'

interface Props {
  wopiUrl: string
  filename: string
  fileId: string
  downloadUrl: string
  dbMessageId?: number
  chatId?: string
  onDismiss?: () => void
  /** Render inside an interactive-CLI PiP window: the
   * floating window supplies the title + minimize + close chrome, so this hides
   * its own filename + close button (no duplicate), keeps the refresh/fullscreen/
   * download actions, and fills the window height instead of a fixed 60vh. */
  embedded?: boolean
}

function getFileExtBadge(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const labels: Record<string, string> = {
    pdf: 'PDF', docx: 'DOCX', doc: 'DOC', xlsx: 'XLSX', xls: 'XLS',
    pptx: 'PPTX', ppt: 'PPT', odt: 'ODT', ods: 'ODS', odp: 'ODP',
    csv: 'CSV', txt: 'TXT', html: 'HTML', rtf: 'RTF',
  }
  return labels[ext] || ext.toUpperCase()
}

export default function DocumentPreview({ wopiUrl, filename, fileId, downloadUrl, dbMessageId, chatId, onDismiss, embedded }: Props) {
  const [fullscreen, setFullscreen] = useState(false)
  const [iframeLoaded, setIframeLoaded] = useState(false)
  const [refreshTs, setRefreshTs] = useState<number | null>(null)

  // Replace _t= timestamp to force iframe reload on refresh
  const effectiveUrl = refreshTs
    ? wopiUrl.replace(/&_t=\d+/, `&_t=${refreshTs}`)
    : wopiUrl

  const handleRefresh = useCallback(() => {
    setIframeLoaded(false)
    setRefreshTs(Date.now())
  }, [])

  // Live-reload when another user changes this same file.
  // Dirty-guarded: an agent/disk write auto-reloads only when there are no
  // unsaved edits; otherwise it surfaces a manual "Reload" affordance.
  const { iframeRef, reloadAvailable, doReload } = useCollaboraLiveReload({
    fileId,
    reload: handleRefresh,
  })

  const handleDismiss = useCallback(async () => {
    if (chatId && fileId) {
      try {
        await fetch(`/v1/chats/${chatId}/dismiss-preview/${encodeURIComponent(fileId)}`, {
          method: 'PATCH',
          credentials: 'include',
        })
      } catch { /* Best-effort */ }
    }
    onDismiss?.()
  }, [chatId, fileId, onDismiss])

  const extBadge = getFileExtBadge(filename)
  const dlUrl = `${downloadUrl}${downloadUrl.includes('?') ? '&' : '?'}fn=${encodeURIComponent(filename)}`

  // A preview URL whose origin differs from the page can never load — the
  // browser (and Collabora's frame-ancestors check) refuses the frame. The
  // classic first-run miss: DASHBOARD_PUBLIC_URL left at its localhost
  // default while browsing the server from another machine. Detectable right
  // here, so explain the fix instead of rendering a dead frame.
  const previewOrigin = (() => {
    try { return new URL(wopiUrl, window.location.href).origin } catch { return '' }
  })()
  const originMismatch = !!previewOrigin && previewOrigin !== window.location.origin

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

  return (
    <>
      <div
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
            </div>
          )}
          <div className="flex items-center gap-1 shrink-0 ml-2">
            {/* Refresh button */}
            <button
              onClick={handleRefresh}
              title="Refresh preview"
              className="p-1.5 rounded-sm hover:bg-p-surface transition-colors"
            >
              <svg className="w-4 h-4 text-p-text-secondary" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M20.016 4.66v4.993" />
              </svg>
            </button>
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
            {/* Download button */}
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
            {/* Close button — hidden in embedded/PiP mode (the window's × closes it). */}
            {!embedded && (
              <button
                onClick={handleDismiss}
                title="Close preview"
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
          {reloadAvailable && (
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
          {originMismatch ? mismatchNotice : (
            <>
              {!iframeLoaded && (
                <div className="absolute inset-0 flex items-center justify-center bg-p-surface/80 z-10">
                  <div className="flex items-center gap-2 text-sm text-p-text-secondary">
                    <div className="w-4 h-4 border-2 border-brand/30 border-t-brand rounded-full animate-spin" />
                    Loading preview...
                  </div>
                </div>
              )}
              <iframe
                ref={iframeRef}
                key={effectiveUrl}
                src={effectiveUrl}
                className="w-full border-0"
                style={{ height: '100%' }}
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
                allow="clipboard-read; clipboard-write"
                onLoad={() => setIframeLoaded(true)}
              />
            </>
          )}
        </div>
      </div>

      {/* Fullscreen portal */}
      {fullscreen && (
        <FilePreviewPortal
          filename={filename}
          onClose={() => setFullscreen(false)}
          downloadUrl={dlUrl}
          onReload={handleRefresh}
        >
          {originMismatch ? mismatchNotice : (
            <iframe
              src={effectiveUrl}
              className="w-full h-full border-0"
              sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
              allow="clipboard-read; clipboard-write"
            />
          )}
        </FilePreviewPortal>
      )}
    </>
  )
}
