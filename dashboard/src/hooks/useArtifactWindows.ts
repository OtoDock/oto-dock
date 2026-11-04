import { useCallback, useEffect, useRef, useState, useLayoutEffect } from 'react'
import type { MessageBlock } from '@/components/chat/types'
import { isNarrowViewport } from '@/components/settings/useNarrow'
import { useDocumentPaneStore, useDocumentPushStore, type PaneChat } from '@/store/documentPaneStore'
import { eventToBlock } from '@/lib/messageBlocks'
import { replayableDisplayEvents } from '@/lib/displayReplay'
import { loadDismissedPips, recordDismissedPip } from '@/lib/pipDismissals'
import { fetchChatPage } from '@/api/chats'
import type { InteractiveWs } from './useInteractiveTerminal'
import { keepDock, keptDock, nextWindowId } from '@/store/artifactDockStore'
import { WIRE } from '../api/wireEvents'
import { evictedBy, identityKey, isArtifactBlock } from '../lib/kinds/artifact'

/**
 * Owns the floating display/file-tools artifact windows for an interactive CLI
 * session (a document push goes to the chat's document pane instead, through
 * `onDocument`). Subscribes to `pty_artifact` frames (the
 * drainer forwards display-mcp/file-tools artifacts there — there is no inline
 * message list in interactive), converts each to a renderable MessageBlock via
 * the shared `eventToBlock` mapper, and maintains the open-window list. The
 * placeholder/dedup rules are the artifact kinds' facts (lib/kinds/artifact.ts,
 * the mirror of the pump's table): a placeholder kind would open a window
 * (`eventToBlock` maps none, so none opens here), the kind that `evicts` it
 * removes the latest one (the real artifact replaces it, a failure only
 * removes), a kind with an `identity` replaces the window
 * with the same key in place (a preview by file_id, a ui page by path),
 * every other block kind opens a new window.
 *
 * REPLAY-ON-OPEN: the drainer also persists every final artifact as a chat row
 * and stamps its row id on the frame (`db_message_id`). When the terminal
 * (re)attaches, the hook fetches the newest history page and re-opens the
 * FINAL TURN's popups (rule + cap in `lib/displayReplay`), so a user who was
 * away — or who reloaded — still gets the displays. The row id is the dedupe
 * key between the seed and any frames that raced in live, and the X-dismiss
 * memory key (`lib/pipDismissals`, per-browser localStorage).
 *
 * KEPT PER CHAT WHILE THE PAGE LIVES (`store/artifactDockStore`): a switch to
 * another chat and back finds the windows as they were left, open, minimized
 * or closed, the chips in their order; the seed then opens only what a turn
 * made while the chat was not on show.
 *
 * ONE OPEN WINDOW ON A PHONE (below the chat page's `md` width): a live new
 * window or a restore minimizes every other open window, and puts a shown
 * document pane away; the pane coming on show puts the open windows away; a
 * replay opens only its newest window, none when something is already open.
 * The rule is the pure updates below, so it is tested without a view.
 *
 * Visual placement + dragging lives in the rendering component; this hook holds
 * only data (windows, which are minimized, the dock's chip order) so it stays
 * testable + view-free.
 */

export interface ArtifactWindow {
  id: number
  block: MessageBlock
  title: string
  /** chat_messages row id of the persisted artifact event (final types only —
   * placeholders never persist). Stable across reloads; keys replay dedupe +
   * dismissal memory. */
  dbId?: number
}

export function titleFor(block: MessageBlock): string {
  switch (block.type) {
    case 'images': {
      const cap = block.images.find((i) => i.caption)?.caption
      return cap || (block.images.length > 1 ? `${block.images.length} images` : 'Image')
    }
    case 'image_generating':
      return 'Generating image…'
    case 'media_processing':
      return block.mediaKind === 'audio' ? 'Preparing audio…' : 'Preparing video…'
    case 'video':
      return block.title || block.caption || 'Video'
    case 'audio':
      return block.title || block.caption || 'Audio'
    case 'url':
      return block.title || block.url || 'Link'
    case 'file':
      return block.filename || 'File'
    case 'ui':
      return block.title || 'UI artifact'
    case 'setting':
      return block.label
    case 'card':
      return block.title
    case 'list':
      return block.title
    default:
      return 'Artifact'
  }
}

/** The dock's key of a window's chip; a document chip is `doc:<fileId>`,
 * the chip for the documents past four `doc:more`. */
export const windowChipKey = (id: number) => `win:${id}`
const WINDOW_KEY = 'win:'

/** The dock's chips in the order a drag left, the others after them in
 * their arrival order (a key of a chip not on show is ignored). */
export function arrangeChips<T extends { key: string }>(chips: T[], order: string[] | undefined): T[] {
  if (!order?.length) return chips
  const rank = new Map(order.map((k, i) => [k, i]))
  const known = chips.filter((c) => rank.has(c.key)).sort((a, b) => rank.get(a.key)! - rank.get(b.key)!)
  return [...known, ...chips.filter((c) => !rank.has(c.key))]
}

/** The windows, which are minimized, the dock's chip order and the last
 * window a phone opened (a counter, so the same window opened twice still
 * acts), in one state: the phone's rule is one pure update. */
export interface DockState {
  windows: ArtifactWindow[]
  minimized: Set<number>
  order: string[]
  opened: { id: number; seq: number } | null
}

/** What the phone's rule reads at the moment an event arrives. */
export interface PhoneFacts {
  narrow: boolean
  /** The chat's document pane is open and not minimized. */
  paneShown: boolean
  /** ...and its document on show holds unsaved edits. */
  paneEdited: boolean
}

export const NOT_A_PHONE: PhoneFacts = { narrow: false, paneShown: false, paneEdited: false }
const EMPTY_DOCK: DockState = { windows: [], minimized: new Set(), order: [], opened: null }

/** The dock of one chat: every update the hook makes names the chat it was
 * made for, so a frame or a replay of the chat the page just left changes
 * nothing. */
type HeldDock = DockState & { chat: string }

function removeLatestOfType(wins: ArtifactWindow[], type: string): ArtifactWindow[] {
  for (let i = wins.length - 1; i >= 0; i--) {
    if (wins[i].block.type === type) {
      const copy = wins.slice()
      copy.splice(i, 1)
      return copy
    }
  }
  return wins
}

/** Insert/replace one artifact block in the window list: skip when its row id
 * is already showing (seed ⇄ live race), replace in place on the kind's
 * identity (a preview by file_id, a ui page by path — the PiP twin of the
 * inline chat's supersede-to-chip rule), else append a new window. */
function upsertWindow(
  wins: ArtifactWindow[],
  block: MessageBlock,
  dbId: number | undefined,
  allocId: () => number,
): ArtifactWindow[] {
  if (dbId !== undefined && wins.some((w) => w.dbId === dbId)) return wins
  // In-place replace, but never DOWNGRADE: a seed row must not overwrite a
  // window a newer live frame already updated (row ids are monotonic).
  const replaceAt = (idx: number): ArtifactWindow[] => {
    const cur = wins[idx].dbId
    if (cur !== undefined && dbId !== undefined && cur > dbId) return wins
    const copy = wins.slice()
    copy[idx] = { ...copy[idx], block, title: titleFor(block), dbId }
    return copy
  }
  const key = identityKey(block)
  if (key) {
    const idx = wins.findIndex((w) => w.block.type === block.type && identityKey(w.block) === key)
    if (idx >= 0) return replaceAt(idx)
  }
  return [...wins, { id: allocId(), block, title: titleFor(block), dbId }]
}

/** The state over a new window list: the minimized set and the chips' order
 * keep only the windows still there. */
function settle(d: DockState, windows: ArtifactWindow[], minimized: Set<number>, opened: DockState['opened']): DockState {
  const ids = new Set(windows.map((w) => w.id))
  const kept = new Set([...minimized].filter((id) => ids.has(id)))
  const order = d.order.filter((k) => !k.startsWith(WINDOW_KEY) || ids.has(Number(k.slice(WINDOW_KEY.length))))
  return { windows, minimized: kept, order: order.length === d.order.length ? d.order : order, opened }
}

const nextOpened = (d: DockState, id: number) => ({ id, seq: (d.opened?.seq ?? 0) + 1 })

/** The dock a chat left on this page, as it comes back: a phone's last
 * opening does not act again (it would put a shown pane away). */
function restoredDock(chat: string): HeldDock {
  const kept = keptDock(chat)
  return kept ? { ...kept, opened: null, chat } : { ...EMPTY_DOCK, chat }
}

/** A live frame: `evicts` drops the latest window of that placeholder kind
 * (a removal frame carries no block), then the block upserts. On a phone a
 * new window minimizes every other one, unless the pane holds unsaved edits
 * on show, when the new window waits in the dock instead. */
export function liveArtifact(
  d: DockState, block: MessageBlock | null, dbId: number | undefined, evicts: string | null,
  allocId: () => number, phone: PhoneFacts,
): DockState {
  const wins = evicts ? removeLatestOfType(d.windows, evicts) : d.windows
  if (!block) return settle(d, wins, d.minimized, d.opened)
  const next = upsertWindow(wins, block, dbId, allocId)
  const added = next.length > wins.length ? next[next.length - 1] : null
  if (!added) return settle(d, next, d.minimized, d.opened)
  const minimized = new Set(d.minimized)
  if (phone.narrow && phone.paneEdited) {
    minimized.add(added.id)
    return settle(d, next, minimized, d.opened)
  }
  if (!phone.narrow) return settle(d, next, minimized, d.opened)
  for (const w of next) if (w.id !== added.id) minimized.add(w.id)
  return settle(d, next, minimized, nextOpened(d, added.id))
}

/** The replay seed: on a phone only the newest replayed window opens, and
 * none when a window or the pane is already open. */
export function replayArtifacts(
  d: DockState, replay: { block: MessageBlock; dbId: number }[], allocId: () => number, phone: PhoneFacts,
): DockState {
  let wins = d.windows
  for (const r of replay) wins = upsertWindow(wins, r.block, r.dbId, allocId)
  const before = new Set(d.windows.map((w) => w.id))
  const added = wins.filter((w) => !before.has(w.id))
  if (!phone.narrow || !added.length) return settle(d, wins, d.minimized, d.opened)
  const minimized = new Set(d.minimized)
  const somethingOpen = phone.paneShown || d.windows.some((w) => !d.minimized.has(w.id))
  const keep = somethingOpen ? null : added[added.length - 1].id
  for (const w of added) if (w.id !== keep) minimized.add(w.id)
  return settle(d, wins, minimized, d.opened)
}

/** A chip opens its window; on a phone the other windows go to the dock. */
export function restoreWindow(d: DockState, id: number, phone: PhoneFacts): DockState {
  if (!d.minimized.has(id)) return d
  const minimized = new Set(d.minimized)
  minimized.delete(id)
  if (!phone.narrow) return { ...d, minimized }
  for (const w of d.windows) if (w.id !== id) minimized.add(w.id)
  return { ...d, minimized, opened: nextOpened(d, id) }
}

/** The document pane came on show on a phone: every open window goes to the dock. */
export function minimizeOpenWindows(d: DockState): DockState {
  if (d.windows.every((w) => d.minimized.has(w.id))) return d
  return { ...d, minimized: new Set([...d.minimized, ...d.windows.map((w) => w.id)]) }
}

const paneShownIn = (pane: PaneChat | undefined): boolean => !!pane && pane.open && !pane.minimized

export function useArtifactWindows(
  ws: InteractiveWs,
  chatId: string,
  /** A document push: the chat's document pane takes it, not a window. */
  onDocument?: (event: unknown) => void,
) {
  const { subscribe } = ws
  const [dock, setDock] = useState<HeldDock>(() => restoredDock(chatId))
  const dockRef = useRef(dock)
  const onDocumentRef = useRef(onDocument)
  useLayoutEffect(() => {
    dockRef.current = dock
    onDocumentRef.current = onDocument
  })

  // Read when an event arrives (never inside an update, which may run twice).
  const phoneNow = useCallback((): PhoneFacts => {
    if (!isNarrowViewport()) return NOT_A_PHONE
    const paneShown = !!chatId && paneShownIn(useDocumentPaneStore.getState().byChat[chatId])
    const paneEdited = paneShown && !!useDocumentPushStore.getState().dirty[chatId]
    return { narrow: true, paneShown, paneEdited }
  }, [chatId])

  /** Change the dock of `chat`; another chat's dock is left as it is. */
  const updateFor = useCallback((chat: string, change: (d: DockState) => DockState) => {
    setDock((d) => {
      if (d.chat !== chat) return d
      const next = change(d)
      return next === d ? d : { ...next, chat }
    })
  }, [])

  const close = useCallback((id: number) => {
    // X = permanent for this browser: remember the persisted row id so the
    // replay-on-open seed never re-opens this popup (see lib/pipDismissals —
    // cross-device re-show is the documented v1 limit).
    const dbId = dockRef.current.windows.find((w) => w.id === id)?.dbId
    if (dbId !== undefined && chatId) recordDismissedPip(chatId, dbId)
    updateFor(chatId, (d) => settle(d, d.windows.filter((w) => w.id !== id), d.minimized, d.opened))
  }, [chatId, updateFor])

  const minimize = useCallback((id: number) => {
    updateFor(chatId, (d) => (d.minimized.has(id) ? d : { ...d, minimized: new Set(d.minimized).add(id) }))
  }, [chatId, updateFor])

  const restore = useCallback((id: number) => {
    const phone = phoneNow()
    updateFor(chatId, (d) => restoreWindow(d, id, phone))
  }, [chatId, phoneNow, updateFor])

  /** The dock's chips in the order a drag left them (window and document keys). */
  const setDockOrder = useCallback((order: string[]) => {
    updateFor(chatId, (d) => ({ ...d, order }))
  }, [chatId, updateFor])

  // Another chat, or leaving the interactive view (an empty id): the dock it
  // left on this page (in render, on the change), else none; the replay seed
  // below opens the final turn's persisted artifacts it does not hold.
  if (dock.chat !== chatId) setDock(restoredDock(chatId))

  useEffect(() => { keepDock(dock.chat, dock) }, [dock])

  // A window a phone opened puts a shown document pane away...
  const opened = dock.opened
  useEffect(() => {
    if (!opened || !chatId) return
    const store = useDocumentPaneStore.getState()
    if (paneShownIn(store.byChat[chatId])) store.minimize(chatId)
  }, [opened, chatId])

  // ...and the pane coming on show on a phone puts the open windows away (its
  // going away changes nothing, so the two never answer each other).
  useEffect(() => {
    if (!chatId) return
    let was = paneShownIn(useDocumentPaneStore.getState().byChat[chatId])
    return useDocumentPaneStore.subscribe((s) => {
      const now = paneShownIn(s.byChat[chatId])
      if (now && !was && isNarrowViewport()) updateFor(chatId, minimizeOpenWindows)
      was = now
    })
  }, [chatId, updateFor])

  useEffect(() => {
    // An empty chatId (not interactive) does not subscribe.
    if (!chatId) return

    const unsub = subscribe(WIRE.PTY_ARTIFACT, (msg) => {
      if (msg.type !== WIRE.PTY_ARTIFACT) return
      if (msg.chat_id && msg.chat_id !== chatId) return
      const event = msg.event
      const t = event?.type
      if (!t) return
      if (t === WIRE.DOCUMENT_PREVIEW) {
        onDocumentRef.current?.(event)
        return
      }
      const phone = phoneNow()
      const dbId = typeof event.db_message_id === 'number' ? event.db_message_id : undefined
      // A card opens a window of its own, like a final artifact.
      if (t === WIRE.SETTINGS_CARD || t === WIRE.ACTION_CARD || t === WIRE.LIST_CARD) {
        const block = eventToBlock(event)
        if (!block) return
        updateFor(chatId, (d) => liveArtifact(d, block, dbId, null, nextWindowId, phone))
        return
      }
      const evicts = evictedBy(t)
      // A removal kind carries no renderable block — it only drops the
      // latest window of the placeholder it evicts.
      if (!isArtifactBlock(t)) {
        if (evicts) updateFor(chatId, (d) => liveArtifact(d, null, undefined, evicts, nextWindowId, phone))
        return
      }
      const block = eventToBlock(event)
      if (!block) return
      updateFor(chatId, (d) => liveArtifact(d, block, dbId, evicts, nextWindowId, phone))
    })

    // Replay-on-open: re-open the FINAL TURN's persisted popups (recency rule,
    // identity dedupe + cap in lib/displayReplay; per-browser X-dismissals
    // filtered there). Runs after subscribe, so frames racing in live are
    // deduped against the seed by row id in upsertWindow.
    let stale = false
    fetchChatPage(chatId, 50)
      .then(({ messages }) => {
        if (stale) return
        const replay = replayableDisplayEvents(messages, loadDismissedPips(chatId))
        if (!replay.length) return
        const phone = phoneNow()
        updateFor(chatId, (d) => replayArtifacts(d, replay, nextWindowId, phone))
      })
      .catch(() => { /* seed is best-effort — live frames still render */ })

    return () => {
      stale = true
      unsub()
    }
  }, [chatId, subscribe, phoneNow, updateFor])

  return {
    windows: dock.windows,
    minimized: dock.minimized,
    close,
    minimize,
    restore,
    dockOrder: dock.order,
    setDockOrder,
    /** A terminal chat: the windows are live. */
    terminal: !!chatId,
  }
}
