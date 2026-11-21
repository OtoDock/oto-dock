/**
 * What the host keeps about an app's media links (the addresses
 * `files.media` answers). A link is an address that needs no sign-in, made for the page
 * that asked: it stays in the app. The host remembers the secret part of
 * every link it handed a frame and refuses to pass on page text that
 * carries one in plain form (an address to open, a target to open, a
 * button's arguments, a prompt's, a platform call's).
 */
import type { User } from '../api/auth'
import { HEAD } from './layout/tree'
import { canManageAgent, canWriteWorkspace } from './permissions'

const SECRET_RE = /[?&]t=([A-Za-z0-9_-]{20,})/
const KEEP = 512

export const MEDIA_STAYS = 'a media link stays in the app'

function secretOf(url: unknown): string {
  return typeof url === 'string' ? SECRET_RE.exec(url)?.[1] ?? '' : ''
}

/** Remember the links of a `files.media` answer (one link, or `links`). */
export function rememberMediaSecrets(held: Set<string>, result: unknown): void {
  const one = result as { url?: unknown; links?: unknown } | null
  const rows = Array.isArray(one?.links) ? (one.links as { url?: unknown }[]) : [one]
  for (const row of rows) {
    const secret = secretOf(row?.url)
    if (!secret) continue
    held.delete(secret)
    held.add(secret)
  }
  // The oldest go first: a link lives half an hour at most.
  for (const old of held) {
    if (held.size <= KEEP) break
    held.delete(old)
  }
}

/** Whether page text (a string, or anything JSON can write) carries one.
 *  A value JSON cannot write is not read here, and some roads take its
 *  parts without JSON (a prompt's arguments): it counts as carrying one. */
export function carriesMediaSecret(held: Set<string>, value: unknown): boolean {
  if (!held.size || value === null || value === undefined) return false
  let text: string
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value) ?? ''
  } catch {
    return true
  }
  if (text.length < 20) return false
  for (const secret of held) if (text.includes(secret)) return true
  return false
}

/**
 * The viewer's own write right on a workspace path an app opens over
 * itself, for the preview's edit controls (the server judges every write
 * again): their own folder, the shared workspace at the workspace tier,
 * the knowledge library for a manager.
 */
export function fileCanWrite(user: User | null | undefined, agent: string, path: string): boolean {
  const parts = path.split('/')
  if (parts[0] === HEAD.USERS) return !!user?.username && parts[1] === user.username
  if (parts[0] === HEAD.WORKSPACE) return canWriteWorkspace(user, agent)
  if (parts[0] === HEAD.KNOWLEDGE) return canManageAgent(user, agent)
  return false
}
