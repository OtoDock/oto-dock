import { useCallback, useEffect, useRef, useState } from 'react'
import type { FileNode } from '../../api/agents'
import { apiFetch } from '../../api/auth'
import { getFileKind } from '../../lib/fileTypes'
import { encodePathSegments } from '../../lib/paths'
import { useCollaboraLiveReload } from '../../hooks/useCollaboraLiveReload'
import { postSaveAndWait, useCollaboraIdle } from '../../hooks/useCollaboraIdle'
import FileEditor from '../FileEditor'
import FilePreviewPortal from './FilePreviewPortal'
import VideoPlayer from '../chat/media/VideoPlayer'
import AudioPlayer from '../chat/media/AudioPlayer'
import CollaboraFrame, { type CollaboraFrameData } from '../chat/media/CollaboraFrame'

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
 * - document → Collabora iframe (WOPI flow). Each load mints (the first,
 *   Reload, a `file_updated` reload, the idle recovery), so a reload opens
 *   the file's newest document; React Query invalidation on the file tree
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
  // The frame each load minted, kept in component state; never re-derived
  // from React Query state. The frame key moves only when a mint lands: a
  // key alone would post the previous load's token into a new iframe.
  const [frame, setFrame] = useState<CollaboraFrameData | null>(null)
  const [frameKey, setFrameKey] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [loadKey, setLoadKey] = useState(0)
  const reload = useCallback(() => setLoadKey((k) => k + 1), [])
  const hostRef = useRef<HTMLDivElement | null>(null)

  // Live-reload when another user changes this same file,
  // dirty-guarded so unsaved edits are never silently discarded.
  const { iframeRef, reloadAvailable, doReload, modifiedRef } = useCollaboraLiveReload({
    agentSlug: agent,
    relPath: node.path,
    reload,
  })

  useEffect(() => {
    let cancelled = false
    setError(null)
    apiFetch('/v1/documents/wopi-url', {
      method: 'POST',
      body: JSON.stringify({ file_path: node.path, agent, edit: canWrite }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(await res.text())
        return res.json()
      })
      .then((data) => {
        if (cancelled) return
        setFrame({ url: data.wopi_url, token: data.access_token, ttl: data.access_token_ttl })
        setFrameKey(loadKey)
      })
      .catch((e) => {
        if (cancelled) return
        // A token from an earlier mint may be past its end: no frame then.
        setFrame(null)
        setError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      cancelled = true
    }
  }, [agent, node.path, canWrite, loadKey])

  // Collabora's idle end: a resume in place when it can, else a fresh mint.
  // Not while the Reload banner is up (the person decides); unsaved edits
  // are saved first (bounded).
  const recoverIdle = useCallback(() => {
    if (reloadAvailable) return
    void (async () => {
      const win = iframeRef.current?.contentWindow
      if (modifiedRef.current && win) await postSaveAndWait(win, 5000)
      doReload()
    })()
  }, [reloadAvailable, iframeRef, modifiedRef, doReload])
  useCollaboraIdle({ iframeRef, hostRef, loadKey, token: frame?.token, recover: recoverIdle })

  return (
    <FilePreviewPortal
      filename={node.name}
      onClose={onClose}
      downloadUrl={downloadUrl}
      onReload={reload}
    >
      <div ref={hostRef} className="relative h-full w-full bg-black">
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
          <div className="h-full flex flex-col items-center justify-center gap-2 text-red-300 text-sm px-4 text-center">
            <span>{error}</span>
            <button
              onClick={reload}
              className="px-2 py-1 rounded-md border border-white/30 hover:bg-white/10 text-xs text-white/80"
            >
              Retry
            </button>
          </div>
        )}
        {!error && !frame && (
          <div className="h-full flex items-center justify-center text-white/70 text-sm">
            Loading preview…
          </div>
        )}
        {!error && frame && (
          <CollaboraFrame
            iframeRef={iframeRef}
            url={frame.url}
            accessToken={frame.token}
            accessTokenTtl={frame.ttl}
            frameKey={frameKey}
            className="w-full h-full border-0"
          />
        )}
      </div>
    </FilePreviewPortal>
  )
}
