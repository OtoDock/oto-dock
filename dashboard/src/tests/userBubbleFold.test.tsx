/**
 * A person's long message folds: past ten rendered lines (measured against
 * the text's own line height, so wrapped lines count) it shows its first
 * eight under a fade with Show more; it is measured again when its width
 * changes; a find-in-chat query that matches it opens it; the copy button
 * copies the whole text. An unknown line height never folds.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import BlockRenderer from '@/components/chat/ChatBlockRenderer'
import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage } from '@/components/chat/types'
import { SearchProvider } from '@/contexts/SearchContext'

const LINE = 20
let contentHeight = 0
let lineHeight = `${LINE}px`
const observers: Array<() => void> = []

class ResizeObserverStub {
  private cb: () => void
  constructor(cb: () => void) { this.cb = cb; observers.push(cb) }
  observe() {}
  unobserve() {}
  disconnect() { const i = observers.indexOf(this.cb); if (i >= 0) observers.splice(i, 1) }
}

const realStyle = window.getComputedStyle
beforeEach(() => {
  contentHeight = 0
  lineHeight = `${LINE}px`
  vi.stubGlobal('ResizeObserver', ResizeObserverStub)
  vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} })
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el, pseudo) => {
    const style = realStyle(el, pseudo)
    if ((el as HTMLElement).dataset?.testid === 'user-message-text') {
      return { ...style, lineHeight } as CSSStyleDeclaration
    }
    return style
  })
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) { return this.dataset.testid === 'user-message-text' ? contentHeight : 0 },
  })
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  observers.length = 0
})

const LONG = Array.from({ length: 14 }, (_, i) => `line ${i + 1} of the pasted log`).join('\n')

function renderText(text: string, query = '') {
  return render(
    <SearchProvider query={query}>
      <BlockRenderer block={{ type: 'text', content: text }} blockId="m1-b0" blockOrder={0} isUserMessage
        onPermissionRespond={() => {}} />
    </SearchProvider>,
  )
}

const textBox = () => screen.getByTestId('user-message-text')

describe("a person's long message folds", () => {
  it('past ten lines it shows eight under Show more', () => {
    contentHeight = 11 * LINE
    renderText(LONG)
    expect(textBox().style.maxHeight).toBe(`${8 * LINE}px`)
    const more = screen.getByRole('button', { name: 'Show more' })
    expect(more).toHaveAttribute('aria-expanded', 'false')
  })

  it('ten lines or fewer never fold', () => {
    contentHeight = 10 * LINE
    renderText(LONG)
    expect(textBox().style.maxHeight).toBe('')
    expect(screen.queryByRole('button', { name: /Show/ })).toBeNull()
  })

  it('opens with Show more and folds again with Show less', () => {
    contentHeight = 30 * LINE
    renderText(LONG)
    fireEvent.click(screen.getByRole('button', { name: 'Show more' }))
    expect(textBox().style.maxHeight).toBe('')
    const less = screen.getByRole('button', { name: 'Show less' })
    expect(less).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(less)
    expect(textBox().style.maxHeight).toBe(`${8 * LINE}px`)
  })

  it('is measured again when its width changes', () => {
    contentHeight = 6 * LINE
    renderText(LONG)
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
    // A narrower bubble (a phone turned, the pane opened) wraps it longer.
    contentHeight = 12 * LINE
    act(() => { for (const cb of [...observers]) cb() })
    expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument()
  })

  it('a find-in-chat query that matches opens it', () => {
    contentHeight = 30 * LINE
    renderText(LONG, 'line 13')
    expect(textBox().style.maxHeight).toBe('')
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('a query that does not match leaves it folded', () => {
    contentHeight = 30 * LINE
    renderText(LONG, 'nowhere in it')
    expect(textBox().style.maxHeight).toBe(`${8 * LINE}px`)
  })

  it('an unknown line height never folds', () => {
    contentHeight = 30 * LINE
    lineHeight = 'normal'
    renderText(LONG)
    expect(screen.queryByRole('button', { name: /Show/ })).toBeNull()
  })

  it('a photo-only message draws no text', () => {
    renderText('')
    expect(screen.queryByTestId('user-message-text')).toBeNull()
  })
})

function renderList(messages: DisplayMessage[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <SearchProvider query="">
          <ChatMessages messages={messages} agentName="a" chatId="c1" onPermissionRespond={() => {}} />
        </SearchProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('Show less brings the message back into view', () => {
  it("scrolls the person's bubble, whose margin clears the floating top bar", () => {
    contentHeight = 30 * LINE
    const scrolled: Element[] = []
    const real = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) { scrolled.push(this) }
    try {
      renderList([{ id: 'u1', role: 'user', createdAt: '2026-10-26T18:00:00+00:00',
        blocks: [{ type: 'text', content: LONG }] }])
      fireEvent.click(screen.getByRole('button', { name: 'Show more' }))
      expect(scrolled).toEqual([])
      fireEvent.click(screen.getByRole('button', { name: 'Show less' }))
      expect(scrolled).toHaveLength(1)
      expect(scrolled[0].getAttribute('data-msg-id')).toBe('u1')
      expect(scrolled[0].className).toContain('scroll-mt-16')
    } finally {
      Element.prototype.scrollIntoView = real
    }
  })
})

describe('a message of photos alone', () => {
  it('has no copy button, since there is nothing to copy', () => {
    renderList([{ id: 'u2', role: 'user', createdAt: '2026-10-26T18:00:00+00:00', blocks: [
      { type: 'image_attachments', images: ['a.jpg'], paths: ['users/u/workspace/uploads/photos/a.jpg'] },
      { type: 'text', content: '' },
    ] }])
    expect(screen.queryByTitle('Copy message')).toBeNull()
  })
})

describe('the copy button under a folded message', () => {
  it('copies the whole text', async () => {
    contentHeight = 30 * LINE
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const msg: DisplayMessage = {
      id: 'u1', role: 'user', createdAt: '2026-10-26T18:00:00+00:00',
      blocks: [{ type: 'text', content: LONG }],
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <SearchProvider query="">
            <ChatMessages messages={[msg]} agentName="a" chatId="c1" onPermissionRespond={() => {}} />
          </SearchProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    )
    expect(screen.getByRole('button', { name: 'Show more' })).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByTitle('Copy message')) })
    expect(writeText).toHaveBeenCalledWith(LONG)
  })
})
