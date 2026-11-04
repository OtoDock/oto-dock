import { settingsBaseOf } from './sections'

// The tab's history as the dashboard rendered it: React Router numbers each
// entry (`history.state.idx`, 0 at the tab's first entry after a load from
// outside the app), and the trail keeps the pathname shown at each number,
// never the query: a single-use token in it (a password reset, an OAuth
// return) must not reach storage, and nothing reads a query back. The
// settings areas read it to know what sits behind an entry: another section
// of the same area (back pops to it), a chat ("Back to Chat" pops to it),
// or nothing they know. sessionStorage, so it lives as long as the tab's
// own entries do, reloads included; a copy in memory serves a tab whose
// storage refuses writes. Each entry also keeps the router's key of the
// location shown there (`history.state.key`, "" for the unkeyed first
// entry): a full-page navigation starts a new page life whose numbers begin
// at 0 again while the older life's entries stay behind it, and only the
// key tells one life's entry from the other's.

const KEY = 'otodock.settingsTrail'
/** React Router's key for a location it never keyed: the first one after a
 *  load from outside the app, until something replaces it. */
export const INITIAL_LOCATION_KEY = 'default'
/** Entries kept below the current one. */
const KEEP = 200
const CHAT_PREFIX = '/chat/'

export interface Trail {
  entries: Record<number, string>
  /** The router's key of each entry, where recorded. */
  keys?: Record<number, string>
  /** The entry number this page life starts at when the trail was dropped
   *  on a keyed load into it: the entries below belong to another document,
   *  and a travel there would load it with no follow-up. */
  floor?: number
  /** The last chat this tab showed (a pathname). */
  lastChat: string | null
}

export type NavAction = 'PUSH' | 'REPLACE' | 'POP'

const EMPTY: Trail = { entries: {}, lastChat: null }

let memory: Trail = EMPTY
let storageBroken = false

function numbered(raw: unknown): Record<number, string> {
  const out: Record<number, string> = {}
  if (raw && typeof raw === 'object') {
    for (const [k, v] of Object.entries(raw)) {
      const n = Number(k)
      if (Number.isInteger(n) && n >= 0 && typeof v === 'string') out[n] = v
    }
  }
  return out
}

function parseTrail(raw: string): Trail {
  const parsed = JSON.parse(raw) as Partial<Trail> | null
  // A trail stored before the keys has none: its entries plan as before.
  const keys = numbered(parsed?.keys)
  const floor = parsed?.floor
  return {
    entries: numbered(parsed?.entries),
    ...(Object.keys(keys).length ? { keys } : {}),
    ...(Number.isInteger(floor) && (floor as number) > 0 ? { floor: floor as number } : {}),
    lastChat: typeof parsed?.lastChat === 'string' ? parsed.lastChat : null,
  }
}

export function readTrail(): Trail {
  if (storageBroken) return memory
  let raw: string | null
  try {
    raw = sessionStorage.getItem(KEY)
  } catch {
    storageBroken = true
    return memory
  }
  if (raw === null) return EMPTY
  try {
    memory = parseTrail(raw)
  } catch {
    memory = EMPTY
  }
  return memory
}

function writeTrail(trail: Trail): void {
  memory = trail
  if (storageBroken) return
  try {
    sessionStorage.setItem(KEY, JSON.stringify(trail))
  } catch {
    storageBroken = true
  }
}

export function isChatPath(path: string): boolean {
  return path.startsWith(CHAT_PREFIX)
}

/** The trail after the browser showed `path` at `idx` by `action`, its
 *  pathname alone kept, with the location's key when given. A push drops
 *  every entry above it, as the browser did. */
export function recordEntry(trail: Trail, idx: number, path: string, action: NavAction, key?: string): Trail {
  const kept = (n: number) => !(action === 'PUSH' && n > idx) && n >= idx - KEEP
  const entries: Record<number, string> = {}
  for (const [k, v] of Object.entries(trail.entries)) if (kept(Number(k))) entries[Number(k)] = v
  const keys: Record<number, string> = {}
  for (const [k, v] of Object.entries(trail.keys ?? {})) if (kept(Number(k))) keys[Number(k)] = v
  const pathname = path.split(/[?#]/, 1)[0]
  entries[idx] = pathname
  if (key === undefined) delete keys[idx]
  else keys[idx] = key
  return {
    entries, keys, ...(trail.floor ? { floor: trail.floor } : {}),
    lastChat: isChatPath(pathname) ? pathname : trail.lastChat,
  }
}

export function saveEntry(idx: number, path: string, action: NavAction, key?: string): void {
  writeTrail(recordEntry(readTrail(), idx, path, action, key))
}

/** The key the trail keeps for the browser's current entry: the router's,
 *  "" for an entry it never keyed. */
export function currentKey(): string {
  return (window.history.state as { key?: string } | null)?.key ?? ''
}

/** Whether the trail belongs to the page life the browser shows at `idx`
 *  under `key`: its entry there was recorded with that key (a trail stored
 *  before the keys passes on its path alone). A keyed load into an older
 *  page life (Back, or the Android back gesture, after a full-page
 *  navigation) finds another key there, or no entry. */
export function trailHolds(trail: Trail, idx: number, key: string, path: string): boolean {
  const recorded = trail.keys?.[idx]
  if (recorded !== undefined) return recorded === key
  return trail.entries[idx] === path.split(/[?#]/, 1)[0]
}

export function clearTrail(): void {
  writeTrail(EMPTY)
}

/** Drop another page life's trail on a keyed load at `idx`: nothing below
 *  `idx` is known, and the tab's last chat stays the fallback. */
export function dropTrail(idx: number): void {
  writeTrail({ entries: {}, ...(idx > 0 ? { floor: idx } : {}), lastChat: readTrail().lastChat })
}

/** The browser's current entry number, only while it still shows the
 *  location React rendered; null once another navigation has moved it. A
 *  location with the initial key is an entry React Router never keyed, so
 *  it matches an entry without a key. */
export function currentIdx(locationKey: string): number | null {
  const st = window.history.state as { key?: string; idx?: number } | null
  if (!st || typeof st.idx !== 'number') return null
  if (locationKey === INITIAL_LOCATION_KEY) return st.key ? null : st.idx
  return st.key === locationKey ? st.idx : null
}

/** True when the entry right behind `idx` belongs to the area at `base`. */
export function areaEntryBehind(trail: Trail, idx: number, base: string): boolean {
  const behind = trail.entries[idx - 1]
  return behind !== undefined && settingsBaseOf(behind) === base
}

export type BackToChat =
  /** A chat sits right under the areas: travel to it. */
  | { kind: 'pop'; delta: number; key?: string }
  /** The cold root (the guard's `/`, or an entry the trail never saw):
   *  travel there and replace it with the chat, so it stays entry 0. */
  | { kind: 'replace'; delta: number; target: string; key?: string }
  /** Another page sits under the areas (the agents page, an app): travel
   *  there and push the chat over the areas, so that page stays behind. */
  | { kind: 'push'; delta: number; target: string; key?: string }

/** How "Back to Chat" leaves the settings. Walks down over the entries of
 *  any settings area to the first entry below them. */
export function planBackToChat(trail: Trail, idx: number, target: string): BackToChat {
  const floor = trail.floor ?? 0
  let j = idx - 1
  while (j >= floor) {
    const path = trail.entries[j]
    if (path === undefined || settingsBaseOf(path) === null) break
    j--
  }
  // Below the floor lies another document: the chat is pushed from here.
  if (j < floor && floor > 0) return { kind: 'push', delta: 0, target }
  // The key the browser must show on arrival, where the trail recorded it.
  const at = (n: number) => (trail.keys?.[n] !== undefined ? { key: trail.keys[n] } : {})
  if (j < 0) return { kind: 'replace', delta: idx > 0 ? -idx : 0, target, ...(idx > 0 ? at(0) : {}) }
  const path = trail.entries[j]
  const delta = j - idx
  if (path !== undefined && isChatPath(path)) return { kind: 'pop', delta, ...at(j) }
  if (path === undefined || path === '/') return { kind: 'replace', delta, target, ...at(j) }
  return { kind: 'push', delta, target, ...at(j) }
}

/** The chat "Back to Chat" lands on when no chat is right behind the area:
 *  the tab's last chat, kept to `agent`'s chats when the area belongs to an
 *  agent (else that agent's own chat), and the default agent (`/`) only
 *  when this tab has shown no chat. */
export function fallbackChat(trail: Trail, agent?: string): string {
  if (agent) {
    const own = `${CHAT_PREFIX}${agent}`
    const last = trail.lastChat
    return last && (last === own || last.startsWith(`${own}/`)) ? last : own
  }
  return trail.lastChat ?? '/'
}
