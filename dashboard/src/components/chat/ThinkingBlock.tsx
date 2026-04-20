import { useState, useEffect } from 'react'
import SearchHighlight from './SearchHighlight'
import { useSearch } from '../../contexts/SearchContext'

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
  useEffect(() => {
    setCollapsed(externalCollapsed)
  }, [externalCollapsed])

  const hasContent = content.length > 0
  const matchId = blockId ? `${blockId}-think` : ''

  // If current search match is inside this block, auto-expand
  useEffect(() => {
    if (!query || !matchId || !hasContent || !collapsed) return
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const count = (content.match(new RegExp(escaped, 'gi')) || []).length
    if (count > 0) {
      const offset = getGlobalOffset(matchId)
      if (currentMatch >= offset && currentMatch < offset + count) {
        setCollapsed(false)
      }
    }
  }, [query, currentMatch, matchId, hasContent, content, collapsed, getGlobalOffset])

  return (
    <div className="my-1.5 rounded-lg border border-[#673a97]/20 bg-[#673a97]/5 overflow-hidden">
      <button
        onClick={() => hasContent && setCollapsed(!collapsed)}
        className={`w-full flex items-center gap-2 px-3 py-1.5 text-xs text-p-accent-purple transition-colors ${hasContent ? 'hover:bg-[#673a97]/10 cursor-pointer' : 'cursor-default'}`}
      >
        {hasContent ? (
          <>
            <span className={`transform transition-transform text-[10px] ${collapsed ? '' : 'rotate-90'}`}>
              {'\u25B6'}
            </span>
            <span className="font-medium">Thinking</span>
            <span className="text-p-accent-purple/60">({content.trim().split(/\s+/).length.toLocaleString()} words)</span>
          </>
        ) : done ? (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-p-accent-purple/60" />
            <span className="font-medium">Thought</span>
            {tokens > 0 && (
              <span className="text-p-accent-purple/60">(~{tokens.toLocaleString()} tokens)</span>
            )}
          </>
        ) : (
          <>
            <span className="w-1.5 h-1.5 rounded-full bg-p-accent-purple/60 animate-pulse" />
            <span className="font-medium animate-pulse">Thinking...</span>
            {tokens > 0 && (
              <span className="text-p-accent-purple/60">(~{tokens.toLocaleString()} tokens)</span>
            )}
          </>
        )}
      </button>
      {/* Always render content when search is active (so SearchHighlight registers in DOM order).
          Hidden visually when collapsed via CSS — avoids late registration causing wrong match order. */}
      {hasContent && (query ? true : !collapsed) && (
        <div className={`border-t border-[#673a97]/20 px-3 py-2 ${collapsed ? 'hidden' : ''}`}>
          <pre className="text-xs text-[#673a97] whitespace-pre-wrap max-h-60 overflow-auto leading-relaxed">
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
