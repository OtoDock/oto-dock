import { useState } from 'react'
import { inputText } from '../../lib/tools/input'

interface Props {
  description: string
  subagentType?: string
  isActive?: boolean
  /** Set when the user stopped generation while this subagent was still
   * running — renders a red X instead of the green check so it's clear
   * the subagent didn't complete normally. */
  failed?: boolean
  background?: boolean
  /** Full Agent tool input (prompt, model, …) — enables the expanded view. */
  toolInput?: unknown
  /** Foreground subagent's final report, when the turn attached one. */
  toolResult?: string
}

// The type is a word on the agent's tint, not a second accent.
const TYPE_CHIP = 'bg-surface-card text-secondary'
const AGENT_ACCENT = { bg: 'bg-accent-agent-bg', text: 'text-accent-agent-fg', border: 'border-accent-agent-solid' }

const TYPE_BADGES: Record<string, { label: string; color: string }> = {
  'general-purpose': { label: 'General', color: TYPE_CHIP },
  'Explore': { label: 'Explore', color: TYPE_CHIP },
  'Plan': { label: 'Plan', color: TYPE_CHIP },
}

export default function SubagentInfo({ description, subagentType, isActive, failed, background, toolInput, toolResult }: Props) {
  const [expanded, setExpanded] = useState(false)
  const badge = TYPE_BADGES[subagentType || 'general-purpose'] || TYPE_BADGES['general-purpose']
  const prompt = inputText(toolInput, 'prompt')
  const expandable = !!prompt || !!toolResult

  // A subagent is agent work in the foreground and in the background alike;
  // a chip says which.
  const accent = AGENT_ACCENT

  return (
    <div className={`my-1.5 rounded-card ${accent.bg} text-xs ${accent.text} overflow-hidden`}>
      <div
        className={`flex items-center gap-2 py-1.5 px-2 ${expandable ? 'cursor-pointer hover:bg-surface-hover' : ''}`}
        onClick={expandable ? () => setExpanded(!expanded) : undefined}
      >
        {expandable && (
          <span className={`shrink-0 text-3xs transform transition-transform ${expanded ? 'rotate-90' : ''}`}>
            &#9654;
          </span>
        )}
        <span className="shrink-0">
          {isActive ? (
            <span className={`inline-block w-3 h-3 border-2 ${accent.border} border-t-transparent rounded-full animate-spin`} />
          ) : failed ? (
            <span className="text-status-danger-fg" title="Stopped before completion">&#10007;</span>
          ) : (
            <span className={accent.text}>&#10003;</span>
          )}
        </span>
        <span className={`shrink-0 px-1.5 py-0.5 rounded-sm text-3xs font-medium ${badge.color}`}>
          {badge.label}
        </span>
        {background && (
          <span className={`shrink-0 px-1.5 py-0.5 rounded-sm text-3xs font-medium ${TYPE_CHIP}`}>Background</span>
        )}
        <span className="truncate">{description}</span>
      </div>
      {expanded && expandable && (
        <div className="border-t border-subtle px-3 py-2 text-xs font-mono text-primary space-y-2">
          {prompt && (
            <div>
              <div className="text-secondary font-sans font-medium mb-1">Prompt</div>
              <pre className="whitespace-pre-wrap bg-surface-sunken rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
                {prompt}
              </pre>
            </div>
          )}
          {toolResult && (
            <div>
              <div className="text-secondary font-sans font-medium mb-1">Report</div>
              <pre className="whitespace-pre-wrap bg-surface-sunken rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
                {toolResult}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
