import { useCallback, useEffect, useRef, useState } from 'react'
import type { FileNode } from '../../api/agents'
import { apiFetch } from '../../api/auth'
import { apiJson } from '../../lib/http/apiJson'
import { getFileKind } from '../../lib/fileTypes'
import { encodePathSegments } from '../../lib/paths'
import { useCollaboraLiveReload } from '../../hooks/useCollaboraLiveReload'
import { postSaveAndWait, useCollaboraIdle } from '../../hooks/useCollaboraIdle'
import FileEditor from '../FileEditor'
import FilePreviewPortal from './FilePreviewPortal'
import VideoPlayer from '../chat/media/VideoPlayer'
import AudioPlayer from '../chat/media/AudioPlayer'
import { STAGE_FRAME, STAGE_TEXT, STAGE_TEXT_MUTED } from '../chat/media/stage'
import CollaboraFrame, { type CollaboraFrameData } from '../chat/media/CollaboraFrame'
import Button from '../ui/Button'

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
        bodyBg="bg-surface-card"
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
      <div className="h-full flex items-center justify-center text-secondary text-sm">
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
        if (!res.ok) throw new Error('Could not load the image.')
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
    <FilePreviewPortal filename={node.name} onClose={onClose} downloadUrl={downloadUrl} bodyBg={STAGE_FRAME}>
      <div className="h-full w-full flex items-center justify-center p-4 overflow-hidden">
        {error && <span className={`${STAGE_TEXT} text-sm`}>{error}</span>}
        {!error && !src && (
          <span className={`${STAGE_TEXT_MUTED} text-sm`}>Loading…</span>
        )}
        {src && (
          <img
            src={src}
            alt={node.name}
            className="max-w-full max-h-full object-contain rounded-card shadow-elevation-2"
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
    apiJson<{ url: string }>('/v1/media/token', {
      method: 'POST',
      json: { agent, path: node.path },
    }, 'Could not open the preview')
      .then((data) => !cancelled && setSrc(data.url))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    return () => { cancelled = true }
  }, [agent, node.path])

  return (
    // A video keeps the media stage, as in the chat; audio is a player, not a
    // picture, and sits on the sunken step.
    <FilePreviewPortal filename={node.name} onClose={onClose} downloadUrl={downloadUrl}
      bodyBg={kind === 'video' ? STAGE_FRAME : 'bg-surface-sunken'}>
      <div className="h-full w-full flex items-center justify-center p-4 overflow-hidden">
        {error && <span className={`${kind === 'video' ? STAGE_TEXT : 'text-status-danger-fg'} text-sm`}>{error}</span>}
        {!error && !src && <span className={`${kind === 'video' ? STAGE_TEXT_MUTED : 'text-tertiary'} text-sm`}>Preparing…</span>}
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
  const [loadKey, setLoadKey] = useState(0)
  // A failure belongs to the load that met it: the next load starts clean.
  const loadId = `${agent}|${node.path}|${canWrite}|${loadKey}`
  const [failed, setFailed] = useState<{ load: string; message: string } | null>(null)
  const error = failed?.load === loadId ? failed.message : null
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
    apiJson<{ wopi_url: string; access_token: string; access_token_ttl: number }>('/v1/documents/wopi-url', {
      method: 'POST',
      json: { file_path: node.path, agent, edit: canWrite },
    }, 'Could not open the document')
      .then((data) => {
        if (cancelled) return
        setFrame({ url: data.wopi_url, token: data.access_token, ttl: data.access_token_ttl })
        setFrameKey(loadKey)
      })
      .catch((e) => {
        if (cancelled) return
        // A token from an earlier mint may be past its end: no frame then.
        setFrame(null)
        setFailed({ load: loadId, message: e instanceof Error ? e.message : String(e) })
      })
    return () => {
      cancelled = true
    }
  }, [agent, node.path, canWrite, loadKey, loadId])

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
      <div ref={hostRef} className="relative h-full w-full bg-surface-sunken">
        {reloadAvailable && (
          <div className="absolute top-0 inset-x-0 z-20 flex items-center justify-between gap-2 px-3 py-1.5 bg-status-warning-bg text-status-warning-fg border-b border-status-warning-border text-xs">
            <span>This document changed. Reload to see the latest. Your unsaved edits will be lost.</span>
            <Button size="xs" className="shrink-0" onClick={doReload}>
              Reload
            </Button>
          </div>
        )}
        {error && (
          <div className="h-full flex flex-col items-center justify-center gap-2 text-status-danger-fg text-sm px-4 text-center">
            <span>{error}</span>
            <Button size="xs" onClick={reload}>
              Retry
            </Button>
          </div>
        )}
        {!error && !frame && (
          <div className="h-full flex items-center justify-center text-tertiary text-sm">
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
