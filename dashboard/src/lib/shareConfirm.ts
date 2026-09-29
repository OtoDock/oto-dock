/**
 * The identity-provider confirm round trip (SHARING.md "The confirm").
 *
 * Making a link from an account that signed in through the provider means
 * leaving the page for the provider to answer for the account and coming
 * back (a signed-in browser is answered at once). What the person
 * asked for waits here meanwhile: the choices they made (never a generated
 * password), keyed by account, for five minutes at most, taken once, and
 * cleared when the account signs out. The one-shot confirm token the
 * callback brings back waits the same way; the server binds it to the
 * account and consumes it, so a stale or foreign entry buys nothing.
 */

export interface PendingShare {
  app_id: string
  target_kind: 'app' | 'chat'
  op: 'create' | 'patch'
  share_id?: string
  tab: 'link'
  /** The choices to restore. */
  fields: { public: boolean; expiry: string; buttons: boolean; include_tools: boolean; allow_actions?: boolean }
  /** Where the callback sends the page back to. */
  return_to: string
  at: number
}

const TTL_MS = 5 * 60 * 1000
const PENDING = 'otodock-share-pending:'
const CONFIRM = 'otodock-confirm:'

function read<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch { return null }
}
function write(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode */ }
}
function drop(key: string): void {
  try { localStorage.removeItem(key) } catch { /* private mode */ }
}

export function savePending(sub: string, entry: Omit<PendingShare, 'at'>): void {
  write(PENDING + sub, { ...entry, at: Date.now() })
}

/** The account's pending share, still fresh; a stale one is dropped. */
export function peekPending(sub: string): PendingShare | null {
  const e = read<PendingShare>(PENDING + sub)
  if (!e) return null
  if (typeof e.at !== 'number' || Date.now() - e.at > TTL_MS) {
    drop(PENDING + sub)
    return null
  }
  return e
}

/** The pending share for THIS app, taken (another app's stays). */
export function takePending(sub: string, appId: string): PendingShare | null {
  const e = peekPending(sub)
  if (!e || e.app_id !== appId) return null
  drop(PENDING + sub)
  return e
}

export function saveConfirm(sub: string, token: string): void {
  write(CONFIRM + sub, { token, at: Date.now() })
}

/** The confirm token, taken; null when there is none or it is stale. */
export function takeConfirm(sub: string): string | null {
  const e = read<{ token: string; at: number }>(CONFIRM + sub)
  drop(CONFIRM + sub)
  if (!e || typeof e.at !== 'number' || Date.now() - e.at > TTL_MS || !e.token) return null
  return e.token
}

/** Every account's entries: the sign-out sweep. */
export function clearShareConfirm(): void {
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i) || ''
      if (k.startsWith(PENDING) || k.startsWith(CONFIRM)) localStorage.removeItem(k)
    }
  } catch { /* private mode */ }
}
