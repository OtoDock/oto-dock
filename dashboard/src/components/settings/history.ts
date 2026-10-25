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
// storage refuses writes.

const KEY = 'otodock.settingsTrail'
/** React Router's key for a location it never keyed: the first one after a
 *  load from outside the app, until something replaces it. */
export const INITIAL_LOCATION_KEY = 'default'
/** Entries kept below the current one. */
const KEEP = 200
const CHAT_PREFIX = '/chat/'

export interface Trail {
  entries: Record<number, string>
  /** The last chat this tab showed (a pathname). */
  lastChat: string | null
}

export type NavAction = 'PUSH' | 'REPLACE' | 'POP'

const EMPTY: Trail = { entries: {}, lastChat: null }

let memory: Trail = EMPTY
let storageBroken = false

function parseTrail(raw: string): Trail {
  const parsed = JSON.parse(raw) as Partial<Trail> | null
  const entries: Record<number, string> = {}
  if (parsed && typeof parsed.entries === 'object' && parsed.entries) {
    for (const [k, v] of Object.entries(parsed.entries)) {
      const n = Number(k)
      if (Number.isInteger(n) && n >= 0 && typeof v === 'string') entries[n] = v
    }
  }
  return { entries, lastChat: typeof parsed?.lastChat === 'string' ? parsed.lastChat : null }
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
 *  pathname alone kept. A push drops every entry above it, as the browser
 *  did. */
export function recordEntry(trail: Trail, idx: number, path: string, action: NavAction): Trail {
  const entries: Record<number, string> = {}
  for (const [k, v] of Object.entries(trail.entries)) {
    const n = Number(k)
    if (action === 'PUSH' && n > idx) continue
    if (n < idx - KEEP) continue
    entries[n] = v
  }
  const pathname = path.split(/[?#]/, 1)[0]
  entries[idx] = pathname
  return { entries, lastChat: isChatPath(pathname) ? pathname : trail.lastChat }
}

export function saveEntry(idx: number, path: string, action: NavAction): void {
  writeTrail(recordEntry(readTrail(), idx, path, action))
}

export function clearTrail(): void {
  writeTrail(EMPTY)
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
  | { kind: 'pop'; delta: number }
  /** The cold root (the guard's `/`, or an entry the trail never saw):
   *  travel there and replace it with the chat, so it stays entry 0. */
  | { kind: 'replace'; delta: number; target: string }
  /** Another page sits under the areas (the agents page, an app): travel
   *  there and push the chat over the areas, so that page stays behind. */
  | { kind: 'push'; delta: number; target: string }

/** How "Back to Chat" leaves the settings. Walks down over the entries of
 *  any settings area to the first entry below them. */
export function planBackToChat(trail: Trail, idx: number, target: string): BackToChat {
  let j = idx - 1
  while (j >= 0) {
    const path = trail.entries[j]
    if (path === undefined || settingsBaseOf(path) === null) break
    j--
  }
  if (j < 0) return { kind: 'replace', delta: idx > 0 ? -idx : 0, target }
  const path = trail.entries[j]
  const delta = j - idx
  if (path !== undefined && isChatPath(path)) return { kind: 'pop', delta }
  if (path === undefined || path === '/') return { kind: 'replace', delta, target }
  return { kind: 'push', delta, target }
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
