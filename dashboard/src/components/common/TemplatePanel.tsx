/**
 * One panel says what a community template is, for an agent and for a
 * department, in the store and in the install dialogs alike: the mark and
 * the name with a line that says what it is (a department with its badge),
 * the short text, the icon rows (the template's or the set's highlights), a
 * department's team under a small title that counts its agents, then
 * the folded lines (the tools, then the apps and checks). A store card shows
 * it contracted (`card`); Show details and the install dialogs in full
 * (`full`), each adding its own parts: the install dialogs their fields in
 * `slot` and the approval inside the apps line, the store its buttons.
 */

import type { ReactNode } from 'react'
import { departmentAgentsTitle, templateWhatLine } from '../../lib/status/departmentSet'
import DepartmentTeam, { type TeamMember } from '../departments/DepartmentTeam'
import FeatureList from './FeatureList'
import { BADGE_BASE, BADGE_TONE } from '../ui/classes'

/** How many icon rows a contracted card shows. */
const CARD_FEATURES = 4
/** The most icon rows drawn, whatever a catalog sends. */
const MAX_FEATURES = 6

export interface PanelTeam {
  members: TeamMember[]
  levels: string[]
  colors?: Record<string, { color: string } | undefined>
  onOpen?: (template: string) => void
  /** Lines under the team: an agent renamed or already installed. */
  notes?: ReactNode
}

export default function TemplatePanel({
  kind, variant, name, color, version, author, agents = 1, summary, description, features, team,
  memberOf, slot, folded, testId = 'template-panel',
}: {
  kind: 'agent' | 'department'
  variant: 'card' | 'full'
  name: string
  color?: string
  version?: string
  author?: ReactNode
  agents?: number
  summary?: string
  description: string
  features: { icon: string; text: string }[]
  team?: PanelTeam
  /** A member agent's line, with its link to the department. */
  memberOf?: ReactNode
  /** The install dialog's own fields, between what it is and the folded lines. */
  slot?: ReactNode
  /** The folded lines: the tools, then the apps and checks. */
  folded?: ReactNode
  testId?: string
}) {
  const card = variant === 'card'
  const text = summary?.trim() || description
  const rows = features.filter(f => f && typeof f.text === 'string' && f.text.trim()).slice(0, MAX_FEATURES)
  return (
    <div className="flex flex-col gap-3" data-testid={testId}>
      <div className="flex items-start gap-3">
        <div
          // eslint-disable-next-line otodock/no-raw-palette -- white initial on the template's own colour
          className={`${card ? 'w-11 h-11 text-base' : 'w-10 h-10'} rounded-card shrink-0 flex items-center justify-center text-white font-semibold`}
          style={{ background: color || 'var(--ot-action)' }}
        >
          {name.charAt(0)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-sm font-semibold text-primary truncate">{name}</span>
            {kind === 'department' && (
              <span className={`${BADGE_BASE} ${BADGE_TONE.action} shrink-0`} data-testid="panel-badge">Department</span>
            )}
          </div>
          <div className="text-xs text-primary" data-testid="panel-what">{templateWhatLine(kind, agents)}</div>
          {(version || author) && (
            <div className="text-2xs text-tertiary">
              {version && `v${version}`}{version && author ? ' · ' : ''}{author}
            </div>
          )}
        </div>
      </div>
      <p className={card ? 'text-xs text-secondary line-clamp-2' : 'text-sm text-primary'} data-testid="panel-text">{text}</p>
      {rows.length > 0 && (
        <FeatureList items={card ? rows.slice(0, CARD_FEATURES) : rows} size={card ? 'compact' : 'list'} testId="panel-features" />
      )}
      {!card && summary?.trim() && description && (
        <p className="text-xs text-secondary" data-testid="panel-description">{description}</p>
      )}
      {team && (
        <div>
          <p className="text-xs font-medium text-secondary mb-1.5" data-testid="panel-team-title">
            {departmentAgentsTitle(team.members.length)}
          </p>
          <DepartmentTeam members={team.members} levels={team.levels} templates={team.colors} onOpen={team.onOpen} />
          {team.notes}
        </div>
      )}
      {memberOf}
      {slot}
      {!card && folded && (
        <div className="rounded-card border border-subtle p-3 space-y-3" data-testid="panel-folded">{folded}</div>
      )}
    </div>
  )
}
