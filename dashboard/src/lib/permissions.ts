import type { User } from '../api/auth'

/**
 * 3-tier per-agent role model:
 *   - manager (= owner): full control of agent behavior (config + MCPs +
 *     knowledge + service-account bindings + delegation targets)
 *   - editor (NEW): collaborative tier — RW on workspace + own user dir,
 *     RO on config + knowledge
 *   - viewer: RO across the agent (workspace + config + knowledge readable),
 *     RW only on own user dir
 *
 * Platform admin overrides every per-agent role.
 */

/**
 * Owner-tier check: can the user CHANGE this agent's behavior?
 * (config edits, MCP wiring, knowledge curation, service-account binding,
 * delegation targets, MCP install requests.)
 *
 * Admin or per-agent 'manager' role only. Editors are NOT included —
 * use `canEditAgent` for workspace-collaboration checks.
 */
export function canManageAgent(user: User | null, agent: string): boolean {
  if (!user) return false
  if (user.role === 'admin') return true
  return user.agent_roles?.[agent] === 'manager'
}

/**
 * Editor-tier check: can the user WRITE to the agent's shared workspace,
 * create their own agent-scope tasks/notifications/triggers, edit files in
 * `/workspace/`? True for admin + per-agent 'manager' + per-agent 'editor'.
 *
 * Viewers excluded — they're read-only collaborators.
 */
export function canEditAgent(user: User | null, agent: string): boolean {
  if (!user) return false
  if (user.role === 'admin') return true
  const role = user.agent_roles?.[agent]
  return role === 'manager' || role === 'editor'
}
