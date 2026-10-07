/**
 * ChatDocumentPane — where the chat's document pane lives on the page
 * (FEATURES.md "Document pane"): docked as the right half of the chat page
 * (a fine pointer, a window of at least 1024 px, a chat that is not a
 * terminal chat), or as one window over the chat's slot otherwise. The page
 * mounts it in exactly one of the two places (`useDocumentPaneForm`). It
 * also keeps the pane's state in step with the chat's documents listing.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import DocumentPane from '../../../components/chat/media/DocumentPane'
import { useDocumentTabs } from '../../../components/chat/media/documentTabs'
import { chatDocumentsKey } from '../../../api/documents'
import { leaveDocumentPane, useDocumentPaneStore, useDocumentPushStore } from '../../../store/documentPaneStore'
import { pushEscHandler } from '../../../lib/escStack'

export type DocumentPaneForm = 'docked' | 'floating'

/** Docked only with room for two halves and a mouse; tablets and phones get
 * the window. Read on the first render, so a phone never paints a docked
 * pane first. */
function formNow(terminal: boolean): DocumentPaneForm {
  if (terminal || typeof window === 'undefined') return 'floating'
  const coarse = window.matchMedia?.('(pointer: coarse)').matches ?? false
  return !coarse && window.innerWidth >= 1024 ? 'docked' : 'floating'
}

export function useDocumentPaneForm(terminal: boolean, chatId: string | null): DocumentPaneForm {
  // A change of form remounts the editor: a document with unsaved edits
  // holds the form it has until it is saved.
  const dirty = useDocumentPushStore((s) => !!(chatId && s.dirty[chatId]))
  const [form, setForm] = useState<DocumentPaneForm>(() => formNow(terminal))
  useEffect(() => {
    if (dirty) return
    setForm(formNow(terminal))
    let t: ReturnType<typeof setTimeout> | undefined
    const onResize = () => {
      clearTimeout(t)
      t = setTimeout(() => setForm(formNow(terminal)), 100)
    }
    window.addEventListener('resize', onResize)
    return () => {
      clearTimeout(t)
      window.removeEventListener('resize', onResize)
    }
  }, [terminal, dirty])
  return form
}

/** The minimized pane's chips for the left stack: one per open document,
 * at most four, then a chip that restores the pane. */
export function useDocumentChips(chatId: string | null, hidden: boolean) {
  const { tabs, pane, active } = useDocumentTabs(chatId)
  if (!chatId || hidden || !pane.open || !pane.minimized) return null
  const store = useDocumentPaneStore.getState()
  const restore = (fileId?: string) => {
    useDocumentPushStore.getState().setAttended(chatId, true)
    if (!fileId || fileId === active?.fileId) { store.restore(chatId, fileId); return }
    void leaveDocumentPane(chatId).then((ok) => store.restore(chatId, ok ? fileId : undefined))
  }
  return {
    chips: tabs.slice(0, 4).map((t) => ({ fileId: t.fileId, title: t.filename || 'Document' })),
    more: Math.max(0, tabs.length - 4),
    onRestore: restore,
    onClose: (fileId: string) => {
      const close = () => store.closeFile(chatId, fileId, tabs.map((t) => t.fileId))
      if (fileId !== active?.fileId) { close(); return }
      // The document on show may hold unsaved edits: a refused leave brings
      // the pane back with its note.
      void leaveDocumentPane(chatId).then((ok) => (ok ? close() : restore(fileId)))
    },
  }
}

interface Props {
  chatId: string
  placement: DocumentPaneForm
  /** The workspace, apps or Dock overlay fills the slot: the pane hides. */
  hidden: boolean
  userSub: string
}

const CLOSE_MS = 300

export default function ChatDocumentPane({ chatId, placement, hidden, userSub }: Props) {
  const { pane, docs, all } = useDocumentTabs(chatId)
  const pushed = useDocumentPushStore((s) => s.pushed[chatId])
  const attended = useDocumentPushStore((s) => !!s.attended[chatId])
  const queryClient = useQueryClient()
  // Stored state of another person (the backstop of the sign-in's reset):
  // nothing renders, so no editor loads and no token is asked for, until
  // the effect below drops it.
  const ownerMismatch = useDocumentPaneStore((s) => !!userSub && s.owner !== userSub)

  useEffect(() => {
    if (userSub) useDocumentPaneStore.getState().setOwner(userSub)
  }, [userSub])

  // Back on a chat: a push that landed while the page was away is in a
  // fresher listing than the cached one.
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: chatDocumentsKey(chatId) })
  }, [chatId, queryClient])

  // Each listing result: first sight of the chat, or a push that landed
  // while this page was away (never over a document with unsaved edits).
  useEffect(() => {
    if (!docs) return
    const [newest, ...rest] = docs.map((d) => ({ fileId: d.file_id, generation: d.generation }))
    const dirtyFile = useDocumentPushStore.getState().dirty[chatId] ?? null
    useDocumentPaneStore.getState().ensure(chatId, newest ?? null, rest, dirtyFile)
  }, [chatId, docs])

  // The turn's rows persist after the frames: refetch the listing until it
  // holds a version for every push (0.5, 1.5 and 4 s), a push whose copy
  // could not be made included.
  const missing = Object.values(pushed ?? {}).filter((p) =>
    !all.some((t) => t.doc?.versions.some((v) => v.generation === p.generation))).length
  useEffect(() => {
    if (!missing) return
    const timers = [500, 1500, 4000].map((ms) => setTimeout(
      () => void queryClient.invalidateQueries({ queryKey: chatDocumentsKey(chatId) }), ms))
    return () => timers.forEach(clearTimeout)
  }, [missing, chatId, queryClient])

  const shown = pane.open && !pane.minimized && !hidden
  // The pane's content stays mounted while it is open (minimized, behind an
  // overlay) so an open editor keeps its edits; a close unmounts it once the
  // slide is over.
  const [mounted, setMounted] = useState(pane.open && !ownerMismatch)
  useEffect(() => {
    if (ownerMismatch) { setMounted(false); return }
    if (pane.open) { setMounted(true); return }
    const t = setTimeout(() => setMounted(false), CLOSE_MS)
    return () => clearTimeout(t)
  }, [pane.open, ownerMismatch])

  if (ownerMismatch) return null
  if (placement === 'docked') return <Docked chatId={chatId} shown={shown} mounted={mounted} />
  return <Floating chatId={chatId} shown={shown} mounted={mounted} attended={attended} />
}

function Docked({ chatId, shown, mounted }: { chatId: string; shown: boolean; mounted: boolean }) {
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const [rowWidth, setRowWidth] = useState(0)
  // The slide is enabled after the first paint: a reload restores an open
  // pane in place.
  const [animate, setAnimate] = useState(false)
  useEffect(() => {
    const raf = requestAnimationFrame(() => setAnimate(true))
    return () => cancelAnimationFrame(raf)
  }, [])
  // The content keeps the half's width while the wrapper's width slides, so
  // the editor does not reflow under the animation.
  useLayoutEffect(() => {
    const row = wrapperRef.current?.parentElement
    if (!row) return
    const measure = () => setRowWidth(row.clientWidth)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(row)
    return () => ro.disconnect()
  }, [])

  return (
    <div
      ref={wrapperRef}
      data-document-pane-dock=""
      className={`h-full shrink-0 overflow-clip ${shown ? 'w-1/2' : 'w-0'} ${animate
        ? 'transition-[width] duration-300 ease-in-out motion-reduce:transition-none' : ''}`}
    >
      <div
        className={`h-full pb-3 pr-3 pt-12 ${shown ? '' : 'invisible'}`}
        style={{ width: rowWidth ? rowWidth / 2 : undefined }}
        inert={!shown}
      >
        {mounted && <DocumentPane chatId={chatId} placement="docked" shown={shown} />}
      </div>
    </div>
  )
}

function Floating({ chatId, shown, mounted, attended }: {
  chatId: string; shown: boolean; mounted: boolean; attended: boolean
}) {
  const overlayRef = useRef<HTMLDivElement | null>(null)
  const [maxHeight, setMaxHeight] = useState<number | undefined>(undefined)
  const [narrow, setNarrow] = useState(() => typeof window !== 'undefined' && window.innerWidth < 640)

  // The keyboard of a phone shrinks the visual viewport only: cap the window
  // at it so its header stays in sight.
  useEffect(() => {
    const vv = window.visualViewport
    const fit = () => {
      setNarrow(window.innerWidth < 640)
      const top = overlayRef.current?.getBoundingClientRect().top ?? 0
      if (vv) setMaxHeight(Math.max(160, vv.height + vv.offsetTop - top - 16))
    }
    fit()
    vv?.addEventListener('resize', fit)
    vv?.addEventListener('scroll', fit)
    window.addEventListener('resize', fit)
    return () => {
      vv?.removeEventListener('resize', fit)
      vv?.removeEventListener('scroll', fit)
      window.removeEventListener('resize', fit)
    }
  }, [])

  // Esc closes the window as the topmost layer, once the person has opened
  // or touched it: a pane that opened by itself never takes an Esc meant for
  // something else (the find bar).
  useEffect(() => {
    if (!shown || !attended) return
    return pushEscHandler(() => {
      void leaveDocumentPane(chatId).then((ok) => { if (ok) useDocumentPaneStore.getState().close(chatId) })
    })
  }, [shown, attended, chatId])

  return (
    <div ref={overlayRef} className="pointer-events-none absolute inset-x-0 top-12 bottom-0 z-[15] overflow-clip">
      <div
        className={`pointer-events-auto absolute flex flex-col ${narrow
          ? 'inset-x-2 top-2' : 'right-3 top-3 w-[min(40rem,calc(100%-1.5rem))]'} ${shown ? '' : 'invisible'}`}
        style={{ height: 'min(80%, 44rem)', maxHeight }}
        inert={!shown}
        onPointerDown={() => useDocumentPushStore.getState().setAttended(chatId, true)}
      >
        {mounted && <DocumentPane chatId={chatId} placement="floating" shown={shown} />}
      </div>
    </div>
  )
}
