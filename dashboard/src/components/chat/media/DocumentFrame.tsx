import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { useAuth } from '../../../contexts/AuthContext'
import { useCollaboraLiveReload } from '../../../hooks/useCollaboraLiveReload'
import { emitFileUpdate } from '../../../lib/fileUpdates'
import type { PushedDocument } from '../../../store/documentPaneStore'
import CollaboraFrame, { type CollaboraFrameData } from './CollaboraFrame'

/** How long after its push a pushed token is trusted. Tokens last 4 h, but
 * the push may reuse a token cached for up to 3.5 h, so about 30 minutes of
 * validity is the only guarantee. Past this age (or with no pushed token)
 * each load mints a fresh one through the chat-scoped route. */
export const LIVE_URL_TRUST_MS = 15 * 60 * 1000

/** How long a leave waits for Collabora to answer the save it asked for. */
export const LEAVE_SAVE_TIMEOUT_MS = 5000

/** How long a send waits for that answer: a message goes out even when the
 * editor is slow to confirm. */
export const SEND_SAVE_TIMEOUT_MS = 1500

/** A push reaches the frame twice (its own frame and the file_updated the
 * write sends). In a terminal chat the push comes first: a file update this
 * soon after the push's own load is the same news and does not reload. */
export const RELOAD_DEDUPE_MS = 1000

/** How long after a load starts a focus taken by the editor is handed back
 * (the cap; Collabora's "document loaded" ends it earlier). */
const FOCUS_GUARD_MS = 8000
/** Collabora focuses its editing surface once more about half a second
 * after it reports the document loaded. */
export const FOCUS_AFTER_LOAD_MS = 1500

export interface DocumentFrameHandle {
  /** Leave a live document: a clean one at once; one with unsaved edits
   * after Collabora confirms its save. False keeps it on show. */
  leave: () => Promise<boolean>
  /** Save a live document's unsaved edits and stay on it: true once
   * Collabora confirms the save (or when there is nothing to save), false
   * when it does not confirm in time (`timeoutMs`, the leave's wait by
   * default) or the file changed under the edits. */
  save: (timeoutMs?: number) => Promise<boolean>
  refresh: () => void
}

interface Props {
  chatId: string
  fileId: string
  filename: string
  /** null: the live file; a snapshot id: that version, read-only. */
  snapshotId: string | null
  /** The pane is on show: a reload that arrives while hidden waits. */
  shown: boolean
  /** The file's newest live push, in memory (with its token). */
  pushed?: PushedDocument
  onDirtyChange?: (dirty: boolean) => void
  /** The live file is gone (its mint answered 404). */
  onLiveGone?: () => void
  /** The version's copy is gone (its mint answered 404). */
  onVersionGone?: () => void
  /** What the live load's token allows: the mint's `permissions`, or the
   * pushed token's own claim; null while unknown. */
  onPermissions?: (permissions: 'edit' | 'view' | null) => void
  className?: string
}

type Status = 'loading' | 'ready' | 'failed' | 'gone'

/** The `permissions` claim of a WOPI token (a JWT), for the words only:
 * the WOPI host enforces it. */
export const tokenPermissions = (token: string | null | undefined): 'edit' | 'view' | null => {
  try {
    const part = (token ?? '').split('.')[1] ?? ''
    const claims = JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')))
    return claims?.permissions === 'edit' || claims?.permissions === 'view' ? claims.permissions : null
  } catch {
    return null
  }
}

const permissionsOf = (j: { permissions?: unknown } | null): 'edit' | 'view' | null =>
  j?.permissions === 'edit' || j?.permissions === 'view' ? j.permissions : null

const frameFrom = (j: { wopi_url?: string; access_token?: string; access_token_ttl?: number } | null): CollaboraFrameData | null =>
  j?.wopi_url ? { url: j.wopi_url, token: j.access_token, ttl: j.access_token_ttl } : null

/** The document a host page URL opens (its WOPISrc). */
const wopiSrcOf = (url: string | null | undefined): string => {
  if (!url) return ''
  try { return new URL(url, window.location.href).searchParams.get('WOPISrc') ?? '' } catch { return '' }
}

const originOf = (url: string | null | undefined): string => {
  if (!url) return ''
  try { return new URL(url, window.location.href).origin } catch { return '' }
}

const parseMessage = (data: unknown): { MessageId?: string; Values?: any } | null => {
  try {
    const d = typeof data === 'string' ? JSON.parse(data) : data
    return d && typeof d === 'object' ? d as { MessageId?: string; Values?: any } : null
  } catch {
    return null
  }
}

const editable = (el: Element | null): el is HTMLElement =>
  !!el && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
    || (el instanceof HTMLElement && el.isContentEditable))

/** The editor of the document pane: the live file (`preview-wopi-url`) or a
 * version (`snapshot-wopi-url`, view only). Tokens are minted per load and
 * kept in this component's state only; `CollaboraFrame` posts them. */
const DocumentFrame = forwardRef<DocumentFrameHandle, Props>(function DocumentFrame({
  chatId, fileId, filename, snapshotId, shown, pushed, onDirtyChange, onLiveGone, onVersionGone,
  onPermissions, className,
}, ref) {
  const { authConfig } = useAuth()
  const live = !snapshotId
  const [loadKey, setLoadKey] = useState(0)
  const [frame, setFrame] = useState<CollaboraFrameData | null>(null)
  // The editor frame's key moves with the frame a load decided, never
  // ahead of it: a key alone would post the previous load's URL once more.
  const [frameKey, setFrameKey] = useState(0)
  const [status, setStatus] = useState<Status>('loading')
  const [loaded, setLoaded] = useState(false)
  const [note, setNote] = useState('')
  const hostRef = useRef<HTMLDivElement | null>(null)
  const shownRef = useRef(shown)
  shownRef.current = shown
  const pendingReload = useRef(false)
  const pushedRef = useRef(pushed)
  pushedRef.current = pushed
  const onPermissionsRef = useRef(onPermissions)
  onPermissionsRef.current = onPermissions

  const reload = useCallback(() => {
    if (!shownRef.current) {
      pendingReload.current = true
      return
    }
    setLoadKey((k) => k + 1)
  }, [])

  // What the next load opens. The first load and a push's reload take the
  // pushed frame while it is fresh (its token, its push's document). A
  // reload for any other change mints: the file's newest document, which
  // in a headless turn is a push whose frame arrives only at the turn's
  // flush. The person's Refresh and Reload always load.
  const lastGeneration = useRef(pushed?.generation ?? 0)
  const nextFromPush = useRef(true)
  const pushNews = useRef(false)
  const forceReload = useRef(false)
  const lastLoad = useRef({ at: 0, generation: -1, fromPush: false })
  const loadedSrc = useRef('')
  const reloadOnChange = useCallback(() => {
    const fromPush = pushNews.current
    pushNews.current = false
    const last = lastLoad.current
    const sameNews = !fromPush && last.fromPush && last.generation === lastGeneration.current
      && Date.now() - last.at < RELOAD_DEDUPE_MS
    if (sameNews && !forceReload.current) return
    forceReload.current = false
    nextFromPush.current = fromPush
    reload()
  }, [reload])

  useEffect(() => {
    if (shown && pendingReload.current) {
      pendingReload.current = false
      setLoadKey((k) => k + 1)
    }
  }, [shown])

  // A file_updated disk change reloads a clean live document and offers
  // Reload on unsaved edits; a version never reloads (its copy is fixed).
  const { iframeRef, reloadAvailable, doReload, modifiedRef } = useCollaboraLiveReload({
    fileId: live ? fileId : undefined,
    reload: reloadOnChange,
  })
  const reloadNow = useCallback(() => {
    forceReload.current = true
    doReload()
  }, [doReload])
  const changedUnderEditsRef = useRef(reloadAvailable)
  changedUnderEditsRef.current = reloadAvailable

  // A new push of the file on show is the same news as a disk change, but
  // a document a file update already opened (the write's file_updated came
  // first) is not loaded again.
  useEffect(() => {
    const generation = pushed?.generation ?? 0
    if (generation <= lastGeneration.current) return
    lastGeneration.current = generation
    if (!live) return
    const src = wopiSrcOf(pushed?.wopiUrl)
    if (src && src === loadedSrc.current) return
    pushNews.current = true
    emitFileUpdate({ agent_slug: '', rel_path: '', file_id: fileId, source: 'disk' })
  }, [pushed?.generation, pushed?.wopiUrl, live, fileId])

  // Each load decides its own source: a pushed token while its push is
  // under 15 minutes old and the load is the push's, else a fresh mint.
  // Never persisted.
  useEffect(() => {
    let alive = true
    const fromPush = nextFromPush.current
    nextFromPush.current = false
    lastLoad.current = { at: Date.now(), generation: lastGeneration.current, fromPush }
    loadedSrc.current = ''  // until this load decides its frame
    setStatus('loading')
    setLoaded(false)
    setFrame(null)
    const p = pushedRef.current
    const pushedFrame: CollaboraFrameData | null = live && p?.accessToken && p.wopiUrl
      ? { url: p.wopiUrl, token: p.accessToken, ttl: p.accessTokenTtl } : null
    const allows = (perm: 'edit' | 'view' | null) => { if (live) onPermissionsRef.current?.(perm) }
    const takePushed = () => {
      loadedSrc.current = wopiSrcOf(pushedFrame!.url)
      setFrame(pushedFrame)
      setFrameKey(loadKey)
      setStatus('ready')
      allows(tokenPermissions(pushedFrame!.token))
    }
    allows(null)
    if (fromPush && pushedFrame && Date.now() - p!.generation < LIVE_URL_TRUST_MS) {
      takePushed()
      return
    }
    const url = live
      ? `/v1/documents/preview-wopi-url?chat_id=${encodeURIComponent(chatId)}&file_id=${encodeURIComponent(fileId)}`
      : `/v1/documents/snapshot-wopi-url?chat_id=${encodeURIComponent(chatId)}&snapshot_id=${encodeURIComponent(snapshotId!)}`
    fetch(url, { credentials: 'include' })
      .then(async (r) => {
        if (!alive) return
        if (r.ok) {
          const j = await r.json()
          const f = frameFrom(j)
          if (f) {
            loadedSrc.current = wopiSrcOf(f.url)
            setFrame(f)
            setFrameKey(loadKey)
            setStatus('ready')
            allows(permissionsOf(j))
            return
          }
        }
        if (pushedFrame) { takePushed(); return }
        if (r.status === 404) {
          setStatus('gone')
          if (live) onLiveGone?.()
          else onVersionGone?.()
          return
        }
        setStatus('failed')
      })
      .catch(() => {
        if (!alive) return
        if (pushedFrame) takePushed()
        else setStatus('failed')
      })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadKey, chatId, fileId, snapshotId])

  // Collabora's status stream: the dirty flag (for the pane and the page's
  // leave prompt), the end of loading (the focus guard).
  const loadingDone = useRef<() => void>(() => {})
  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      if (e.source !== iframeRef.current?.contentWindow) return
      const d = parseMessage(e.data)
      if (d?.MessageId === 'Doc_ModifiedStatus') onDirtyChange?.(!!d.Values?.Modified)
      else if (d?.MessageId === 'App_LoadingStatus' && d.Values?.Status === 'Document_Loaded') loadingDone.current()
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, [iframeRef, onDirtyChange])

  // A document that boots while the person types elsewhere must not take
  // the keyboard: Collabora focuses its editing surface when it loads. From
  // each load until the document reports loaded (or the cap), a focus on the
  // frame is handed back to the field that held it, unless the person
  // pointed at the pane meanwhile.
  useEffect(() => {
    const host = hostRef.current
    if (!host || !frame) return
    const held = document.activeElement
    if (!editable(held) || host.closest('[data-document-pane]')?.contains(held)) return
    let armed = true
    // A touch inside the editor never reaches this page, so on a touch
    // screen the guard cannot learn the person tapped the document: it
    // hands the focus back once, then lets the editor keep it.
    const coarse = window.matchMedia?.('(pointer: coarse)').matches ?? false
    const pane = host.closest('[data-document-pane]') ?? host
    const disarm = () => { armed = false }
    // A pane sliding in under a resting pointer sends boundary events with
    // no movement: only a real move or a press counts as the person's.
    const onMove = (e: PointerEvent) => { if (e.movementX || e.movementY) armed = false }
    const reclaim = () => {
      const a = document.activeElement
      if (armed && held.isConnected && a instanceof HTMLIFrameElement && host.contains(a)) {
        held.focus()
        if (coarse) armed = false
      }
    }
    // A frame that focuses itself from inside fires no focusin on the frame
    // element in Chrome: the page's window blurs instead.
    const onBlur = () => { setTimeout(reclaim, 0) }
    const cap = setTimeout(disarm, FOCUS_GUARD_MS)
    loadingDone.current = () => { setTimeout(disarm, FOCUS_AFTER_LOAD_MS) }
    host.addEventListener('focusin', reclaim)
    window.addEventListener('blur', onBlur)
    pane.addEventListener('pointerdown', disarm, { capture: true })
    pane.addEventListener('pointermove', onMove as EventListener)
    // A pointer moving straight into the editor reaches the page only as
    // the frame's pointerover.
    pane.addEventListener('pointerover', onMove as EventListener)
    return () => {
      armed = false
      clearTimeout(cap)
      loadingDone.current = () => {}
      host.removeEventListener('focusin', reclaim)
      window.removeEventListener('blur', onBlur)
      pane.removeEventListener('pointerdown', disarm, { capture: true })
      pane.removeEventListener('pointermove', onMove as EventListener)
      pane.removeEventListener('pointerover', onMove as EventListener)
    }
  }, [frame, loadKey])

  // Ask Collabora to save the unsaved edits and wait for its answer. True
  // when there is nothing to save.
  const requestSave = useCallback(async (timeoutMs = LEAVE_SAVE_TIMEOUT_MS): Promise<boolean> => {
    if (!modifiedRef.current) return true
    const win = iframeRef.current?.contentWindow
    if (!win) return true
    const ok = await new Promise<boolean>((resolve) => {
      const done = (v: boolean) => {
        window.removeEventListener('message', onMsg)
        clearTimeout(timer)
        resolve(v)
      }
      const onMsg = (e: MessageEvent) => {
        if (e.source !== win) return
        const d = parseMessage(e.data)
        if (d?.MessageId === 'Action_Save_Resp') done(d.Values?.success === true)
      }
      // Collabora answers once its own save is done (whatever the upload
      // answered); no answer in time keeps the document.
      const timer = setTimeout(() => done(false), timeoutMs)
      window.addEventListener('message', onMsg)
      try {
        win.postMessage(JSON.stringify({
          MessageId: 'Action_Save',
          Values: { DontTerminateEdit: true, DontSaveIfUnmodified: true, Notify: true },
        }), '*')
      } catch {
        done(false)
      }
    })
    if (ok) {
      modifiedRef.current = false
      onDirtyChange?.(false)
    }
    return ok
  }, [iframeRef, modifiedRef, onDirtyChange])

  const leave = useCallback(async (): Promise<boolean> => {
    if (!live) return true
    // The file changed under these edits: a save is refused (WOPI 1010), and
    // Collabora confirms its in-editor save whatever the upload answers and
    // then reports the document unmodified, so the banner, not the dirty
    // flag, tells: only the person can resolve it, in the editor (discard or
    // overwrite) and then with Reload.
    if (changedUnderEditsRef.current) {
      setNote('This file changed while you edited it. Choose in the editor (discard or overwrite), then Reload.')
      return false
    }
    const ok = await requestSave()
    setNote(ok ? '' : 'Not saved yet: the editor has not confirmed the save. Try again in a moment.')
    return ok
  }, [live, requestSave])

  // A save that keeps the document on show (Minimize, an overlay, a send):
  // a file changed under the edits is the banner's to resolve.
  const save = useCallback(async (timeoutMs?: number): Promise<boolean> => {
    if (!live) return true
    if (changedUnderEditsRef.current) return false
    return requestSave(timeoutMs)
  }, [live, requestSave])

  // The latest file: the same reset as the banner's Reload.
  const refresh = useCallback(() => {
    pendingReload.current = false
    reloadNow()
  }, [reloadNow])

  useImperativeHandle(ref, () => ({ leave, save, refresh }), [leave, save, refresh])

  // A frame on another origin than the editor's can never load (the browser
  // and Collabora's frame-ancestors check refuse it): explain the fix.
  const editorOwnOrigin = originOf(authConfig?.collabora_origin)
  const expectedOrigin = editorOwnOrigin || window.location.origin
  const previewOrigin = frame ? originOf(frame.url) : ''
  const originMismatch = !!authConfig && !!previewOrigin && previewOrigin !== expectedOrigin

  return (
    <div ref={hostRef} data-collabora-host="" className={`relative min-h-0 ${className ?? ''}`}>
      {((reloadAvailable && live) || note) && (
        <div className="absolute top-0 inset-x-0 z-20 flex flex-col">
          {(reloadAvailable && live) && (
            <div className="flex items-center justify-between gap-2 px-3 py-1.5 bg-amber-500/95 text-white text-xs">
              <span>This document changed. Reload to see the latest — your unsaved edits will be discarded.</span>
              <button onClick={() => { setNote(''); reloadNow() }} className="shrink-0 px-2 py-0.5 rounded-sm bg-white/20 hover:bg-white/30 font-medium">
                Reload
              </button>
            </div>
          )}
          {note && (
            <div role="status" className="flex items-center justify-between gap-2 border-t border-white/30 px-3 py-1.5 bg-amber-600/95 text-white text-xs">
              <span>{note}</span>
              <button onClick={() => setNote('')} className="shrink-0 px-2 py-0.5 rounded-sm bg-white/20 hover:bg-white/30 font-medium">
                OK
              </button>
            </div>
          )}
        </div>
      )}
      {originMismatch ? (
        <div className="h-full flex items-center justify-center p-6">
          <div className="max-w-md text-sm text-p-text-secondary space-y-2">
            <p className="font-medium text-p-text">Document preview can't load from this address.</p>
            {editorOwnOrigin ? (
              <>
                <p>
                  The editor is configured for <code className="text-xs">{expectedOrigin}</code>, but
                  this preview names <code className="text-xs">{previewOrigin}</code>.
                </p>
                <p>Reload the page to open it at the current address.</p>
              </>
            ) : (
              <>
                <p>
                  The preview is configured for <code className="text-xs">{previewOrigin}</code>, but
                  you're browsing from <code className="text-xs">{window.location.origin}</code>.
                </p>
                <p>
                  An administrator should set <code className="text-xs">DASHBOARD_PUBLIC_URL={window.location.origin}</code> in
                  the install's <code className="text-xs">.env</code> (or <code className="text-xs">config.env</code>) and
                  run <code className="text-xs">docker compose up -d</code> to apply it.
                </p>
              </>
            )}
          </div>
        </div>
      ) : (
        <>
          {(status === 'failed' || status === 'gone') && (
            <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-p-surface/80 text-sm text-p-text-secondary">
              <span>
                {status === 'gone'
                  ? (live ? 'This file is no longer there.' : 'This version is no longer available.')
                  : 'The document could not be opened.'}
              </span>
              {status === 'failed' && (
                <button onClick={refresh} className="px-2 py-1 rounded-md border border-p-border-light hover:bg-p-surface-hover text-xs">
                  Retry
                </button>
              )}
            </div>
          )}
          {status !== 'failed' && status !== 'gone' && !loaded && (
            <div className="absolute inset-0 z-10 flex items-center justify-center bg-p-surface/80">
              <div className="flex items-center gap-2 text-sm text-p-text-secondary">
                <div className="w-4 h-4 border-2 border-brand/30 border-t-brand rounded-full animate-spin" />
                Loading {filename || 'document'}…
              </div>
            </div>
          )}
          {frame && (
            <CollaboraFrame
              iframeRef={iframeRef}
              url={frame.url}
              accessToken={frame.token}
              accessTokenTtl={frame.ttl}
              frameKey={frameKey}
              className="w-full h-full border-0"
              onLoad={() => setLoaded(true)}
            />
          )}
        </>
      )}
    </div>
  )
})

export default DocumentFrame
