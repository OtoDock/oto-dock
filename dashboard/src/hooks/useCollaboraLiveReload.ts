import { useCallback, useEffect, useRef, useState } from 'react'
import { onFileUpdate, type FileUpdate } from '../lib/fileUpdates'

interface Args {
  // Identity of the document this iframe renders — used to match incoming
  // `file_updated` events. Provide whichever the host has: the chat inline
  // preview has `fileId`; the workspace preview has `agentSlug` + `relPath`.
  fileId?: string
  agentSlug?: string
  relPath?: string
  // Actually reload the iframe (bump its cache-busting timestamp).
  reload: () => void
}

/**
 * Keep an embedded Collabora iframe in sync when another user
 * changes the same file, WITHOUT ever discarding unsaved edits.
 *
 * - Tracks Collabora's modified ("dirty") state via its postMessage API. This
 *   requires the WOPI host to set `PostMessageOrigin` (proxy: api/media/wopi.py).
 *   If no status ever arrives (handshake unavailable), `modified` stays false →
 *   we auto-reload (the default, least-surprising behaviour for a view).
 * - On a `file_updated` event matching THIS document:
 *     · source === "collabora": a peer saved via Collabora. Two Collabora
 *       sessions on the same path-keyed file_id share ONE live document, so the
 *       change is already merged here — do nothing.
 *     · source === "disk" (agent / file-tools / dashboard write): Collabora's
 *       in-memory copy is now stale. Reload when the doc is clean; when it has
 *       unsaved edits, surface a manual "Reload" affordance instead of clobbering.
 *
 * Returns an `iframeRef` to attach to the <iframe>, a `reloadAvailable` flag for
 * the manual affordance, and `doReload` for its button.
 */
export function useCollaboraLiveReload({ fileId, agentSlug, relPath, reload }: Args) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const modifiedRef = useRef(false)
  const [reloadAvailable, setReloadAvailable] = useState(false)

  // Enable + listen to Collabora's postMessage status stream.
  useEffect(() => {
    function postReady() {
      try {
        iframeRef.current?.contentWindow?.postMessage(
          JSON.stringify({ MessageId: 'Host_PostmessageReady', Values: {} }),
          '*',
        )
      } catch {
        /* cross-origin / not-ready race — re-armed below + on App_LoadingStatus */
      }
    }
    function onMsg(e: MessageEvent) {
      // Only trust messages from OUR Collabora iframe. The iframe's origin is
      // deployment-specific (any WOPI/COOL host), so we can't match on origin —
      // but source-window matching blocks any other frame/window from spoofing
      // Doc_ModifiedStatus (to suppress reloads) or App_LoadingStatus.
      if (e.source !== iframeRef.current?.contentWindow) return
      let data: any
      try {
        data = typeof e.data === 'string' ? JSON.parse(e.data) : e.data
      } catch {
        return
      }
      if (!data || typeof data.MessageId !== 'string') return
      if (data.MessageId === 'App_LoadingStatus') {
        // COOL is up — (re)enable the integration API so Doc_ModifiedStatus flows.
        postReady()
      } else if (data.MessageId === 'Doc_ModifiedStatus') {
        modifiedRef.current = !!data?.Values?.Modified
      }
    }
    window.addEventListener('message', onMsg)
    // COOL ignores Host_PostmessageReady sent before its JS is ready, so also
    // arm it on a short delay (best-effort; App_LoadingStatus re-arms it too).
    const t = setTimeout(postReady, 1500)
    return () => {
      window.removeEventListener('message', onMsg)
      clearTimeout(t)
    }
  }, [])

  const matches = useCallback(
    (u: FileUpdate) => {
      if (fileId && u.file_id) return u.file_id === fileId
      if (agentSlug && relPath && u.agent_slug && u.rel_path)
        return u.agent_slug === agentSlug && u.rel_path === relPath
      return false
    },
    [fileId, agentSlug, relPath],
  )

  useEffect(() => {
    return onFileUpdate((u) => {
      if (!matches(u)) return
      if (u.source === 'collabora') return // already live-merged into this session
      if (modifiedRef.current) {
        setReloadAvailable(true) // unsaved edits — never clobber
      } else {
        reload()
      }
    })
  }, [matches, reload])

  const doReload = useCallback(() => {
    setReloadAvailable(false)
    modifiedRef.current = false
    reload()
  }, [reload])

  return { iframeRef, reloadAvailable, doReload }
}
