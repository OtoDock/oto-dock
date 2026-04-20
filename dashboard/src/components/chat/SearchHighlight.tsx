import { memo, useEffect } from 'react'
import { useSearch } from '../../contexts/SearchContext'

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

interface Props {
  text: string
  matchId: string              // unique id for this block in the registry
  order: number                // explicit sort key for correct ordering (msgIdx * 1000 + blockIdx)
  inUserBubble?: boolean       // adjusts highlight colors for white-on-blue
}

function SearchHighlightInner({ text, matchId, order, inUserBubble }: Props) {
  const { query, currentMatch, registerMatches, unregisterMatches, getGlobalOffset } = useSearch()

  const hasQuery = query.length > 0
  const escaped = hasQuery ? escapeRegex(query) : ''
  const regex = hasQuery ? new RegExp(`(${escaped})`, 'gi') : null
  const parts = regex ? text.split(regex) : [text]
  // Count actual matches (odd-indexed parts are matches from split-with-capture)
  const matchCount = regex ? parts.filter((_, i) => i % 2 === 1).length : 0

  useEffect(() => {
    if (hasQuery) {
      registerMatches(matchId, matchCount, order)
      return () => unregisterMatches(matchId)
    } else {
      unregisterMatches(matchId)
    }
  }, [hasQuery, matchId, matchCount, order, registerMatches, unregisterMatches])

  if (!hasQuery || matchCount === 0) {
    return <>{text}</>
  }

  const globalOffset = getGlobalOffset(matchId)
  let localIdx = 0

  // Colors: inside user bubble (white on brand blue) vs assistant message
  const normalCls = inUserBubble
    ? 'bg-white/30 rounded-sm'
    : 'bg-brand-100 rounded-sm'
  const activeCls = inUserBubble
    ? 'bg-white/50 ring-1 ring-white/70 rounded-sm'
    : 'bg-brand/20 ring-1 ring-brand rounded-sm'

  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 0) {
          // Non-match segment
          return part ? <span key={i}>{part}</span> : null
        }
        // Match segment
        const globalIdx = globalOffset + localIdx
        const isActive = globalIdx === currentMatch
        localIdx++
        return (
          <mark
            key={i}
            data-search-match="true"
            data-search-index={globalIdx}
            className={`${isActive ? activeCls : normalCls} text-inherit`}
          >
            {part}
          </mark>
        )
      })}
    </>
  )
}

export default memo(SearchHighlightInner)
