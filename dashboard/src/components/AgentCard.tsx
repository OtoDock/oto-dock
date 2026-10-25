import { Link } from 'react-router-dom'
import type { AgentSummary } from '../api/agents'
import { useRemoteMachines } from '../api/remoteMachines'
import { isLocalTarget } from '../lib/placement'
import { MODE_LABEL, modeOfAgent } from '../lib/visibility'
import RemoteBadge from './RemoteBadge'
import { BADGE_BASE, BADGE_TONE, CARD, CARD_BORDERED } from './ui/classes'

function getInitials(name: string): string {
  return name.slice(0, 2).toUpperCase()
}

interface Props {
  agent: AgentSummary
  isDefault: boolean
  onSetDefault: (name: string) => void
  /** Department role (level name) when the card renders inside a
   *  department group on the grid — e.g. "Head", "Senior". */
  roleLabel?: string
  /** `card` on the page (raised), `item` inside a group (on the page's
   *  own colour with a hairline). */
  surface?: 'card' | 'item'
}

const SURFACE = {
  card: `${CARD} hover:shadow-elevation-2 transition-shadow`,
  item: `${CARD_BORDERED} hover:border-default hover:bg-surface-hover transition-colors`,
} as const

export default function AgentCard({ agent, isDefault, onSetDefault, roleLabel, surface = 'card' }: Props) {
  // If agent has a remote target configured, look up the live status so the
  // badge reflects reality (admin-visible). The query is role-gated inside
  // useRemoteMachines — non-admins never hit the admin endpoint and get no
  // badge (fine — viewers shouldn't see a stale dot).
  const { data: machines } = useRemoteMachines()
  const agentMachine =
    !isLocalTarget(agent.execution_target)
      ? machines?.find(m => m.id === agent.execution_target) ?? null
      : null

  const mode = modeOfAgent(agent)

  const handleStarClick = (e: React.MouseEvent) => {
    e.preventDefault()  // Don't navigate (card is a Link)
    e.stopPropagation()
    if (!isDefault) onSetDefault(agent.name)
  }

  return (
    <Link
      to={`/chat/${agent.name}`}
      className={`${SURFACE[surface]} block p-5 relative`}
    >
      {/* Default agent star — top right */}
      <button
        onClick={handleStarClick}
        className={`absolute top-3 right-3 p-1 rounded-control transition-colors ${
          isDefault
            ? 'text-link cursor-default'
            : 'text-tertiary hover:text-link hover:bg-action-subtle'
        }`}
        title={isDefault ? 'Default agent' : 'Set as default agent'}
      >
        {isDefault ? (
          // Filled star
          <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
          </svg>
        ) : (
          // Outlined star
          <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z" />
          </svg>
        )}
      </button>

      {/* Header */}
      <div className="flex items-center gap-3 mb-3 pr-8">
        <div
          // eslint-disable-next-line otodock/no-raw-palette -- white initials on the agent's own colour
          className="w-10 h-10 rounded-full flex items-center justify-center text-white text-sm font-bold shrink-0"
          style={{ backgroundColor: agent.color || 'var(--color-accent-agent-fill)' }}
        >
          {getInitials(agent.name)}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <h3 className="text-sm font-semibold text-primary truncate">
              {agent.display_name || agent.name.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
            </h3>
            {agentMachine && (
              <RemoteBadge
                state={agentMachine.status ?? null}
                machineName={agentMachine.name}
                lastSeenIso={agentMachine.last_seen}
                heartbeatAgeS={agentMachine.last_heartbeat_age_s ?? null}
                size="xs"
              />
            )}
          </div>
          {agent.display_name && agent.display_name !== agent.name && (
            <span className="text-xs text-tertiary truncate block">{agent.name}</span>
          )}
          <span className="flex items-center gap-1.5 flex-wrap">
            <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
              {MODE_LABEL[mode]}
            </span>
            {roleLabel && (
              // The house label badge (DepartmentsEditor's "View only", the
              // AI-engines provider tag), on the muted fill.
              <span className="shrink-0 text-3xs font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-surface-muted text-secondary">
                {roleLabel}
              </span>
            )}
          </span>
        </div>
      </div>

      {/* Description */}
      {agent.description && (
        <p className="text-xs text-tertiary line-clamp-2 mb-3">{agent.description}</p>
      )}

      {/* Stats */}
      <div className="flex gap-4 text-xs text-secondary">
        <span>{agent.mcp_count} MCPs</span>
        <span>{agent.schedule_count} Schedules</span>
        <span>{agent.trigger_count} Triggers</span>
      </div>
    </Link>
  )
}
