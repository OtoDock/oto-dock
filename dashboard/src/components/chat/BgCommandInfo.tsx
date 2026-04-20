import { useState } from 'react'

interface Props {
  command: string
  description?: string
  isActive?: boolean
  /** Set when the user stopped generation while this command was still
   * running — renders a red X instead of the green check so it's clear
   * the command didn't finish normally. */
  failed?: boolean
  /** Borrowed from the paired Bash tool block (same tool_use_id) — the
   * pairing hides that block so this pill is the command's ONE card. */
  toolInput?: any
  toolResult?: string
}

/**
 * Inline block for a `run_in_background` Bash command — mirrors SubagentInfo but
 * for shell commands (green/terminal accent). Spinner while running, green check
 * when the background task completes, red X if the turn was stopped first.
 * Expands to the full command + the spawn result borrowed from the paired tool
 * block (`pairBgCommandBlocks` hides that block, so there's one pill, not two).
 */
export default function BgCommandInfo({ command, description, isActive, failed, toolInput, toolResult }: Props) {
  const [expanded, setExpanded] = useState(false)
  const label = description || command || 'Background command'
  // The paired Bash tool block's input is the authoritative command — rows from
  // older proxies carry the DESCRIPTION in `command` (both event fields held the
  // same string), so the pairing wins whenever it's available.
  const fullCommand = (typeof toolInput?.command === 'string' && toolInput.command)
    || (command !== description ? command : '')
  const expandable = !!fullCommand || !!toolResult

  return (
    <div className="my-1.5 rounded-lg bg-[#10b981]/10 text-xs text-[#059669] overflow-hidden">
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
            <span className="inline-block w-3 h-3 border-2 border-[#10b981] border-t-transparent rounded-full animate-spin" />
          ) : failed ? (
            <span className="text-p-error" title="Stopped before completion">&#10007;</span>
          ) : (
            <span className="text-[#059669]">&#10003;</span>
          )}
        </span>
        <span className="shrink-0 px-1.5 py-0.5 rounded-sm text-[10px] font-medium bg-[#10b981]/15 text-[#059669]">
          bash
        </span>
        {/* Expanding un-truncates the description so the full sentence wraps
            in place, reading above the command in the body. */}
        <span
          className={`font-mono text-[11px] ${expanded ? 'whitespace-normal break-words min-w-0' : 'truncate'}`}
          title={label}
        >
          {label}
        </span>
      </div>
      {expanded && expandable && (
        <div className="border-t border-black/10 dark:border-white/10 px-3 py-2 text-xs font-mono text-p-text space-y-2">
          {fullCommand && (
            <pre className="whitespace-pre-wrap bg-p-surface dark:bg-p-bg rounded-sm px-2 py-1.5 max-h-60 overflow-y-auto">
              {fullCommand}
            </pre>
          )}
          {toolResult && (
            <div>
              <div className="text-p-text-secondary font-sans font-medium mb-1">Output</div>
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
