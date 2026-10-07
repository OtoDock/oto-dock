import type { User } from '../api/auth'

/**
 * The role vocabulary — the dashboard mirror of `proxy/auth/roles.py`
 * (core-seams phase 5). The proxy's `tests/auth/test_roles.py` reads this
 * file and keeps the members, the tiers and the ranks in lock-step; its
 * acceptance test keeps a quoted role out of every other file's branches,
 * so the dashboard compares `ROLE.*` and asks the questions below.
 *
 * Two independent axes: the PLATFORM role (`users.role`: admin / creator /
 * member — what a person may do platform-wide) and the per-AGENT role
 * (`user_agents.agent_role`: manager / editor / contributor / viewer — what
 * they may do on one agent). A platform admin is `admin` on every agent; the
 * role an admitted principal acts with is the admin's, else the per-agent
 * row, else viewer.
 *
 * The per-agent tiers:
 *   - manager (= owner): full control of agent behavior (config + MCPs +
 *     knowledge + service-account bindings + delegation targets)
 *   - editor: acts as the agent — own agent-scope tasks, triggers and
 *     notifications, agent-scope delegation, the shared apps; RW on the
 *     workspace + own user dir, RO on config + knowledge
 *   - contributor: RW on the workspace + own user dir and nothing under
 *     the agent's identity; RO on knowledge, shared memory read-only
 *   - viewer: RO across the agent (workspace + config + knowledge readable),
 *     RW only on own user dir
 */

export const ROLE = {
  ADMIN: 'admin',
  CREATOR: 'creator',
  MEMBER: 'member',
  MANAGER: 'manager',
  EDITOR: 'editor',
  CONTRIBUTOR: 'contributor',
  VIEWER: 'viewer',
} as const

export type PlatformRole = 'admin' | 'creator' | 'member'
export type AgentRole = 'manager' | 'editor' | 'contributor' | 'viewer'
/** The role a principal acts with on one agent: the admin's, or a per-agent row. */
export type EffectiveRole = AgentRole | 'admin'
/** The floors a manifest may name for an action or an export (the default floor is every viewer). */
export type ActionFloor = Exclude<AgentRole, 'viewer'>

/** The `users.role` CHECK, in its order. */
export const PLATFORM_ROLES: readonly PlatformRole[] = ['admin', 'creator', 'member']
/** The `user_agents.agent_role` CHECK, in its order. */
export const AGENT_ROLES: readonly AgentRole[] = ['manager', 'editor', 'contributor', 'viewer']
/** The effective roles by rank — the order an action floor is judged in. */
export const EFFECTIVE_ROLES: readonly EffectiveRole[] = ['viewer', 'contributor', 'editor', 'manager', 'admin']
/** The platform roles by rank. */
export const PLATFORM_BY_RANK: readonly PlatformRole[] = ['member', 'creator', 'admin']

/** Curates config and knowledge; wires MCPs and bindings. */
export const OWNER_TIER: readonly EffectiveRole[] = ['manager', 'admin']
/** Acts as the agent: its own agent-scope automations, the shared apps. */
export const EDITOR_TIER: readonly EffectiveRole[] = ['manager', 'editor', 'admin']
/** Writes the shared workspace. */
export const WORKSPACE_TIER: readonly EffectiveRole[] = ['manager', 'editor', 'contributor', 'admin']
/** Creates agents; reaches the creator surfaces. */
export const CREATOR_TIER: readonly PlatformRole[] = ['admin', 'creator']
/** The per-agent roles a Shared-only agent's rows may hold (its sessions run as the agent). */
export const SHARED_ONLY_ROLES: readonly AgentRole[] = ['manager', 'editor']

/** A word outside the table ranks 0: it clears the viewer floor and no other. */
export const ROLE_RANK: Record<string, number> = Object.fromEntries(EFFECTIVE_ROLES.map((r, i) => [r, i]))
export const PLATFORM_RANK: Record<string, number> = Object.fromEntries(PLATFORM_BY_RANK.map((r, i) => [r, i]))

type RoleBearer = { role?: string | null } | string | null | undefined

function roleOf(subject: RoleBearer): string {
  if (typeof subject === 'string') return subject
  return subject?.role ?? ''
}

/** A platform admin. Takes the user, or a bare role word. */
export function isAdmin(subject: RoleBearer): boolean {
  return roleOf(subject) === ROLE.ADMIN
}

/** Admin or creator: the platform roles that create agents and reach the creator surfaces. */
export function isCreatorOrAbove(subject: RoleBearer): boolean {
  return (CREATOR_TIER as readonly string[]).includes(roleOf(subject))
}

/** Whether an assignment of `role` may be written on a Shared-only agent. */
export function allowedOnSharedOnly(role: string | null | undefined): boolean {
  return (SHARED_ONLY_ROLES as readonly string[]).includes(role || '')
}

/**
 * Owner-tier check: can the user CHANGE this agent's behavior?
 * (config edits, MCP wiring, knowledge curation, service-account binding,
 * delegation targets, MCP install requests.)
 *
 * Admin or per-agent 'manager' role only. Editors are NOT included —
 * use `canEditAgent` for the automation checks and `canWriteWorkspace`
 * for the shared-workspace ones.
 */
export function canManageAgent(user: User | null | undefined, agent: string): boolean {
  return (OWNER_TIER as readonly string[]).includes(actingRole(user, agent))
}

/**
 * Editor-tier check: can the user ACT AS the agent — create their own
 * agent-scope tasks/notifications/triggers, continue an agent-scope run,
 * pin and approve shared apps? True for admin + per-agent 'manager' +
 * per-agent 'editor'.
 *
 * Contributors and viewers are excluded.
 */
export function canEditAgent(user: User | null | undefined, agent: string): boolean {
  return (EDITOR_TIER as readonly string[]).includes(actingRole(user, agent))
}

/**
 * Workspace-tier check: can the user WRITE to the agent's shared workspace
 * (the file overlay's shared chip, uploads, WOPI edits)? True for admin +
 * per-agent 'manager' + 'editor' + 'contributor'.
 *
 * Viewers excluded — they're read-only collaborators.
 */
export function canWriteWorkspace(user: User | null | undefined, agent: string): boolean {
  return (WORKSPACE_TIER as readonly string[]).includes(actingRole(user, agent))
}

/**
 * The role this user acts with on `agent`: `admin` for a platform admin,
 * else the per-agent row, else `viewer` (the floor of an admitted
 * principal). Mirrors the proxy's `acting_role`.
 */
export function actingRole(user: User | null | undefined, agent: string): EffectiveRole {
  if (!user) return ROLE.VIEWER
  if (isAdmin(user)) return ROLE.ADMIN
  return user.agent_roles?.[agent] ?? ROLE.VIEWER
}

export function rank(role: string | null | undefined): number {
  return ROLE_RANK[role ?? ''] ?? 0
}

/**
 * Whether `role` clears the action's floor (the server judges the same way):
 * an empty floor is the viewer floor; a floor the table does not know is
 * unreachable.
 */
export function meetsFloor(action: { min_role?: string | null }, role: string | null | undefined): boolean {
  const wanted = action.min_role || ROLE.VIEWER
  return wanted in ROLE_RANK && rank(role) >= ROLE_RANK[wanted]
}

const BADGE: Record<string, string> = {
  admin: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400',
  creator: 'bg-brand-100 text-brand',
  member: 'bg-p-surface text-p-text-secondary',
  manager: 'bg-brand-100 text-brand',
  editor: 'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300',
  contributor: 'bg-teal-100 dark:bg-teal-900/30 text-teal-700 dark:text-teal-300',
  viewer: 'bg-gray-100 dark:bg-gray-800 text-p-text-secondary',
}

const LABEL: Record<string, string> = {
  admin: 'Admin',
  creator: 'Creator',
  member: 'Member',
  manager: 'Manager',
  editor: 'Editor',
  contributor: 'Contributor',
  viewer: 'Viewer',
}

/** The badge classes for a role; an unknown word wears the viewer's. */
export function roleBadge(role: string | null | undefined): string {
  return BADGE[role ?? ''] ?? BADGE.viewer
}

/** The capitalised word a chip shows; an unknown word reads Viewer. */
export function roleLabel(role: string | null | undefined): string {
  return LABEL[role ?? ''] ?? LABEL.viewer
}
