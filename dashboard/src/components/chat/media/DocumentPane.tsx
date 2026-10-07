import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { safeHref } from '../../../lib/safeUrl'
import FilePreviewPortal from '../../workspace/FilePreviewPortal'
import {
  registerPaneLeave, registerPaneSave, useDocumentPaneStore, useDocumentPushStore, type PushedDocument,
} from '../../../store/documentPaneStore'
import DocumentFrame, { type DocumentFrameHandle } from './DocumentFrame'
import DocumentVersionsMenu from './DocumentVersionsMenu'
import { extensionBadge, useDocumentTabs } from './documentTabs'

interface Props {
  chatId: string
  /** Docked: the right half of the chat page. Floating: the window over the
   * chat (a phone, a narrow window, a terminal chat). */
  placement: 'docked' | 'floating'
  /** On show: not minimized and no overlay in the chat's slot. */
  shown: boolean
}

const NO_PUSHED: Record<string, PushedDocument> = {}

// The frame's box is a manual popover: in the pane's flow while closed (the
// author style overrides the closed popover's `display: none`), in the top
// layer over the Fullscreen portal's body while open, so the one editor
// shows there whatever stacking context or transform an ancestor carries
// and never reloads. Without the popover API (iOS 16, Firefox before 125) a
// fixed box inside the floating pane would paint under the portal, so
// there is no Fullscreen.
const canPresent = (): boolean => typeof HTMLElement !== 'undefined' && 'showPopover' in HTMLElement.prototype
const BOX_RESET: CSSProperties = {
  display: 'block', inset: 'auto', margin: 0, padding: 0, border: 0, overflow: 'visible',
  background: 'transparent', color: 'inherit', maxWidth: 'none', maxHeight: 'none',
}
const IN_FLOW: CSSProperties = { ...BOX_RESET, position: 'relative', width: '100%', height: '100%' }

type Rect = { top: number; left: number; width: number; height: number }

function popoverOpen(el: HTMLElement): boolean {
  try { return el.matches(':popover-open') } catch { return false }
}

/**
 * The chat's document pane (FEATURES.md "Document pane"): one tab per
 * document pushed into the chat, newest first; Versions, Refresh, Download,
 * Fullscreen, Minimize and Close; the editor on the document on show, live
 * or a read-only version. A leave from a document with unsaved edits waits
 * for its save (`DocumentFrame.leave`); a refused save keeps it on show.
 */
export default function DocumentPane({ chatId, placement, shown }: Props) {
  const { tabs, active, pane, listingFailed } = useDocumentTabs(chatId)
  const pushed = useDocumentPushStore((s) => s.pushed[chatId] ?? NO_PUSHED)
  const setDirtyFile = useDocumentPushStore((s) => s.setDirty)
  const store = useDocumentPaneStore.getState
  const frameRef = useRef<DocumentFrameHandle | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [note, setNote] = useState('')
  const [liveGone, setLiveGone] = useState(false)
  // What the live file's token allows, per file: the Live row's words.
  const [allowed, setAllowed] = useState<{ fileId: string; permissions: 'edit' | 'view' | null } | null>(null)

  const fileId = active?.fileId ?? null
  const snapshotId = fileId ? pane.view[fileId] ?? null : null
  const versions = active?.doc?.versions ?? []
  const livePermissions = allowed && allowed.fileId === fileId ? allowed.permissions : null
  const onPermissions = useCallback((permissions: 'edit' | 'view' | null) => {
    if (fileId) setAllowed({ fileId, permissions })
  }, [fileId])
  const newestAvailable = versions.find((v) => v.available) ?? null

  // Fullscreen presents the pane's own frame over the portal's body (one
  // view of the document per device, the frame never reloads): the box is
  // sized to the slot and goes to the top layer. A pane put away leaves it.
  useEffect(() => { if (!shown) setFullscreen(false) }, [shown])
  const boxRef = useRef<HTMLDivElement | null>(null)
  const [slot, setSlot] = useState<HTMLDivElement | null>(null)
  const [slotRect, setSlotRect] = useState<Rect | null>(null)
  useLayoutEffect(() => {
    if (!fullscreen || !slot) { setSlotRect(null); return }
    const measure = () => {
      const r = slot.getBoundingClientRect()
      setSlotRect({ top: r.top, left: r.left, width: r.width, height: r.height })
    }
    measure()
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    ro?.observe(slot)
    window.addEventListener('resize', measure)
    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [fullscreen, slot])
  const presented = fullscreen && !!slotRect
  useLayoutEffect(() => {
    const box = boxRef.current
    if (!box || typeof box.showPopover !== 'function') return
    if (presented && !popoverOpen(box)) {
      try { box.showPopover() } catch { /* not in the document: nothing to present */ }
    } else if (!presented && popoverOpen(box)) {
      box.hidePopover()
    }
  }, [presented])
  const boxStyle: CSSProperties = presented && slotRect
    ? { ...BOX_RESET, position: 'fixed', zIndex: 60, ...slotRect }
    : IN_FLOW

  // The frames of a pane restored as minimized mount on its first show.
  const [everShown, setEverShown] = useState(shown)
  useEffect(() => { if (shown) setEverShown(true) }, [shown])

  // A note handed over by the change it explains (a version gone).
  const nextNote = useRef('')
  useEffect(() => {
    setLiveGone(false)
    setNote(nextNote.current)
    nextNote.current = ''
  }, [fileId, snapshotId])

  const filename = active?.filename ?? ''
  const onDirtyChange = useCallback((d: boolean) => {
    setDirty(d)
    setDirtyFile(chatId, d && fileId ? fileId : null, filename)
  }, [chatId, fileId, filename, setDirtyFile])

  useEffect(() => () => setDirtyFile(chatId, null), [chatId, setDirtyFile])

  // A live document with unsaved edits arms the page's leave prompt.
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const afterLeave = useCallback(async (act: () => void) => {
    const ok = (await frameRef.current?.leave()) ?? true
    if (ok) act()
  }, [])

  useEffect(() => registerPaneLeave(chatId, async () => (await frameRef.current?.leave()) ?? true), [chatId])
  useEffect(() => registerPaneSave(chatId, async (timeoutMs) => (await frameRef.current?.save(timeoutMs)) ?? true), [chatId])

  // A pane put away (Minimize, an overlay in the chat's slot) saves its
  // unsaved edits: the person may go on in the chat, and the agent reads
  // the stored file. The frame stays mounted, so the save completes.
  const wasShown = useRef(shown)
  useEffect(() => {
    if (wasShown.current && !shown) void frameRef.current?.save()
    wasShown.current = shown
  }, [shown])

  const order = tabs.map((t) => t.fileId)
  const select = (id: string) => {
    if (id === fileId) return
    void afterLeave(() => store().select(chatId, id))
  }
  const closeTab = (id: string) => {
    if (id !== fileId) { store().closeFile(chatId, id, order); return }
    void afterLeave(() => store().closeFile(chatId, id, order))
  }
  const pickVersion = (sid: string | null) => {
    if (!fileId || sid === snapshotId) return
    void afterLeave(() => store().showVersion(chatId, fileId, sid))
  }
  const closePane = () => { void afterLeave(() => store().close(chatId)) }
  const minimize = () => store().minimize(chatId)

  const downloadUrl = active?.downloadUrl
    ? `${active.downloadUrl}${active.downloadUrl.includes('?') ? '&' : '?'}fn=${encodeURIComponent(active.filename)}`
    : ''
  const notLive = !!snapshotId
  const version = notLive ? versions.find((v) => v.snapshot_id === snapshotId) : undefined
  const iconBtn = 'p-1.5 rounded-sm text-p-text-secondary transition-colors hover:bg-p-surface disabled:opacity-40'

  return (
    <div
      data-document-pane=""
      className={`flex h-full min-h-0 flex-col overflow-hidden bg-white dark:bg-p-surface ${placement === 'docked'
        ? 'rounded-xl border border-p-border-light shadow-xs'
        : 'rounded-xl border border-p-border-light shadow-2xl'}`}
    >
      <div className="flex items-center gap-1 border-b border-p-border-light bg-p-surface/50 px-2 py-1.5">
        <div role="tablist" aria-label="Documents" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {tabs.map((t) => {
            const on = t.fileId === fileId
            const fresh = pane.fresh.includes(t.fileId)
            return (
              <div
                key={t.fileId}
                className={`group flex max-w-[14rem] shrink-0 items-center gap-1 rounded-md border pl-2 pr-0.5 py-0.5 text-xs ${on
                  ? 'border-brand/40 bg-brand/10 text-p-text'
                  : 'border-transparent text-p-text-secondary hover:bg-p-surface'}`}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={on}
                  onClick={() => select(t.fileId)}
                  title={t.filename}
                  className="flex min-w-0 items-center gap-1.5"
                >
                  {fresh && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-brand" aria-label="new" />}
                  <span className="truncate">{t.filename || 'Document'}</span>
                  <span className="shrink-0 rounded-sm bg-brand/10 px-1 text-[9px] font-semibold text-brand">
                    {extensionBadge(t.filename)}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => closeTab(t.fileId)}
                  aria-label={`Close ${t.filename}`}
                  title="Close this document"
                  className="rounded-sm p-0.5 text-p-text-light hover:bg-black/10 hover:text-p-text dark:hover:bg-white/10"
                >
                  <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            )
          })}
        </div>
        <div className="flex shrink-0 items-center">
          {fileId && (
            <DocumentVersionsMenu versions={versions} current={snapshotId} failed={listingFailed}
              livePermissions={livePermissions} onPick={pickVersion} />
          )}
          <button type="button" onClick={() => void afterLeave(() => frameRef.current?.refresh())} disabled={!fileId}
            title="Refresh" aria-label="Refresh" className={iconBtn}>
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M20.016 4.66v4.993" />
            </svg>
          </button>
          {!notLive && downloadUrl && (
            <a href={safeHref(downloadUrl)} download={active?.filename} title="Download" aria-label="Download" className={iconBtn}>
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
            </a>
          )}
          {canPresent() && (
            <button type="button" onClick={() => setFullscreen(true)} disabled={!fileId}
              title="Fullscreen" aria-label="Fullscreen" className={iconBtn}>
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" />
              </svg>
            </button>
          )}
          <button type="button" onClick={minimize} title="Minimize" aria-label="Minimize" className={iconBtn}>
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <path strokeLinecap="round" d="M5 12h14" />
            </svg>
          </button>
          <button type="button" onClick={closePane} title="Close" aria-label="Close the document pane" className={iconBtn}>
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      </div>

      <div className="relative min-h-0 flex-1">
        {notLive && (
          <span className="pointer-events-none absolute bottom-2 right-2 z-30 rounded-full bg-amber-500/90 px-2 py-0.5 text-[10px] font-semibold text-white shadow-xs">
            Not live{version ? ` · version ${version.version}` : ''}
          </span>
        )}
        {(note || liveGone) && (
          <div className="absolute inset-x-0 top-0 z-30 flex items-center justify-between gap-2 bg-amber-500/95 px-3 py-1.5 text-xs text-white">
            <span>{note || 'This file is no longer there.'}</span>
            {liveGone && newestAvailable && fileId && (
              <button type="button" onClick={() => store().showVersion(chatId, fileId, newestAvailable.snapshot_id)}
                className="shrink-0 rounded-sm bg-white/20 px-2 py-0.5 font-medium hover:bg-white/30">
                Open the newest version
              </button>
            )}
          </div>
        )}
        {!fileId && (
          <div className="flex h-full items-center justify-center p-6 text-sm text-p-text-secondary">
            No document is open in this chat.
          </div>
        )}
        {fileId && everShown && (
          <div ref={boxRef} popover="manual" data-document-frame-box="" style={boxStyle}>
            <DocumentFrame
              key={`${fileId}:${snapshotId ?? 'live'}`}
              ref={frameRef}
              chatId={chatId}
              fileId={fileId}
              filename={active?.filename ?? ''}
              snapshotId={snapshotId}
              shown={shown}
              pushed={pushed[fileId]}
              onDirtyChange={onDirtyChange}
              onLiveGone={() => setLiveGone(true)}
              onPermissions={onPermissions}
              onVersionGone={() => {
                nextNote.current = 'That version is no longer available: showing the live file.'
                store().showVersion(chatId, fileId, null)
              }}
              className="h-full"
            />
          </div>
        )}
      </div>

      {fullscreen && fileId && (
        <FilePreviewPortal
          filename={active?.filename ?? 'Document'}
          onClose={() => setFullscreen(false)}
          downloadUrl={notLive ? undefined : downloadUrl || undefined}
          headerExtra={notLive ? (
            <span className="rounded-full bg-amber-500/90 px-2 py-0.5 text-[10px] font-semibold text-white">
              Not live{version ? ` · version ${version.version}` : ''}
            </span>
          ) : undefined}
          slotRef={setSlot}
        />
      )}
    </div>
  )
}
