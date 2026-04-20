import { useState } from 'react'

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
  toolInput?: any
  /** Foreground subagent's final report, when the turn attached one. */
  toolResult?: string
}

const TYPE_BADGES: Record<string, { label: string; color: string }> = {
  'general-purpose': { label: 'General', color: 'bg-p-surface text-p-text' },
  'Explore': { label: 'Explore', color: 'bg-p-success/10 text-p-success' },
  'Plan': { label: 'Plan', color: 'bg-p-accent-purple/10 text-p-accent-purple' },
}

export default function SubagentInfo({ description, subagentType, isActive, failed, background, toolInput, toolResult }: Props) {
  const [expanded, setExpanded] = useState(false)
  const badge = TYPE_BADGES[subagentType || 'general-purpose'] || TYPE_BADGES['general-purpose']
  const prompt = typeof toolInput?.prompt === 'string' ? toolInput.prompt : ''
  const expandable = !!prompt || !!toolResult

  // Background agents use amber accent, foreground use brand blue
  const accent = background
    ? { bg: 'bg-p-accent-yellow/10', text: 'text-p-accent-yellow', border: 'border-p-accent-yellow' }
    : { bg: 'bg-brand-50', text: 'text-brand', border: 'border-brand' }

  return (
    <div className={`my-1.5 rounded-lg ${accent.bg} text-xs ${accent.text} overflow-hidden`}>
      <div
        className={`flex items-center gap-2 py-1.5 px-2 ${expandable ? 'cursor-pointer hover:bg-black/5 dark:hover:bg-white/5' : ''}`}
        onClick={expandable ? () => setExpanded(!expanded) : undefined}
      >
        {expandable && (
          <span className={`shrink-0 text-[10px] transform transition-transform ${expanded ? 'rotate-90' : ''}`}>
            &#9654;
          </span>
        )}
        <span className="shrink-0">
          {isActive ? (
            <span className={`inline-block w-3 h-3 border-2 ${accent.border} border-t-transparent rounded-full animate-spin`} />
          ) : failed ? (
            <span className="text-p-error" title="Stopped before completion">&#10007;</span>
          ) : (
            <span className={accent.text}>&#10003;</span>
          )}
        </span>
        <span className={`shrink-0 px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${badge.color}`}>
          {badge.label}
        </span>
        <span className="truncate">{description}</span>
      </div>
      {expanded && expandable && (
        <div className="border-t border-black/10 dark:border-white/10 px-3 py-2 text-xs font-mono text-p-text space-y-2">
          {prompt && (
            <div>
              <div className="text-p-text-secondary font-sans font-medium mb-1">Prompt</div>
              <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
                {prompt}
              </pre>
            </div>
          )}
          {toolResult && (
            <div>
              <div className="text-p-text-secondary font-sans font-medium mb-1">Report</div>
              <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-80 overflow-y-auto">
                {toolResult}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
