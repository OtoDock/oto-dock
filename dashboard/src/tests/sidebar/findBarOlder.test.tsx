import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { useState, type ReactNode } from 'react'

// ─── The find bar says what it cannot see, and looks further back ────────────

import FindBar, { SEEK_PAGES } from '@/components/chat/FindBar'
import { SearchContext, type SearchContextValue } from '@/contexts/useSearch'
import type { OlderPage } from '@/hooks/useChatMessages'

// A search context the test drives: the match count, and what the bar sets.
let setTotal: (n: number) => void = () => {}
const setCurrent = vi.fn()
function Context({ children, current = 0 }: { children: ReactNode; current?: number }) {
  const [total, setT] = useState(0)
  setTotal = setT
  const value: SearchContextValue = {
    query: 'needle', currentMatch: current, totalMatches: total, setCurrentMatch: setCurrent,
    nextMatch: () => {}, prevMatch: () => {}, registerMatches: () => {}, unregisterMatches: () => {},
    getGlobalOffset: () => 0,
  }
  return <SearchContext.Provider value={value}>{children}</SearchContext.Provider>
}

const scrolled = vi.fn()
beforeEach(() => {
  setCurrent.mockReset()
  scrolled.mockReset()
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolled(this.getAttribute('data-search-index'))
  }
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0))
  vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id))
})
afterEach(() => vi.unstubAllGlobals())

async function frames() {
  await act(async () => { await new Promise((r) => setTimeout(r, 5)) })
}

function bar(props: Partial<Parameters<typeof FindBar>[0]> = {}) {
  return (
    <Context>
      <FindBar value="needle" onChange={() => {}} onClose={() => {}} {...props} />
      <div data-search-index="0">match</div>
    </Context>
  )
}

describe('FindBar and older messages', () => {
  it('says older messages are not loaded while the chat has more', () => {
    const { rerender } = render(bar({ canLoadOlder: true }))
    expect(screen.getByText('Older messages are not loaded.')).toBeTruthy()
    rerender(bar({ canLoadOlder: false }))
    expect(screen.queryByTestId('find-older')).toBeNull()
    rerender(<Context><FindBar value="" onChange={() => {}} onClose={() => {}} canLoadOlder /></Context>)
    expect(screen.queryByTestId('find-older')).toBeNull()
  })

  it('pages back until a match shows, then moves to it and scrolls there even at the same index', async () => {
    let pages = 0
    const onLoadOlder = vi.fn(async (): Promise<OlderPage> => {
      pages += 1
      if (pages === 3) setTotal(1)   // the third page holds the word
      return 'landed'
    })
    render(bar({ canLoadOlder: true, onLoadOlder }))
    fireEvent.click(screen.getByText('Search them'))
    expect(screen.getByText('Loading older messages…')).toBeTruthy()
    for (let i = 0; i < 8; i++) await frames()
    expect(onLoadOlder).toHaveBeenCalledTimes(3)
    expect(setCurrent).toHaveBeenLastCalledWith(0)
    expect(scrolled).toHaveBeenCalledWith('0')
    expect(screen.getByText('Search them')).toBeTruthy()
  })

  it('stops at the chat start, after a failed page, and after SEEK_PAGES pages', async () => {
    const empty = vi.fn(async (): Promise<OlderPage> => 'nothing')
    const view = render(bar({ canLoadOlder: true, onLoadOlder: empty }))
    fireEvent.click(screen.getByText('Search them'))
    for (let i = 0; i < 4; i++) await frames()
    expect(empty).toHaveBeenCalledTimes(1)
    view.unmount()

    const endless = vi.fn(async (): Promise<OlderPage> => 'landed')
    render(bar({ canLoadOlder: true, onLoadOlder: endless }))
    fireEvent.click(screen.getByText('Search them'))
    for (let i = 0; i < SEEK_PAGES * 3 + 10; i++) await frames()
    expect(endless).toHaveBeenCalledTimes(SEEK_PAGES)
    expect(scrolled).not.toHaveBeenCalled()
  })

  it('waits for a page another loader holds, then asks again', async () => {
    const answers: OlderPage[] = ['stale', 'nothing']
    const onLoadOlder = vi.fn(async (): Promise<OlderPage> => answers.shift() ?? 'nothing')
    render(bar({ canLoadOlder: true, onLoadOlder }))
    fireEvent.click(screen.getByText('Search them'))
    await frames()
    expect(onLoadOlder).toHaveBeenCalledTimes(1)
    await act(async () => { await new Promise((r) => setTimeout(r, 350)) })
    expect(onLoadOlder).toHaveBeenCalledTimes(2)
  })

  it('starts at no match while a search result\'s anchor is on its way, then takes the anchored bubble\'s first match', async () => {
    const { rerender } = render(
      <Context>
        <FindBar value="needle" onChange={() => {}} onClose={() => {}} anchorPending />
        <div data-msg-id="db-10"><span data-search-index="0" /></div>
        <div data-msg-id="db-41"><span data-search-index="3" /><span data-search-index="2" /></div>
      </Context>,
    )
    expect(setCurrent).toHaveBeenLastCalledWith(-1)
    rerender(
      <Context>
        <FindBar value="needle" onChange={() => {}} onClose={() => {}} anchorLanded={{ bubbleId: 'db-41' }} />
        <div data-msg-id="db-10"><span data-search-index="0" /></div>
        <div data-msg-id="db-41"><span data-search-index="3" /><span data-search-index="2" /></div>
      </Context>,
    )
    await frames()
    expect(setCurrent).toHaveBeenLastCalledWith(2)
  })

  it('ignores a landing it did not wait for', async () => {
    render(
      <Context>
        <FindBar value="needle" onChange={() => {}} onClose={() => {}} anchorLanded={{ bubbleId: 'db-41' }} />
        <div data-msg-id="db-41"><span data-search-index="2" /></div>
      </Context>,
    )
    await frames()
    expect(setCurrent).not.toHaveBeenCalled()
  })
})
