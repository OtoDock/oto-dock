// The thinking block is held open while the find bar's current match sits
// inside it (a collapse re-opens at once), and a change of the collapsed
// prop re-syncs the block.
import { describe, it, expect } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import ThinkingBlock from '@/components/chat/ThinkingBlock'
import { SearchContext, type SearchContextValue } from '@/contexts/useSearch'

function search(over: Partial<SearchContextValue>): SearchContextValue {
  return {
    query: '', currentMatch: 0, totalMatches: 0, setCurrentMatch: () => {}, nextMatch: () => {},
    prevMatch: () => {}, registerMatches: () => {}, unregisterMatches: () => {}, getGlobalOffset: () => 0,
    ...over,
  }
}

function block(ctx: SearchContextValue, collapsed = true) {
  return render(
    <SearchContext.Provider value={ctx}>
      <ThinkingBlock content="the fox jumps over the dog" collapsed={collapsed} done blockId="b1" />
    </SearchContext.Provider>,
  )
}

const arrow = () => screen.getByText('▶')

describe('ThinkingBlock and the find bar', () => {
  it('stays collapsed without a query and opens on a click', () => {
    block(search({}))
    expect(arrow().className).not.toContain('rotate-90')
    fireEvent.click(screen.getByText('Thinking'))
    expect(arrow().className).toContain('rotate-90')
  })

  it('is held open while the current match is inside it', () => {
    block(search({ query: 'fox', currentMatch: 0, totalMatches: 1 }))
    expect(arrow().className).toContain('rotate-90')
    fireEvent.click(screen.getByText('Thinking'))
    expect(arrow().className).toContain('rotate-90')
  })

  it('stays collapsed when the current match is elsewhere', () => {
    block(search({ query: 'fox', currentMatch: 3, totalMatches: 4, getGlobalOffset: () => 0 }))
    expect(arrow().className).not.toContain('rotate-90')
  })

  it('re-syncs when the collapsed prop changes', () => {
    const r = block(search({}), false)
    expect(arrow().className).toContain('rotate-90')
    r.rerender(
      <SearchContext.Provider value={search({})}>
        <ThinkingBlock content="the fox jumps over the dog" collapsed done blockId="b1" />
      </SearchContext.Provider>,
    )
    expect(arrow().className).not.toContain('rotate-90')
  })
})
