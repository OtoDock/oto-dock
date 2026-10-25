import { useState } from 'react'
import SearchHighlight from './SearchHighlight'
import { useSearch } from '../../contexts/useSearch'


interface Props {
  content: string
  collapsed?: boolean
  /** True once the thinking phase has ended. Some providers stop emitting
   * thinking-token deltas but still fire start + end events; the block needs
   * an explicit "done" signal to swap the "Thinking..." pulse for a static
   * "Thought" badge, otherwise the spinner stays forever (content never
   * arrives to satisfy the `hasContent` heuristic). */
  done?: boolean
  /** Live ~token estimate for content-less thinking (when adaptive effort
   * hides the thinking text, the CLI's `thinking_tokens` pings ride progress
   * events instead). Display-only. */
  tokens?: number
  blockId?: string
  blockOrder?: number
}

export default function ThinkingBlock({ content, collapsed: externalCollapsed = true, done = false, tokens = 0, blockId, blockOrder = 0 }: Props) {
  const [collapsed, setCollapsed] = useState(externalCollapsed)
  const { query, currentMatch, getGlobalOffset } = useSearch()

  // Auto-collapse when external state changes (thinking phase ends)
  const [syncedCollapsed, setSyncedCollapsed] = useState(externalCollapsed)
  if (syncedCollapsed !== externalCollapsed) {
    setSyncedCollapsed(externalCollapsed)
    setCollapsed(externalCollapsed)
  }

  const hasContent = content.length > 0
  const matchId = blockId ? `${blockId}-think` : ''

  // The block is held open while the current search match is inside it
  if (query && matchId && hasContent && collapsed) {
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const count = (content.match(new RegExp(escaped, 'gi')) || []).length
    if (count > 0) {
      const offset = getGlobalOffset(matchId)
      if (currentMatch >= offset && currentMatch < offset + count) setCollapsed(false)
    }
  }

  return (
    <div className="my-1.5 rounded-card border border-accent-agent-border bg-accent-agent-bg overflow-hidden">
      <button
        onClick={() => hasContent && setCollapsed(!collapsed)}
        className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-accent-agent-fg transition-colors ${hasContent ? 'hover:bg-surface-hover cursor-pointer' : 'cursor-default'}`}
      >
        {hasContent ? (
          <>
            <span className={`transform transition-transform text-3xs ${collapsed ? '' : 'rotate-90'}`}>
              {'\u25B6'}
            </span>
            <span className="font-medium">Thinking</span>
            <span className="text-secondary">({content.trim().split(/\s+/).length.toLocaleString()} words)</span>
          </>
        ) : done ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-accent-agent-solid" />
            <span className="font-medium">Thought</span>
            {tokens > 0 && (
              <span className="text-secondary">(~{tokens.toLocaleString()} tokens)</span>
            )}
          </>
        ) : (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-accent-agent-solid animate-pulse" />
            <span className="font-medium animate-pulse">Thinking...</span>
            {tokens > 0 && (
              <span className="text-secondary">(~{tokens.toLocaleString()} tokens)</span>
            )}
          </>
        )}
      </button>
      {/* Always render content when search is active (so SearchHighlight registers in DOM order).
          Hidden visually when collapsed via CSS — avoids late registration causing wrong match order. */}
      {hasContent && (query ? true : !collapsed) && (
        <div className={`border-t border-accent-agent-border px-3 py-2 ${collapsed ? 'hidden' : ''}`}>
          <pre className="text-xs text-accent-agent-fg whitespace-pre-wrap max-h-60 overflow-auto leading-relaxed">
            {query && matchId ? (
              <SearchHighlight text={content} matchId={matchId} order={blockOrder} />
            ) : (
              content
            )}
          </pre>
        </div>
      )}
    </div>
  )
}

/** A progress note: the agent's words between its steps (a Codex preamble, a
 * Claude narration block). Plain faint prose in the steps group, no box;
 * searchable like the thinking text. */
export function NoteBlock({ content, blockId, blockOrder = 0 }: { content: string; blockId?: string; blockOrder?: number }) {
  const { query } = useSearch()
  const text = content.trim()
  if (!text) return null
  const matchId = blockId ? `${blockId}-note` : ''
  return (
    <p data-testid="progress-note" className="my-0.5 text-xs leading-relaxed text-tertiary whitespace-pre-wrap">
      {query && matchId ? <SearchHighlight text={text} matchId={matchId} order={blockOrder} /> : text}
    </p>
  )
}
