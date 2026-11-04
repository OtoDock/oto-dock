/**
 * When the apps ask for a store review. The store's own sheet (Play's in-app
 * review, the App Store's) shows only if the store decides so and it never
 * says; this module decides when to hand it the request, so the timing
 * changes with the server and never needs a store release.
 *
 * Only inside an app shell with the native channel, never in a browser, and
 * only while the admin's switch is on. A request goes out at a success
 * moment (a reply to the person's own message, read to its end, a push they
 * opened into a chat, the third distinct day they opened the app), never on
 * the first day of the record, never in a session's first three minutes (a
 * moment then waits for the mark), never within two minutes of an error, and
 * at most twice, the second after the admin's number of days. The record is
 * kept on the device and in the person's UI preferences, merged as a union,
 * so a reinstall does not start it again.
 */

import { requestStoreReview } from '../nativeBridge'
import { apiJson } from '../http/apiJson'
import { debugLog } from '../debug/log'
import { onAppToast } from '../appToasts'
import { NOTIFICATION_SEVERITY } from '../status/notificationSeverity'
import { CHAT_PHASE } from '../status/chat'
import { micBusy } from '../../audio/micCoordinator'
import { isSpeaking } from '../../audio/speechActivity'
import { useChatStore } from '../../store/chatStore'

/** The device's copy, per person: local storage is per origin, so per
 *  installation, and two people may sign in on one phone. */
export function storeReviewKey(sub: string): string {
  return `otodock.storeReview.${sub}`
}
/** The server's copy: one key of the person's UI preferences. */
export const STORE_REVIEW_PREF = 'store_review'
const PREFS_PATH = '/v1/users/me/ui-prefs'

const MINUTE = 60 * 1000
const DAY = 24 * 60 * MINUTE
export const FIRST_DAY_MS = DAY
export const SESSION_WARMUP_MS = 3 * MINUTE
export const QUIET_AFTER_ERROR_MS = 2 * MINUTE
/** A page that comes back after this long hidden starts a new session. */
export const SESSION_GAP_MS = 30 * MINUTE
/** How long a reply that ended cleanly waits for the person to reach it. */
export const READ_WAIT_MS = 10 * MINUTE
export const MAX_ASKS = 2
/** The day moment is the third distinct day; later days are not kept. */
const KEPT_DAYS = 3
const DEFAULT_DAYS_BETWEEN = 90

export interface StoreReviewRecord {
  since: number
  days: string[]
  asked: number[]
}

export type StoreReviewMoment = 'reply' | 'push' | 'day'

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/

function stamp(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0
}

/** A record from storage or the server, or null when any part is malformed. */
export function parseRecord(raw: unknown): StoreReviewRecord | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  if (!stamp(r.since)) return null
  if (!Array.isArray(r.days) || !r.days.every((d) => typeof d === 'string' && DAY_RE.test(d))) return null
  if (!Array.isArray(r.asked) || !r.asked.every(stamp)) return null
  return { since: r.since, days: r.days as string[], asked: r.asked as number[] }
}

/** The union of two records: the earliest start, the first days, the first asks. */
export function mergeRecords(a: StoreReviewRecord | null, b: StoreReviewRecord | null): StoreReviewRecord | null {
  if (!a) return b && { since: b.since, days: [...b.days], asked: [...b.asked] }
  if (!b) return { since: a.since, days: [...a.days], asked: [...a.asked] }
  return {
    since: Math.min(a.since, b.since),
    days: [...new Set([...a.days, ...b.days])].sort().slice(0, KEPT_DAYS),
    asked: [...new Set([...a.asked, ...b.asked])].sort((x, y) => x - y).slice(0, MAX_ASKS),
  }
}

function sameRecord(a: StoreReviewRecord | null, b: StoreReviewRecord | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** The local calendar day of a time. */
export function localDay(ms: number): string {
  const d = new Date(ms)
  const two = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`
}

function readDevice(e: Engine): StoreReviewRecord | null {
  try {
    const raw = localStorage.getItem(storeReviewKey(e.sub))
    return raw ? parseRecord(JSON.parse(raw)) : null
  } catch {
    return null
  }
}

function writeDevice(e: Engine, r: StoreReviewRecord | null): void {
  if (!r) return
  try {
    localStorage.setItem(storeReviewKey(e.sub), JSON.stringify(r))
  } catch {
    // A page without storage keeps the server's copy only.
  }
}

function log(...parts: unknown[]): void {
  debugLog('store review:', ...parts)
}

interface Engine {
  /** The signed-in person. */
  sub: string
  record: StoreReviewRecord | null
  /** The server's copy has been read and merged at least once. */
  read: boolean
  sessionStart: number
  hiddenAt: number | null
  lastError: number
  held: ReturnType<typeof setTimeout> | null
  asking: boolean
  /** The app answered "not handed": no further request from this page. */
  stopped: boolean
  pendingReply: { chatId: string; at: number } | null
  /** The chats this page sent a message to, with the time, until that turn
   *  ends: only those turns count (a shared chat streams other people's). */
  sent: Map<string, number>
  /** A send that opened a new chat, before the server named it. */
  sentNew: number
  /** The chat on screen and whether its list is at its end. */
  view: { chatId: string; atEnd: boolean } | null
  chain: Promise<void>
}

const settings = { on: true, daysBetween: DEFAULT_DAYS_BETWEEN }
let engine: Engine | null = null

/** The admin's two settings, as the person's user payload carries them. */
export function setStoreReviewSettings(on: boolean, daysBetween: number): void {
  settings.on = on
  settings.daysBetween = Number.isFinite(daysBetween) && daysBetween > 0 ? daysBetween : DEFAULT_DAYS_BETWEEN
}

/** One serialized step with the server: read its copy, merge it with ours,
 *  change the merged record if asked to, and write it back when the server
 *  lacks anything. True when the server's copy was read; a failed read
 *  leaves the record as it was. */
function sync(e: Engine, change?: (r: StoreReviewRecord) => void): Promise<boolean> {
  const step = e.chain.then(async () => {
    try {
      const bag = await apiJson<Record<string, unknown>>(PREFS_PATH)
      const server = parseRecord(bag?.[STORE_REVIEW_PREF])
      const merged = mergeRecords(mergeRecords(readDevice(e), e.record), server)
        ?? { since: Date.now(), days: [], asked: [] }
      change?.(merged)
      e.record = merged
      e.read = true
      writeDevice(e, merged)
      if (!sameRecord(server, merged)) {
        const out = await apiJson<Record<string, unknown>>(PREFS_PATH, {
          method: 'PUT', json: { [STORE_REVIEW_PREF]: merged },
        })
        e.record = mergeRecords(e.record, parseRecord(out?.[STORE_REVIEW_PREF]))
        writeDevice(e, e.record)
      }
      return true
    } catch (err) {
      log('the server copy was not read or written', err)
      return false
    }
  })
  e.chain = step.then(() => undefined)
  return step
}

/** What keeps a request from going out now, or null. */
function blocker(e: Engine, now: number): string | null {
  if (!settings.on) return 'turned off'
  if (!e.read || !e.record) return 'the record is not read yet'
  if (now - e.record.since < FIRST_DAY_MS) return 'the first day'
  const asked = e.record.asked
  if (asked.length >= MAX_ASKS) return 'asked twice already'
  if (asked.length === 1 && now - asked[0] < settings.daysBetween * DAY) {
    return `asked once, waits ${settings.daysBetween} days`
  }
  if (now - e.lastError < QUIET_AFTER_ERROR_MS) return 'an error came less than two minutes ago'
  if (typeof document !== 'undefined' && document.hidden) return 'the page is hidden'
  if (e.stopped) return 'the app did not hand the last request over'
  return null
}

/** The person is in the middle of something: typing, talking or dictating,
 *  a reply read aloud, or a reply streaming. */
function busy(e: Engine): string | null {
  const el = typeof document !== 'undefined' ? document.activeElement : null
  if (el instanceof HTMLElement && (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) {
    return 'the person is typing'
  }
  if (micBusy()) return 'a voice conversation or dictation is on'
  if (isSpeaking()) return 'a reply is being read aloud'
  const chatId = e.view?.chatId
  if (chatId && useChatStore.getState().byChat[chatId]?.status === CHAT_PHASE.STREAMING) {
    return 'a reply is streaming'
  }
  return null
}

function moment(kind: StoreReviewMoment, held = false): void {
  const e = engine
  if (!e || !settings.on) return
  const now = Date.now()
  const label = held ? `held ${kind}` : kind
  const why = blocker(e, now) ?? (e.asking ? 'a request is already out' : null)
  if (why) {
    log(`moment ${label}, not asked: ${why}`)
    return
  }
  const left = e.sessionStart + SESSION_WARMUP_MS - now
  if (left > 0) {
    if (e.held) {
      log(`moment ${label}, one is held already`)
      return
    }
    log(`moment ${label}, held until the session's third minute`)
    e.held = setTimeout(() => {
      if (engine !== e) return
      e.held = null
      moment(kind, true)
    }, left)
    return
  }
  const occupied = busy(e)
  if (occupied) {
    log(`moment ${label}, not asked: ${occupied}`)
    return
  }
  log(`moment ${label}`)
  void ask(e)
}

async function ask(e: Engine): Promise<void> {
  e.asking = true
  try {
    // Another phone may have asked since this page read the server; without
    // the server's copy the page cannot know, so it does not ask.
    const read = await sync(e)
    if (engine !== e) return
    if (!read) {
      log('not asked: the server copy was not read')
      return
    }
    const why = blocker(e, Date.now()) ?? busy(e)
    if (why) {
      log(`not asked after reading the server: ${why}`)
      return
    }
    const handed = await requestStoreReview()
    if (engine !== e) return
    if (!handed) {
      e.stopped = true
      log('not handed to a store')
      return
    }
    // The ask is kept on the device before the server hears of it: a server
    // that does not answer now gets it from the union at the next good read.
    const at = Date.now()
    log('handed to the store')
    if (e.record) {
      e.record = { ...e.record, asked: [...new Set([...e.record.asked, at])].sort((x, y) => x - y).slice(0, MAX_ASKS) }
      writeDevice(e, e.record)
    }
    await sync(e)
  } finally {
    e.asking = false
  }
}

/** Today counts as a day the app was opened; the third such day is a moment.
 *  A record already read that holds today, or its three days, needs no read
 *  of the server: the read before an ask stays. */
async function openDay(e: Engine): Promise<void> {
  const today = localDay(Date.now())
  const r = e.record
  if (e.read && r && (r.days.includes(today) || r.days.length >= KEPT_DAYS)) return
  let third = false
  await sync(e, (r) => {
    if (r.days.includes(today) || r.days.length >= KEPT_DAYS) return
    r.days = [...r.days, today].sort()
    third = r.days.length === KEPT_DAYS && r.days[KEPT_DAYS - 1] === today
  })
  if (engine === e && third) moment('day')
}

/** Start the engine for this page and person; the returned function stops it. */
export function startStoreReview(sub: string): () => void {
  const now = Date.now()
  const e: Engine = {
    sub, record: null, read: false, sessionStart: now, hiddenAt: null, lastError: 0, held: null,
    asking: false, stopped: false, pendingReply: null, sent: new Map(), sentNew: 0, view: null,
    chain: Promise.resolve(),
  }
  engine = e
  void openDay(e)

  const onVisibility = () => {
    const t = Date.now()
    if (document.hidden) {
      e.hiddenAt = t
      return
    }
    // A return after a long absence is a new session; the server's copy is
    // read once per session (and again until a first read succeeds).
    const newSession = e.hiddenAt !== null && t - e.hiddenAt >= SESSION_GAP_MS
    if (newSession) e.sessionStart = t
    e.hiddenAt = null
    if (newSession || !e.read) void openDay(e)
  }
  document.addEventListener('visibilitychange', onVisibility)
  const offToasts = onAppToast((t) => {
    if (t.severity === NOTIFICATION_SEVERITY.WARNING || t.severity === NOTIFICATION_SEVERITY.DANGER) {
      noteStoreReviewError()
    }
  })
  return () => {
    document.removeEventListener('visibilitychange', onVisibility)
    offToasts()
    if (e.held) clearTimeout(e.held)
    if (engine === e) engine = null
  }
}

/** How long a send waits for its turn to stream. */
const SENT_WAIT_MS = 10 * MINUTE

/** The person sent a message from this page; a new chat has no id yet. */
export function noteTurnSent(chatId: string | null | undefined): void {
  const e = engine
  if (!e) return
  if (chatId) e.sent.set(chatId, Date.now())
  else e.sentNew = Date.now()
}

/** A turn of a chat on screen streamed its first text: an earlier reply of
 *  that chat is no longer the one to wait for. A new chat the page just
 *  opened takes the send made before it had an id. */
export function noteTurnStreaming(chatId: string): void {
  const e = engine
  if (!e) return
  if (e.pendingReply?.chatId === chatId) e.pendingReply = null
  if (!e.sent.has(chatId) && e.sentNew && Date.now() - e.sentNew <= SENT_WAIT_MS) {
    e.sent.set(chatId, e.sentNew)
    e.sentNew = 0
  }
}

/** A turn that streamed text ended; clean means no error and no stop. Only a
 *  turn this page sent counts: another person's turn in a shared chat, a
 *  re-attach or a task run is no moment of this person's. */
export function noteTurnEnded(chatId: string, clean: boolean): void {
  const e = engine
  if (!e) return
  const sentAt = e.sent.get(chatId)
  e.sent.delete(chatId)
  if (sentAt === undefined || Date.now() - sentAt > SENT_WAIT_MS + READ_WAIT_MS) {
    log('a turn ended that this page did not send')
    return
  }
  e.pendingReply = clean ? { chatId, at: Date.now() } : null
  if (clean && e.view?.chatId === chatId && e.view.atEnd) {
    e.pendingReply = null
    moment('reply')
  }
}

/** The chat list on screen is at its end (the latest reply's end shows), or not. */
export function noteChatAtBottom(chatId: string | undefined, atEnd: boolean): void {
  const e = engine
  if (!e) return
  e.view = chatId ? { chatId, atEnd } : null
  const p = e.pendingReply
  if (!p || !atEnd || p.chatId !== chatId) return
  e.pendingReply = null
  if (Date.now() - p.at <= READ_WAIT_MS) moment('reply')
}

/** A push the person tapped opened a chat. */
export function noteStoreReviewMoment(kind: 'push'): void {
  moment(kind)
}

/** An error the person saw: an error toast, a refused card, a failed turn. */
export function noteStoreReviewError(): void {
  if (engine) engine.lastError = Date.now()
}
