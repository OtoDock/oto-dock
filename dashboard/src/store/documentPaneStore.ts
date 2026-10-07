import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

/**
 * The document pane's state per chat (FEATURES.md "Document pane"): which
 * documents are open, the one on show, Live or a version of it, and whether
 * the pane is shown, minimized or closed. Persisted per browser so a reload
 * shows the pane as it was left; it holds ids only, never a token or a URL.
 *
 * The pushed frames (the newest live `document_preview` per file, with the
 * WOPI token it carries) live in the second, in-memory store below: the only
 * place a pushed token is kept, for the 15-minute trust rule.
 */

export interface PaneChat {
  /** Shown: the right half (docked) or the window (floating). */
  open: boolean
  /** Parked as chips in the left stack. */
  minimized: boolean
  /** The file id on show. */
  active: string | null
  /** File ids whose tab the person closed (a later push reopens it). */
  closed: string[]
  /** Pushed files the person has not looked at yet (the tab's dot): a push
   * kept off show by unsaved edits, the other files of a turn that pushed
   * several, cleared when the tab is shown. */
  fresh: string[]
  /** File id → the snapshot id of the version on show; absent = Live. */
  view: Record<string, string>
  /** The newest push generation the pane has acted on. */
  seen: number
  /** When a push last put a file on show (epoch ms): a push of another
   * file in the same flush leaves that one unseen. */
  pushedAt?: number
  touched: number
}

/** The frames of one turn's flush arrive back to back: a push within this
 * of the one that put a file on show is the same turn's. */
export const SAME_FLUSH_MS = 1000

/** Chats kept per browser; the least recently touched go first. */
export const MAX_PANE_CHATS = 200

const EMPTY: PaneChat = {
  open: false, minimized: false, active: null, closed: [], fresh: [], view: {}, seen: 0, touched: 0,
}

interface PaneStore {
  /** The person the data belongs to; another person's sign-in drops it. */
  owner: string
  byChat: Record<string, PaneChat>
  setOwner: (sub: string) => void
  /** A push of `fileId` reached this chat: its tab reopens on Live and, when
   * the push is newer than what the pane acted on, the pane opens on it,
   * unless the document on show has unsaved edits (the tab gets a dot). A
   * file of the same turn that does not stay on show gets a dot too. */
  notePush: (chatId: string, fileId: string, generation: number, dirtyFile: string | null) => void
  /** The documents listing loaded: a chat seen for the first time starts
   * closed; a known chat whose newest push is newer than the one the pane
   * acted on (a push that landed while this page was away) opens on it, and
   * the listing's other files pushed since (`rest`) get a dot. A document
   * with unsaved edits on show (`dirtyFile`) stays on show: the newer file
   * gets a dot, as for a push. */
  ensure: (
    chatId: string, newest: { fileId: string; generation: number } | null,
    rest?: { fileId: string; generation: number }[], dirtyFile?: string | null,
  ) => void
  /** A card or a chip: open the pane on the file, Live or a version. */
  openFile: (chatId: string, fileId: string, snapshotId?: string | null) => void
  select: (chatId: string, fileId: string) => void
  /** Tab X: `order` is the tab strip's order, for the tab that takes over. */
  closeFile: (chatId: string, fileId: string, order: string[]) => void
  close: (chatId: string) => void
  minimize: (chatId: string) => void
  restore: (chatId: string, fileId?: string) => void
  showVersion: (chatId: string, fileId: string, snapshotId: string | null) => void
}

const without = (list: string[], id: string) => (list.includes(id) ? list.filter((x) => x !== id) : list)
const adding = (list: string[], id: string) => (list.includes(id) ? list : [...list, id])

function withChat(
  state: PaneStore, chatId: string, change: (cur: PaneChat) => PaneChat,
): Pick<PaneStore, 'byChat'> {
  const cur = state.byChat[chatId] ?? EMPTY
  const byChat = { ...state.byChat, [chatId]: { ...change(cur), touched: Date.now() } }
  const ids = Object.keys(byChat)
  if (ids.length > MAX_PANE_CHATS) {
    ids.sort((a, b) => byChat[a].touched - byChat[b].touched)
    for (const id of ids.slice(0, ids.length - MAX_PANE_CHATS)) delete byChat[id]
  }
  return { byChat }
}

function shownOn(cur: PaneChat, fileId: string, view?: string | null): PaneChat {
  const nextView = { ...cur.view }
  if (view) nextView[fileId] = view
  else delete nextView[fileId]
  return {
    ...cur, open: true, minimized: false, active: fileId,
    closed: without(cur.closed, fileId), fresh: without(cur.fresh, fileId), view: nextView,
  }
}

export const useDocumentPaneStore = create<PaneStore>()(
  persist(
    (set) => ({
      owner: '',
      byChat: {},
      setOwner: (sub) => set((s) => (s.owner === sub ? s : { owner: sub, byChat: {} })),
      notePush: (chatId, fileId, generation, dirtyFile) => set((s) => withChat(s, chatId, (cur) => {
        const view = { ...cur.view }
        delete view[fileId]
        const reopened = { ...cur, closed: without(cur.closed, fileId), view }
        const now = Date.now()
        // The file a push of this flush put on show (not looked at yet).
        const sameFlush = !!cur.active && cur.active !== fileId && now - (cur.pushedAt ?? 0) < SAME_FLUSH_MS
        if (generation <= cur.seen) {
          // An older push of the same flush arriving after a newer one.
          return sameFlush ? { ...reopened, fresh: adding(reopened.fresh, fileId) } : reopened
        }
        // A document with unsaved edits stays on show (its frame stays
        // mounted while minimized too): the push gets a dot.
        const keepShown = !!dirtyFile && dirtyFile !== fileId && cur.open
        if (keepShown) {
          return { ...reopened, seen: generation, minimized: false, fresh: adding(reopened.fresh, fileId) }
        }
        const left = sameFlush ? { ...reopened, fresh: adding(reopened.fresh, cur.active!) } : reopened
        return { ...shownOn(left, fileId), seen: generation, pushedAt: now }
      })),
      ensure: (chatId, newest, rest = [], dirtyFile = null) => set((s) => {
        const cur = s.byChat[chatId]
        const generation = newest?.generation ?? 0
        if (!cur) return withChat(s, chatId, () => ({ ...EMPTY, seen: generation }))
        if (!newest || generation <= cur.seen) return s
        return withChat(s, chatId, (c) => {
          const unseen = rest.filter((d) => d.generation > c.seen && d.fileId !== newest.fileId)
          const fresh = unseen.reduce((list, d) => adding(list, d.fileId), c.fresh)
          if (dirtyFile && dirtyFile !== newest.fileId && c.open) {
            // As notePush: the file's tab reopens on Live with its dot.
            const view = { ...c.view }
            delete view[newest.fileId]
            return {
              ...c, closed: without(c.closed, newest.fileId), view,
              fresh: adding(fresh, newest.fileId), seen: generation,
            }
          }
          return { ...shownOn({ ...c, fresh }, newest.fileId), seen: generation }
        })
      }),
      openFile: (chatId, fileId, snapshotId) => set((s) => withChat(s, chatId, (cur) => shownOn(cur, fileId, snapshotId))),
      select: (chatId, fileId) => set((s) => withChat(s, chatId, (cur) => ({
        ...cur, active: fileId, fresh: without(cur.fresh, fileId),
      }))),
      closeFile: (chatId, fileId, order) => set((s) => withChat(s, chatId, (cur) => {
        const closed = cur.closed.includes(fileId) ? cur.closed : [...cur.closed, fileId]
        if (cur.active !== fileId) return { ...cur, closed, fresh: without(cur.fresh, fileId) }
        const remaining = order.filter((id) => id !== fileId && !closed.includes(id))
        const at = Math.max(0, order.indexOf(fileId))
        const next = remaining[Math.min(at, remaining.length - 1)] ?? null
        return {
          ...cur, closed, fresh: without(cur.fresh, fileId), active: next,
          open: next ? cur.open : false, minimized: next ? cur.minimized : false,
        }
      })),
      close: (chatId) => set((s) => withChat(s, chatId, (cur) => ({ ...cur, open: false, minimized: false }))),
      minimize: (chatId) => set((s) => withChat(s, chatId, (cur) => ({ ...cur, minimized: true }))),
      restore: (chatId, fileId) => set((s) => withChat(s, chatId, (cur) => ({
        ...cur, open: true, minimized: false, active: fileId ?? cur.active,
        fresh: fileId ? without(cur.fresh, fileId) : cur.fresh,
      }))),
      showVersion: (chatId, fileId, snapshotId) => set((s) => withChat(s, chatId, (cur) => {
        const view = { ...cur.view }
        if (snapshotId) view[fileId] = snapshotId
        else delete view[fileId]
        return { ...cur, view }
      })),
    }),
    {
      name: 'oto-dock-document-pane',
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ owner: s.owner, byChat: s.byChat }),
    },
  ),
)

// Two tabs of one browser share the key: take the other tab's writes as
// they land, so neither overwrites the other's closes and minimizes.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key !== 'oto-dock-document-pane') return
    // A chat whose document has unsaved edits in this tab keeps its state
    // here: the other tab's change would swap the document under them.
    const dirty = useDocumentPushStore.getState().dirty
    const byChat = useDocumentPaneStore.getState().byChat
    const keep = Object.keys(dirty).filter((id) => dirty[id] && byChat[id]).map((id) => [id, byChat[id]] as const)
    void Promise.resolve(useDocumentPaneStore.persist.rehydrate()).then(() => {
      if (keep.length) useDocumentPaneStore.setState((s) => ({ byChat: { ...s.byChat, ...Object.fromEntries(keep) } }))
    })
  })
}

const paneLeaves = new Map<string, () => Promise<boolean>>()

/** The mounted pane of a chat registers how it leaves its document on show
 * (unsaved edits saved first, or the leave refused); returns the unregister. */
export function registerPaneLeave(chatId: string, leave: () => Promise<boolean>): () => void {
  paneLeaves.set(chatId, leave)
  return () => { if (paneLeaves.get(chatId) === leave) paneLeaves.delete(chatId) }
}

/** Leave the document on show the way the pane's own buttons do, for the
 * changes made from outside it: a card, a chip, Esc on the floating window.
 * True when there is nothing to keep (no pane mounted, nothing unsaved). */
export function leaveDocumentPane(chatId: string): Promise<boolean> {
  return paneLeaves.get(chatId)?.() ?? Promise.resolve(true)
}

type PaneSave = (timeoutMs?: number) => Promise<boolean>

const paneSaves = new Map<string, PaneSave>()

/** The mounted pane of a chat registers how it saves its document's unsaved
 * edits without leaving it, and gets the unregister back. */
export function registerPaneSave(chatId: string, save: PaneSave): () => void {
  paneSaves.set(chatId, save)
  return () => { if (paneSaves.get(chatId) === save) paneSaves.delete(chatId) }
}

/** Save the document on show before something the agent will read (a
 * send): true once the editor confirms the save (within `timeoutMs`, the
 * pane's own wait by default), or when there is nothing to save (no pane
 * mounted, nothing unsaved). */
export function saveDocumentPane(chatId: string, timeoutMs?: number): Promise<boolean> {
  return paneSaves.get(chatId)?.(timeoutMs) ?? Promise.resolve(true)
}

export function paneChat(chatId: string | null | undefined): PaneChat {
  return (chatId && useDocumentPaneStore.getState().byChat[chatId]) || EMPTY
}

/** A document pushed live: the newest frame of a file in a chat. */
export interface PushedDocument {
  fileId: string
  filename: string
  wopiUrl: string
  accessToken?: string
  accessTokenTtl?: number
  downloadUrl: string
  snapshotId?: string
  version?: number
  /** The push time, epoch ms. */
  generation: number
}

/** A file the pane knows of before the listing does (a card the person
 * clicked, a push this turn): enough to draw its tab. */
export interface KnownDocument {
  fileId: string
  filename: string
  downloadUrl: string
  generation: number
}

interface PushStore {
  pushed: Record<string, Record<string, PushedDocument>>
  known: Record<string, Record<string, KnownDocument>>
  /** The file whose live document on show has unsaved edits, per chat. */
  dirty: Record<string, string | null>
  /** That file's name, for the composer's line. */
  dirtyNames: Record<string, string>
  /** Opened or touched by the person since the pane last opened by itself:
   * the floating window then takes Esc. */
  attended: Record<string, boolean>
  setDirty: (chatId: string, fileId: string | null, filename?: string) => void
  setAttended: (chatId: string, attended: boolean) => void
  noteKnown: (chatId: string, doc: KnownDocument) => void
}

export const useDocumentPushStore = create<PushStore>()((set) => ({
  pushed: {},
  known: {},
  dirty: {},
  dirtyNames: {},
  attended: {},
  setDirty: (chatId, fileId, filename = '') => set((s) => (
    s.dirty[chatId] === fileId && (s.dirtyNames[chatId] ?? '') === (fileId ? filename : '')
      ? s
      : {
        dirty: { ...s.dirty, [chatId]: fileId },
        dirtyNames: { ...s.dirtyNames, [chatId]: fileId ? filename : '' },
      }
  )),
  setAttended: (chatId, attended) => set((s) => (
    s.attended[chatId] === attended ? s : { attended: { ...s.attended, [chatId]: attended } }
  )),
  noteKnown: (chatId, doc) => set((s) => {
    const cur = s.known[chatId]?.[doc.fileId]
    if (cur && cur.generation >= doc.generation) return s
    return { known: { ...s.known, [chatId]: { ...(s.known[chatId] ?? {}), [doc.fileId]: doc } } }
  }),
}))

/** A live `document_preview` frame (a headless turn's flush, or a terminal
 * chat's push): keep it in memory and let the pane act on it. */
export function notePushedDocument(chatId: string, doc: PushedDocument): void {
  if (!chatId || !doc.fileId) return
  useDocumentPushStore.setState((s) => {
    const cur = s.pushed[chatId]?.[doc.fileId]
    if (cur && cur.generation > doc.generation) return s
    return { pushed: { ...s.pushed, [chatId]: { ...(s.pushed[chatId] ?? {}), [doc.fileId]: doc } } }
  })
  const dirty = useDocumentPushStore.getState().dirty[chatId] ?? null
  const before = paneChat(chatId)
  useDocumentPaneStore.getState().notePush(chatId, doc.fileId, doc.generation, dirty)
  const after = paneChat(chatId)
  // A window the push opened or switched is not the person's yet (Esc).
  if (after.open !== before.open || after.minimized !== before.minimized || after.active !== before.active) {
    useDocumentPushStore.getState().setAttended(chatId, false)
  }
}

/** A pushed frame from the wire (`document_preview`, or a terminal chat's
 * `pty_artifact` event). */
export function pushedDocumentFromFrame(evt: any): PushedDocument | null {
  if (!evt?.file_id) return null
  return {
    fileId: evt.file_id,
    filename: evt.filename || '',
    wopiUrl: evt.wopi_url || '',
    accessToken: evt.access_token || undefined,
    accessTokenTtl: evt.access_token_ttl || undefined,
    downloadUrl: evt.download_url || '',
    snapshotId: evt.snapshot_id || undefined,
    version: evt.version || undefined,
    generation: typeof evt.generation === 'number' && evt.generation > 0 ? evt.generation : Date.now(),
  }
}
