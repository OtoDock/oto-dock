/**
 * What the template panel says about what a template is (an agent, or a
 * department and how many agents), the line of a member agent, a member's
 * choice of an agent the person has, the new agents' IDs, and how a
 * department is set up.
 */

import type { SetMemberOption, SetPreviewMember } from '../../api/communityAgents'
import { MODE_SETUP, type DepartmentMode } from '../kinds/department'
import { namesInProse } from './templateApps'

export function departmentTeamLine(agents: number): string {
  return agents === 1 ? 'One agent, installed as a department.' : `${agents} agents, installed together as one team.`
}

/** The small title above a department's team. */
export function departmentAgentsTitle(agents: number): string {
  return agents === 1 ? '1 agent in this department' : `${agents} agents in this department`
}

/** The template panel's line under the name: what it is. */
export function templateWhatLine(kind: 'agent' | 'department', agents = 1): string {
  return kind === 'department' ? departmentTeamLine(agents) : 'One agent, installed on its own.'
}

/** A member agent's line: the department it belongs to. */
export function memberOfLine(department: string): string {
  return `Part of the ${department} department.`
}

/** How the department is set up, one short line each: the head's library,
 *  the paused briefings, how the levels hand work on. The install dialog's
 *  folded line; the store does not show it. */
export function departmentSetupLines(p: {
  library: { subdir?: string; writable?: boolean } | null
  schedules: unknown[]
  mode: string
}): string[] {
  const out: string[] = []
  if (p.library) {
    const where = p.library.subdir ? `${p.library.subdir} folder` : 'knowledge folder'
    out.push(`The team can read ${p.library.writable ? 'and change ' : ''}the head's ${where}.`)
  }
  if (p.schedules.length) {
    out.push(p.schedules.length === 1
      ? 'One briefing stays paused until you start it.'
      : `${p.schedules.length} briefings stay paused until you start them.`)
  }
  const levels = MODE_SETUP[p.mode as DepartmentMode]
  if (levels) out.push(levels)
  return out
}

/** One option of a member's choice: an agent the person may use, and where it is. */
export function optionLabel(o: SetMemberOption): string {
  const use = `Use ${o.display_name} (${o.slug})`
  return o.department ? `${use}, from ${o.department}` : use
}

/** An install job's lines: those that say something was not done (the
 *  job's `not_done` places, shown) and the rest (folded under Details). */
export function jobLines(job: { lines: string[]; not_done?: number[] } | undefined): { notDone: string[]; rest: string[] } {
  const places = new Set(job?.not_done ?? [])
  const lines = job?.lines ?? []
  return { notDone: lines.filter((_, i) => places.has(i)), rest: lines.filter((_, i) => !places.has(i)) }
}

/** The new agents whose IDs are not their templates' own, in one line ('' when none). */
export function newIdsLine(members: Pick<SetPreviewMember, 'action' | 'slug' | 'template'>[]): string {
  const ids = members.filter((m) => m.action === 'new' && m.slug !== m.template).map((m) => m.slug)
  if (!ids.length) return ''
  return `${ids.length === 1 ? 'New ID' : 'New IDs'}: ${namesInProse(ids)}.`
}
