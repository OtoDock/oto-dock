import type { AgentSummary } from '../../../api/agents'

interface Props {
  agentSlug?: string | null
  agents?: AgentSummary[]
}

/** "personal-assistant-lite" → "Personal Assistant Lite" (chat-header fallback). */
export function titleCaseSlug(slug: string): string {
  return slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/**
 * Sender identity row for a notification card — the chat header's agent
 * avatar (colored circle + initial) and display name, at panel scale.
 *
 * Resolves `agentSlug` against the cached agents list (`useAgents`); an
 * unresolvable slug falls back to a title-cased slug on the brand color so
 * rows from agents the viewer can't open — and rows that predate the list —
 * still render. Without a slug (platform / system notifications) it renders
 * a neutral "OtoDock" header, so every card has the same first row.
 */
export default function NotificationAgentHeader({ agentSlug, agents }: Props) {
  if (!agentSlug) {
    return (
      <div
        className="flex items-center gap-1.5 min-w-0"
        data-testid="notification-agent-header"
        data-agent=""
      >
        <span className="flex items-center justify-center w-4 h-4 rounded-full bg-p-text-light/70 text-white shrink-0">
          <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
            <path
              strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5}
              d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
            />
          </svg>
        </span>
        <span className="text-[11px] font-medium text-p-text-secondary truncate">OtoDock</span>
      </div>
    )
  }

  const agent = agents?.find((a) => a.name === agentSlug)
  const displayName = agent?.display_name || titleCaseSlug(agentSlug)
  const color = agent?.color || ''
  const initial = agentSlug.charAt(0).toUpperCase()

  return (
    <div
      className="flex items-center gap-1.5 min-w-0"
      data-testid="notification-agent-header"
      data-agent={agentSlug}
    >
      <span
        className={`flex items-center justify-center w-4 h-4 rounded-full text-white text-[10px] font-semibold leading-none shrink-0 ${color ? '' : 'bg-brand'}`}
        style={color ? { backgroundColor: color } : undefined}
      >
        {initial}
      </span>
      <span className="text-[11px] font-medium text-p-text-secondary truncate">{displayName}</span>
    </div>
  )
}
