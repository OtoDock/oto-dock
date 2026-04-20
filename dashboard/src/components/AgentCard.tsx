import { Link } from 'react-router-dom'
import type { AgentSummary } from '../api/agents'
import { useRemoteMachines } from '../api/remoteMachines'
import { MODE_LABEL, modeOfAgent } from '../lib/visibility'
import RemoteBadge from './RemoteBadge'

function getInitials(name: string): string {
  return name.slice(0, 2).toUpperCase()
}

interface Props {
  agent: AgentSummary
  isDefault: boolean
  onSetDefault: (name: string) => void
}

export default function AgentCard({ agent, isDefault, onSetDefault }: Props) {
  // If agent has a remote target configured, look up the live status so the
  // badge reflects reality (admin-visible). The query is role-gated inside
  // useRemoteMachines — non-admins never hit the admin endpoint and get no
  // badge (fine — viewers shouldn't see a stale dot).
  const { data: machines } = useRemoteMachines()
  const agentMachine =
    agent.execution_target && agent.execution_target !== 'local'
      ? machines?.find(m => m.id === agent.execution_target) ?? null
      : null

  // Visibility mode. Badge only the three non-default modes — the common
  // Personal + shared needs no annotation.
  const mode = modeOfAgent(agent)

  const handleStarClick = (e: React.MouseEvent) => {
    e.preventDefault()  // Don't navigate (card is a Link)
    e.stopPropagation()
    if (!isDefault) onSetDefault(agent.name)
  }

  return (
    <Link
      to={`/chat/${agent.name}`}
      className="block bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-5 hover:shadow-md transition-shadow relative"
    >
      {/* Default agent star — top right */}
      <button
        onClick={handleStarClick}
        className={`absolute top-3 right-3 p-1 rounded-lg transition-colors ${
          isDefault
            ? 'text-brand cursor-default'
            : 'text-p-text-light/40 hover:text-brand/60 hover:bg-brand-50'
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
          className="w-10 h-10 rounded-full flex items-center justify-center text-white text-sm font-bold shrink-0"
          style={{ backgroundColor: agent.color || '#6B7280' }}
        >
          {getInitials(agent.name)}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <h3 className="text-sm font-semibold text-p-text truncate">
              {agent.display_name || agent.name.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
            </h3>
            {agentMachine && (
              <RemoteBadge
                state={(agentMachine.status as any) ?? null}
                machineName={agentMachine.name}
                lastSeenIso={agentMachine.last_seen}
                heartbeatAgeS={agentMachine.last_heartbeat_age_s ?? null}
                size="xs"
              />
            )}
          </div>
          {agent.display_name && agent.display_name !== agent.name && (
            <span className="text-xs text-p-text-light truncate block">{agent.name}</span>
          )}
          {mode !== 'personal_shared' && (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded-sm text-xs text-p-text-secondary bg-p-surface">
              {MODE_LABEL[mode]}
            </span>
          )}
        </div>
      </div>

      {/* Description */}
      {agent.description && (
        <p className="text-xs text-p-text-light line-clamp-2 mb-3">{agent.description}</p>
      )}

      {/* Stats */}
      <div className="flex gap-4 text-xs text-p-text-secondary">
        <span>{agent.mcp_count} MCPs</span>
        <span>{agent.schedule_count} Schedules</span>
        <span>{agent.trigger_count} Triggers</span>
      </div>
    </Link>
  )
}
