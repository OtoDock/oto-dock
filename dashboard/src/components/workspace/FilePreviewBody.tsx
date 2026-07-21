import { useCallback, useEffect, useState } from 'react'
import type { FileNode } from '../../api/agents'
import { apiFetch } from '../../api/auth'
import { getFileKind } from '../../lib/fileTypes'
import { encodePathSegments } from '../../lib/paths'
import { useCollaboraLiveReload } from '../../hooks/useCollaboraLiveReload'
import FileEditor from '../FileEditor'
import FilePreviewPortal from './FilePreviewPortal'
import VideoPlayer from '../chat/media/VideoPlayer'
import AudioPlayer from '../chat/media/AudioPlayer'

interface Props {
  agent: string
  node: FileNode
  canWrite: boolean
  onClose: () => void
}

/**
 * Routes a file to the correct preview body inside the shared
 * `FilePreviewPortal` chrome.
 *
 * - text  → embedded `FileEditor` (writable if `canWrite`)
 * - image → centered `<img>` with object-contain
 * - document → Collabora iframe (WOPI flow). The wopi_url is cached in
 *   component state at mount; React Query invalidation on the file tree
 *   does NOT re-derive the iframe key.
 * - other → a download-only fallback
 */
export default function FilePreviewBody({ agent, node, canWrite, onClose }: Props) {
  const kind = getFileKind(node.name)
  // `fn=` is read by the Android app's DownloadListener (MainActivity).
  // Browsers ignore it; they use the link's `download` attribute.
  const downloadUrl = `/v1/agents/${encodeURIComponent(agent)}/files/${encodePathSegments(node.path)}?download=true&fn=${encodeURIComponent(node.name)}`

  if (kind === 'text') {
    return (
      <FilePreviewPortal
        filename={node.name}
        onClose={onClose}
        downloadUrl={downloadUrl}
        bodyBg="bg-white dark:bg-gray-900"
      >
        <FileEditor agent={agent} path={node.path} readOnly={!canWrite} compact />
      </FilePreviewPortal>
    )
  }

  if (kind === 'image') {
    return <ImagePreview agent={agent} node={node} downloadUrl={downloadUrl} onClose={onClose} />
  }

  if (kind === 'video') {
    return <MediaPreview kind="video" agent={agent} node={node} downloadUrl={downloadUrl} onClose={onClose} />
  }

  if (kind === 'audio') {
    return <MediaPreview kind="audio" agent={agent} node={node} downloadUrl={downloadUrl} onClose={onClose} />
  }

  if (kind === 'document') {
    return <DocumentPreview agent={agent} node={node} downloadUrl={downloadUrl} canWrite={canWrite} onClose={onClose} />
  }

  // Archive / unknown — just offer the download.
  return (
    <FilePreviewPortal filename={node.name} onClose={onClose} downloadUrl={downloadUrl}>
      <div className="h-full flex items-center justify-center text-white/80 text-sm">
        Preview unavailable for this file type. Use the download button above.
      </div>
    </FilePreviewPortal>
  )
}

// ---------------------------------------------------------------------------
// Image preview
// ---------------------------------------------------------------------------

function ImagePreview({
  agent,
  node,
  downloadUrl,
  onClose,
}: {
  agent: string
  node: FileNode
  downloadUrl: string
  onClose: () => void
}) {
  const [src, setSrc] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let blobUrl: string | null = null
    let cancelled = false
    apiFetch(`/v1/agents/${encodeURIComponent(agent)}/files/${encodePathSegments(node.path)}`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const blob = await res.blob()
        blobUrl = URL.createObjectURL(blob)
        // If we unmounted before the fetch resolved, the cleanup already ran
        // (with blobUrl still null) — revoke here so this URL isn't orphaned.
        if (cancelled) {
          URL.revokeObjectURL(blobUrl)
          blobUrl = null
          return
        }
        setSrc(blobUrl)
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
      if (blobUrl) URL.revokeObjectURL(blobUrl)
    }
  }, [agent, node.path])

  return (
    <FilePreviewPortal filename={node.name} onClose={onClose} downloadUrl={downloadUrl}>
      <div className="h-full w-full flex items-center justify-center p-4 overflow-hidden">
        {error && <span className="text-red-300 text-sm">{error}</span>}
        {!error && !src && (
          <span className="text-white/70 text-sm">Loading…</span>
        )}
        {src && (
          <img
            src={src}
            alt={node.name}
            className="max-w-full max-h-full object-contain rounded-lg shadow-2xl"
          />
        )}
      </div>
    </FilePreviewPortal>
  )
}

// ---------------------------------------------------------------------------
// Audio / video preview — mints a capability token (so <video>/<audio> can
// stream with Range, which a header-authenticated blob fetch can't), then
// reuses the chat players. The proxy transcodes non-web-native codecs.
// ---------------------------------------------------------------------------

function MediaPreview({
  kind,
  agent,
  node,
  downloadUrl,
  onClose,
}: {
  kind: 'video' | 'audio'
  agent: string
  node: FileNode
  downloadUrl: string
  onClose: () => void
}) {
  const [src, setSrc] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    apiFetch('/v1/media/token', {
      method: 'POST',
      body: JSON.stringify({ agent, path: node.path }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(await res.text())
        return res.json()
      })
      .then((data) => !cancelled && setSrc(data.url))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => { cancelled = true }
  }, [agent, node.path])

  return (
    <FilePreviewPortal filename={node.name} onClose={onClose} downloadUrl={downloadUrl}>
      <div className="h-full w-full flex items-center justify-center p-4 overflow-hidden">
        {error && <span className="text-red-300 text-sm">{error}</span>}
        {!error && !src && <span className="text-white/70 text-sm">Preparing…</span>}
        {src && (
          <div className="w-full max-w-3xl">
            {kind === 'video'
              ? <VideoPlayer src={src} canDownload={false} />
              : <AudioPlayer src={src} canDownload={false} />}
          </div>
        )}
      </div>
    </FilePreviewPortal>
  )
}

// ---------------------------------------------------------------------------
// Collabora document preview
// ---------------------------------------------------------------------------

function DocumentPreview({
  agent,
  node,
  downloadUrl,
  canWrite,
  onClose,
}: {
  agent: string
  node: FileNode
  downloadUrl: string
  canWrite: boolean
  onClose: () => void
}) {
  // Cache the wopi_url at mount; never re-derive from React Query state.
  const [wopiUrl, setWopiUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reloadTs, setReloadTs] = useState(0)
  const reload = useCallback(() => setReloadTs(Date.now()), [])

  // Live-reload when another user changes this same file,
  // dirty-guarded so unsaved edits are never silently discarded.
  const { iframeRef, reloadAvailable, doReload } = useCollaboraLiveReload({
    agentSlug: agent,
    relPath: node.path,
    reload,
  })

  useEffect(() => {
    let cancelled = false
    apiFetch('/v1/documents/wopi-url', {
      method: 'POST',
      body: JSON.stringify({ file_path: node.path, agent, edit: canWrite }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(await res.text())
        return res.json()
      })
      .then((data) => !cancelled && setWopiUrl(data.wopi_url))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      cancelled = true
    }
  }, [agent, node.path, canWrite])

  const effectiveUrl = wopiUrl
    ? reloadTs
      ? wopiUrl.includes('?')
        ? `${wopiUrl}&_r=${reloadTs}`
        : `${wopiUrl}?_r=${reloadTs}`
      : wopiUrl
    : null

  return (
    <FilePreviewPortal
      filename={node.name}
      onClose={onClose}
      downloadUrl={downloadUrl}
      onReload={reload}
    >
      <div className="relative h-full w-full bg-black">
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
        {error && (
          <div className="h-full flex items-center justify-center text-red-300 text-sm px-4 text-center">
            {error}
          </div>
        )}
        {!error && !effectiveUrl && (
          <div className="h-full flex items-center justify-center text-white/70 text-sm">
            Loading preview…
          </div>
        )}
        {effectiveUrl && (
          <iframe
            ref={iframeRef}
            key={effectiveUrl}
            src={effectiveUrl}
            className="w-full h-full border-0"
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
            allow="clipboard-read; clipboard-write; fullscreen"
              allowFullScreen
          />
        )}
      </div>
    </FilePreviewPortal>
  )
}
