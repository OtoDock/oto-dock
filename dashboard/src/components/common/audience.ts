import { ROLE, type AgentRole } from '../../lib/permissions'

/** "Who is told": the lowest per-agent role an agent-scope task's or
 *  trigger's notifications reach (the server's `notify_audience`); platform
 *  admins are always told. */
export const WHO_IS_TOLD: readonly { role: AgentRole; label: string }[] = [
  { role: ROLE.VIEWER, label: 'Everyone who uses the agent' },
  { role: ROLE.CONTRIBUTOR, label: 'Contributors, editors and managers' },
  { role: ROLE.EDITOR, label: 'Editors and managers' },
  { role: ROLE.MANAGER, label: 'Managers' },
]

/** The role a stored or missing value reads as: a row the server sent
 *  without the field tells editors and managers, the new-task default. */
export function audienceOf(value: string | null | undefined): AgentRole {
  return WHO_IS_TOLD.find((o) => o.role === value)?.role ?? ROLE.EDITOR
}
