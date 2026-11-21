// The chat store's drafts and queued messages in this browser's storage,
// shared by every window of the dashboard.
//
// Each chat's draft and each chat's queue have a key of their own, named
// after the person who owns them, and a window writes a key only when that
// value changed in that window: a window never erases what another window
// wrote. Another window's write reaches this one as a `storage` event and
// is taken into the store, unless this window holds an edit of that draft
// it has not stored yet. Only the window with the focus holds one: it stores
// its text 300 ms after the last keystroke, and at once when it loses the
// focus, is hidden or closes. Every other change is stored at once (an empty
// draft, a queue, a text that came back to a window the person is not in),
// so the window the person types in keeps what they type and the others
// follow the newest draft.
//
// A value this window could not store (storage full, or no storage at all)
// stays as the window holds it: reading storage again never takes it away,
// only a write of it that succeeds here or another window's write of it.
//
// Nothing is read or written before `bindChatDrafts` names the person.

import { useChatStore, type ChatSlice } from './chatStore'
import { toQueuedMessage, type QueuedMessage } from './types'
import { logError } from '../lib/debug/log'

const DRAFT_PREFIX = 'oto-dock-draft:'
const QUEUE_PREFIX = 'oto-dock-queue:'
/** The one record of every chat's draft and queue that earlier versions kept. */
const LEGACY_KEY = 'oto-dock-chat-store'
const TEXT_DELAY_MS = 300
const NO_QUEUE: QueuedMessage[] = []

let owner = ''
let unsubscribe: (() => void) | null = null
let applying = false
const pending = new Map<string, string>()
/** Keys whose last write in this window was refused. */
const unstored = new Set<string>()
let timer: ReturnType<typeof setTimeout> | null = null

/** The browser's storage, or null where the page may not use it. */
function storage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

function hasFocus(): boolean {
  try {
    return document.hasFocus()
  } catch {
    return true
  }
}

function ownerPart(sub: string): string {
  return `${encodeURIComponent(sub)}:`
}

function draftKeyOf(chatId: string): string {
  return DRAFT_PREFIX + ownerPart(owner) + chatId
}

function queueKeyOf(chatId: string): string {
  return QUEUE_PREFIX + ownerPart(owner) + chatId
}

/** Every stored draft or queue key, of any person. */
function storedKeys(store: Storage): string[] {
  const keys: string[] = []
  for (let i = 0; i < store.length; i++) {
    const key = store.key(i)
    if (key && (key.startsWith(DRAFT_PREFIX) || key.startsWith(QUEUE_PREFIX))) keys.push(key)
  }
  return keys
}

/** `null` stores nothing under the key. False when storage refused it. */
function write(key: string, value: string | null): boolean {
  const store = storage()
  if (!store) return false
  try {
    if (value === null) store.removeItem(key)
    else store.setItem(key, value)
    return true
  } catch {
    return false
  }
}

/** A write of this window's own value, marked when it was refused. */
function keep(key: string, value: string | null): void {
  if (write(key, value)) unstored.delete(key)
  else unstored.add(key)
}

/** Writes the draft text not stored yet. */
export function flushChatDrafts(): void {
  if (timer) clearTimeout(timer)
  timer = null
  const due = [...pending]
  pending.clear()
  for (const [key, value] of due) keep(key, value)
}

function storeText(key: string, text: string): void {
  pending.set(key, text)
  if (timer) clearTimeout(timer)
  timer = setTimeout(flushChatDrafts, TEXT_DELAY_MS)
}

function storeNow(key: string, value: string | null): void {
  pending.delete(key)
  keep(key, value)
}

function queueText(queue: QueuedMessage[]): string | null {
  return queue.length ? JSON.stringify(queue) : null
}

function readQueue(raw: string | null): QueuedMessage[] {
  if (!raw) return NO_QUEUE
  try {
    const list: unknown = JSON.parse(raw)
    return Array.isArray(list) ? list.map(toQueuedMessage) : NO_QUEUE
  } catch {
    return NO_QUEUE
  }
}

/** The store changed: the drafts and queues that changed in this window
 *  are stored, nothing else. */
function onStoreChange(next: { byChat: Record<string, ChatSlice> }, prev: { byChat: Record<string, ChatSlice> }): void {
  if (applying || next.byChat === prev.byChat) return
  const ids = new Set([...Object.keys(next.byChat), ...Object.keys(prev.byChat)])
  for (const id of ids) {
    const now = next.byChat[id]
    const before = prev.byChat[id]
    if (now === before) continue
    const text = now?.draftInput ?? ''
    if (text !== (before?.draftInput ?? '')) {
      if (text && hasFocus()) storeText(draftKeyOf(id), text)
      else storeNow(draftKeyOf(id), text || null)
    }
    const queue = now?.queuedMessages ?? NO_QUEUE
    const queueBefore = before?.queuedMessages ?? NO_QUEUE
    if (queue !== queueBefore && (queue.length || queueBefore.length)) {
      storeNow(queueKeyOf(id), queueText(queue))
    }
  }
}

/** What storage holds for one chat's draft or queue goes into the store,
 *  unless this window has an edit of it not stored yet, or could not store
 *  its own value and no other window wrote the key since (`written`). */
function take(store: Storage | null, key: string, written: boolean): void {
  if (!store || pending.has(key)) return
  if (unstored.has(key)) {
    if (!written) return
    unstored.delete(key)
  }
  let raw: string | null
  try {
    raw = store?.getItem(key) ?? null
  } catch {
    return
  }
  const st = useChatStore.getState()
  applying = true
  try {
    if (key.startsWith(DRAFT_PREFIX + ownerPart(owner))) {
      const id = key.slice((DRAFT_PREFIX + ownerPart(owner)).length)
      const text = raw ?? ''
      if ((st.byChat[id]?.draftInput ?? '') !== text) st.setDraftInput(id, text)
    } else if (key.startsWith(QUEUE_PREFIX + ownerPart(owner))) {
      const id = key.slice((QUEUE_PREFIX + ownerPart(owner)).length)
      const queue = readQueue(raw)
      const held = st.byChat[id]?.queuedMessages ?? NO_QUEUE
      if (JSON.stringify(held) !== JSON.stringify(queue)) st.setQueuedMessages(id, [...queue])
    }
  } finally {
    applying = false
  }
}

function takeKey(key: string, written: boolean): void {
  if (key.startsWith(DRAFT_PREFIX + ownerPart(owner)) || key.startsWith(QUEUE_PREFIX + ownerPart(owner))) {
    take(storage(), key, written)
  }
}

/** Another window wrote `key` (`null`: it emptied the whole storage). */
export function takeStoredChange(key: string | null): void {
  if (!owner) return
  if (key === null) takeAll()
  else takeKey(key, true)
}

/** Every draft and queue of this person, stored or held, as storage has it. */
function takeAll(): void {
  const store = storage()
  const keys = new Set(store ? storedKeys(store) : [])
  for (const [id, slice] of Object.entries(useChatStore.getState().byChat)) {
    if (slice.draftInput) keys.add(draftKeyOf(id))
    if (slice.queuedMessages.length) keys.add(queueKeyOf(id))
  }
  for (const key of keys) takeKey(key, false)
}

/** The record of earlier versions, each chat's draft and queue moved to
 *  keys of their own where none is stored yet; the record goes first, to
 *  free its room. A value storage refuses is held in the store instead. */
function carryOver(store: Storage): void {
  let raw: string | null
  try {
    raw = store.getItem(LEGACY_KEY)
  } catch {
    return
  }
  if (raw === null) return
  let byChat: Record<string, Partial<ChatSlice>> = {}
  try {
    const record = JSON.parse(raw) as { state?: { byChat?: Record<string, Partial<ChatSlice>> } }
    byChat = record.state?.byChat ?? {}
  } catch (e) {
    logError('[drafts] the stored drafts of an earlier version could not be read:', e)
  }
  write(LEGACY_KEY, null)
  const st = useChatStore.getState()
  for (const [id, slice] of Object.entries(byChat)) {
    const text = typeof slice?.draftInput === 'string' ? slice.draftInput : ''
    if (text && store.getItem(draftKeyOf(id)) === null) {
      keep(draftKeyOf(id), text)
      if (unstored.has(draftKeyOf(id))) st.setDraftInput(id, text)
    }
    const queue = Array.isArray(slice?.queuedMessages) ? slice.queuedMessages.map(toQueuedMessage) : NO_QUEUE
    if (queue.length && store.getItem(queueKeyOf(id)) === null) {
      keep(queueKeyOf(id), queueText(queue))
      if (unstored.has(queueKeyOf(id))) st.setQueuedMessages(id, queue)
    }
  }
}

function onStorage(e: StorageEvent): void {
  if (e.storageArea !== storage()) return
  takeStoredChange(e.key)
}

function onVisibility(): void {
  if (document.visibilityState === 'hidden') flushChatDrafts()
  else takeAll()
}

function onPageShow(e: PageTransitionEvent): void {
  if (e.persisted) takeAll()
}

/** Stops storing and following, and drops the drafts and queues this
 *  window holds: they are the person's who is leaving. */
function unbind(): void {
  if (timer) clearTimeout(timer)
  timer = null
  pending.clear()
  unstored.clear()
  unsubscribe?.()
  unsubscribe = null
  owner = ''
  window.removeEventListener('storage', onStorage)
  window.removeEventListener('blur', flushChatDrafts)
  window.removeEventListener('pagehide', flushChatDrafts)
  window.removeEventListener('pageshow', onPageShow)
  document.removeEventListener('visibilitychange', onVisibility)
  const st = useChatStore.getState()
  for (const [id, slice] of Object.entries(st.byChat)) {
    if (slice.draftInput) st.setDraftInput(id, '')
    if (slice.queuedMessages.length) st.setQueuedMessages(id, [])
  }
}

/** The person is known: their stored drafts and queues load into the store
 *  and every later change is stored. Another person's are removed. */
export function bindChatDrafts(sub: string): void {
  if (!sub || sub === owner) return
  if (owner) unbind()
  owner = sub
  const store = storage()
  if (store) {
    const mine = [DRAFT_PREFIX + ownerPart(owner), QUEUE_PREFIX + ownerPart(owner)]
    for (const key of storedKeys(store)) {
      if (!mine.some((prefix) => key.startsWith(prefix))) write(key, null)
    }
    carryOver(store)
  }
  takeAll()
  unsubscribe = useChatStore.subscribe(onStoreChange)
  window.addEventListener('storage', onStorage)
  window.addEventListener('blur', flushChatDrafts)
  window.addEventListener('pagehide', flushChatDrafts)
  window.addEventListener('pageshow', onPageShow)
  document.addEventListener('visibilitychange', onVisibility)
}

/** A sign-out, or another person's drafts of an earlier version: every
 *  stored draft and queue goes, of every person, and nothing more is stored
 *  or followed until the next bind. */
export function clearChatDrafts(): void {
  unbind()
  const store = storage()
  if (!store) return
  for (const key of storedKeys(store)) write(key, null)
  write(LEGACY_KEY, null)
}
