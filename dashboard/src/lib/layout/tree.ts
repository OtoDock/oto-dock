/**
 * The agent tree — the folder names the file browser and the transfer
 * store branch on, mirrored from the proxy's authority `core/layout.py`
 * (`tests/core/test_layout.py` binds this file to it; edit both).
 *
 * An agent-relative path (`users/alice/workspace/report.md`,
 * `workspace/notes.md`, `knowledge/refs/x.md`, `config/agent.md`) begins
 * with one of the four HEADS; a person's tree is `users/<u>/` with its two
 * subdirs (`workspace`, `context`). The dashboard never composes a host
 * path — it reads the tree the API serves and labels it.
 */

/** The folder names, spelled once. */
export const HEAD = {
  USERS: 'users',
  WORKSPACE: 'workspace',
  KNOWLEDGE: 'knowledge',
  CONFIG: 'config',
  CONTEXT: 'context',
} as const
export type HeadName = (typeof HEAD)[keyof typeof HEAD]

/** The synced tree's top level. */
export const HEADS = [HEAD.USERS, HEAD.WORKSPACE, HEAD.KNOWLEDGE, HEAD.CONFIG] as const
/** The heads a person's path may name (`config` is owner-tier). */
export const FILE_HEADS = [HEAD.WORKSPACE, HEAD.KNOWLEDGE, HEAD.USERS] as const
/** A user's tree: the two subdirs created for every user. */
export const USER_SUBDIRS = [HEAD.WORKSPACE, HEAD.CONTEXT] as const

/** The head an agent-relative path begins with (its first segment), or `''`. */
export function headOf(rel: string): string {
  const first = rel.split('/', 1)[0]
  return (HEADS as readonly string[]).includes(first) ? first : ''
}

/** The path lies inside somebody's tree (`users/<…>`); a bare `users` is not personal. */
export function isPersonal(rel: string): boolean {
  return rel.startsWith(HEAD.USERS + '/')
}

/** The person whose tree the path names (`users/<u>[/…]`), else `''`. */
export function userOf(rel: string): string {
  const parts = rel.replace(/^\/+/, '').split('/')
  return parts.length >= 2 && parts[0] === HEAD.USERS ? parts[1] : ''
}

/** A session's default save folder, agent-relative: `users/<u>/workspace` or `workspace`. */
export function scopeWorkspace(username: string): string {
  return username ? `${HEAD.USERS}/${username}/${HEAD.WORKSPACE}` : HEAD.WORKSPACE
}
