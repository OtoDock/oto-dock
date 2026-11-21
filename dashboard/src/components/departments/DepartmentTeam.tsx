/**
 * Who a department set is made of, as a small tree: the first level (the
 * head) on top with its word, each level after it one step in, a mark and a
 * name each. With `onOpen` a name opens that agent's own panel.
 */

import type { CommunityAgentRegistryEntry } from '../../api/communityAgents'

export interface TeamMember {
  template: string
  level: string
  display_name: string
  color?: string
}

/** The members by level, in the set's order of levels; a level the set
 *  does not list comes last. */
function byLevel(members: TeamMember[], levels: string[]): TeamMember[][] {
  const order = [...levels, ...members.map(m => m.level).filter(l => !levels.includes(l))]
  return [...new Set(order)].map(l => members.filter(m => m.level === l)).filter(g => g.length > 0)
}

export default function DepartmentTeam({ members, levels, templates = {}, onOpen }: {
  members: TeamMember[]
  levels: string[]
  templates?: Record<string, Pick<CommunityAgentRegistryEntry, 'color'> | undefined>
  onOpen?: (template: string) => void
}) {
  return (
    <div className="space-y-1.5" data-testid="department-team">
      {byLevel(members, levels).map((tier, depth) => (
        <ul
          key={tier[0].level}
          className={`flex flex-wrap gap-x-3 gap-y-1 ${depth > 0 ? 'border-l border-subtle pl-3' : ''}`}
          style={depth > 1 ? { marginLeft: `${(depth - 1) * 0.75}rem` } : undefined}
        >
          {tier.map(m => {
            const name = <span className={depth === 0 ? 'font-medium text-primary' : 'text-secondary'}>{m.display_name}</span>
            return (
              <li key={m.template} className="flex items-center gap-1.5 text-xs" data-testid={`team-${m.template}`}>
                <span
                  // eslint-disable-next-line otodock/no-raw-palette -- white initial on the template's own colour
                  className="w-5 h-5 text-3xs rounded-card shrink-0 flex items-center justify-center text-white font-semibold"
                  style={{ background: m.color || templates[m.template]?.color || 'var(--ot-action)' }}
                  aria-hidden="true"
                >
                  {m.display_name.charAt(0)}
                </span>
                {onOpen
                  ? <button type="button" className="underline decoration-dotted underline-offset-2" onClick={() => onOpen(m.template)}>{name}</button>
                  : name}
                {depth === 0 && <span className="text-tertiary">{m.level}</span>}
              </li>
            )
          })}
        </ul>
      ))}
    </div>
  )
}
