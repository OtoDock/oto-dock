/**
 * A send while scrolled up pins the chat to the bottom: the page asks the
 * list through its ref (`pinToBottom`); a new message alone, a result from
 * a delegate or a frame from another tab, never moves a reading position.
 * The scroll-back fills the view: while the top of the loaded history is in
 * reach, the next older page is asked for (six in a row at most, a gesture
 * starts again), the loading row keeps its height, and the rows a page puts
 * above the old top fade in.
 */
import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import type { ChatMessagesHandle, DisplayMessage } from '@/components/chat/types'
import type { OlderPage } from '@/hooks/useChatMessages'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const scrollTo = vi.fn()

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', ObserverStub)
  vi.stubGlobal('IntersectionObserver', ObserverStub)
  HTMLElement.prototype.scrollTo = scrollTo as unknown as typeof HTMLElement.prototype.scrollTo
  scrollTo.mockClear()
})

afterEach(() => { vi.unstubAllGlobals() })

const msg = (id: string, role: 'user' | 'assistant', text: string): DisplayMessage => ({
  id, role, blocks: [{ type: 'text', content: text }], createdAt: '2026-10-16T09:00:00+00:00',
})

function renderList(messages: DisplayMessage[], ref = createRef<ChatMessagesHandle>()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ui = (m: DisplayMessage[]) => (
    <QueryClientProvider client={qc}>
      <ChatMessages ref={ref} messages={m} agentName="a" onPermissionRespond={() => {}} />
    </QueryClientProvider>
  )
  const view = render(ui(messages))
  return { ref, rerender: (m: DisplayMessage[]) => view.rerender(ui(m)) }
}

describe('the scroll pin on a send', () => {
  it('a new message alone moves nothing; the page\'s send pins to the newest', () => {
    const { ref, rerender } = renderList([msg('1', 'user', 'hi'), msg('2', 'assistant', 'hello')])
    rerender([msg('1', 'user', 'hi'), msg('2', 'assistant', 'hello'), msg('3', 'assistant', 'a delegate result')])
    expect(scrollTo).not.toHaveBeenCalled()
    act(() => ref.current?.pinToBottom())
    expect(scrollTo).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' })
  })
})

type ListProps = { messages: DisplayMessage[]; hasMoreOlder?: boolean; loadingOlder?: boolean; onLoadOlder?: () => Promise<OlderPage> }

function renderPaged(props: ListProps, ref = createRef<ChatMessagesHandle>()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const ui = (p: ListProps) => (
    <QueryClientProvider client={qc}>
      <ChatMessages ref={ref} agentName="a" onPermissionRespond={() => {}} {...p} />
    </QueryClientProvider>
  )
  const view = render(ui(props))
  return { ...view, ref, rerender: (p: ListProps) => view.rerender(ui(p)) }
}

// The list's scroll position, as a browser keeps it (jsdom keeps none).
function scrollTopOf(container: HTMLElement, initial: number) {
  const root = container.querySelector('.overflow-y-auto') as HTMLElement
  const pos = { top: initial }
  Object.defineProperty(root, 'scrollTop', { configurable: true, get: () => pos.top, set: (v: number) => { pos.top = v } })
  return { root, pos }
}

// A page lands: the list re-renders with one more message on top, loading done.
async function landPage(rerender: (p: ListProps) => void, pages: DisplayMessage[][], load: ListProps['onLoadOlder'], more = true) {
  await act(async () => {})
  const messages = pages[pages.length - 1]
  rerender({ messages, hasMoreOlder: more, loadingOlder: true, onLoadOlder: load })
  pages.push([msg(`p${pages.length}`, 'assistant', 'older'), ...messages])
  rerender({ messages: pages[pages.length - 1], hasMoreOlder: more, loadingOlder: false, onLoadOlder: load })
  await act(async () => {})
}

const landed = (): Promise<OlderPage> => Promise.resolve('landed')

describe('the scroll-back fills the view', () => {
  it('asks for pages while the top is in reach, six in a row, again after a gesture', async () => {
    const load = vi.fn(landed)
    const pages = [[msg('1', 'assistant', 'newest')]]
    const { rerender, container } = renderPaged({ messages: pages[0], hasMoreOlder: false, onLoadOlder: load })
    expect(load).not.toHaveBeenCalled()
    rerender({ messages: pages[0], hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    for (let i = 0; i < 6; i++) await landPage(rerender, pages, load)
    expect(load).toHaveBeenCalledTimes(6)
    await act(async () => { fireEvent.wheel(container.querySelector('.overflow-y-auto')!, { deltaY: -100 }) })
    expect(load).toHaveBeenCalledTimes(7)
  })

  it('asks for nothing with no message yet, nor while a page loads', () => {
    const load = vi.fn(landed)
    const { rerender } = renderPaged({ messages: [], hasMoreOlder: true, onLoadOlder: load })
    expect(load).not.toHaveBeenCalled()
    rerender({ messages: [msg('1', 'assistant', 'x')], hasMoreOlder: true, loadingOlder: true, onLoadOlder: load })
    expect(load).not.toHaveBeenCalled()
  })

  it('rests after a page that brought nothing, until a gesture', async () => {
    const load = vi.fn((): Promise<OlderPage> => Promise.resolve('nothing'))
    const messages = [msg('1', 'assistant', 'x')]
    const { rerender, container } = renderPaged({ messages, hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => {})
    rerender({ messages, hasMoreOlder: true, loadingOlder: true, onLoadOlder: load })
    rerender({ messages, hasMoreOlder: true, loadingOlder: false, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => { fireEvent.wheel(container.querySelector('.overflow-y-auto')!, { deltaY: -100 }) })
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('starts again with the next rows after a stale page, the oldest message unchanged', async () => {
    const load = vi.fn((): Promise<OlderPage> => Promise.resolve('stale'))
    const first = [msg('1', 'assistant', 'x')]
    const { rerender } = renderPaged({ messages: first, hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => {})
    expect(load).toHaveBeenCalledTimes(1)
    // The window sent again: a new list whose oldest message is the same.
    load.mockImplementation(landed)
    rerender({ messages: [msg('1', 'assistant', 'x'), msg('2', 'assistant', 'y')], hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('keeps the loading row\'s height while more history exists', () => {
    const messages = [msg('1', 'assistant', 'x')]
    const { rerender } = renderPaged({ messages, hasMoreOlder: true, loadingOlder: true, onLoadOlder: landed })
    expect(screen.getByTestId('older-loading')).toHaveClass('h-8')
    expect(screen.getByTestId('older-loading')).toHaveTextContent('Loading older messages…')
    rerender({ messages, hasMoreOlder: true, loadingOlder: false })
    expect(screen.getByTestId('older-loading')).toHaveClass('h-8')
    expect(screen.getByTestId('older-loading')).toHaveTextContent('')
    rerender({ messages, hasMoreOlder: false, loadingOlder: false })
    expect(screen.queryByTestId('older-loading')).toBeNull()
  })

  it('fades in the bubbles a page put wholly above the old top, not the one it merged with', async () => {
    // Content bottom at 1000; the old top bubble began at 800.
    const boxes: Record<string, [number, number]> = { content: [0, 1000], 'db-3': [800, 900] }
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const key = this.dataset.msgId ?? (this.className.includes('max-w-4xl') ? 'content' : '')
      const [top, bottom] = boxes[key] ?? [0, 0]
      return { top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ({}) } as DOMRect
    })
    let land: (v: OlderPage) => void = () => {}
    const load = vi.fn(() => new Promise<OlderPage>((r) => { land = r }))
    const old = [msg('db-3', 'assistant', 'the old top')]
    const { rerender, container } = renderPaged({ messages: old, hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    // The page's rows: a bubble of their own, and the old top's earlier rows,
    // which re-key it (it straddles the old top: it was on screen).
    Object.assign(boxes, { 'db-1': [300, 500], 'db-2': [520, 900] })
    rerender({ messages: [msg('db-1', 'assistant', 'a'), msg('db-2', 'assistant', 'the old top, merged')], hasMoreOlder: true, onLoadOlder: load })
    await act(async () => { land('landed') })
    const faded = [...container.querySelectorAll('.chat-older-in')].map((el) => el.getAttribute('data-msg-id'))
    expect(faded).toEqual(['db-1'])
    vi.restoreAllMocks()
  })
})

describe('the reading position across a page', () => {
  it('a page the fill asked for lands with the position kept', async () => {
    const load = vi.fn(landed)
    const old = [msg('3', 'assistant', 'x')]
    const { rerender, container } = renderPaged({ messages: old, hasMoreOlder: false, onLoadOlder: load })
    const { pos } = scrollTopOf(container, -250)
    rerender({ messages: old, hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    pos.top = -40  // an engine that does not anchor a reversed column moved the view
    rerender({ messages: [msg('1', 'assistant', 'older'), ...old], hasMoreOlder: false, onLoadOlder: load })
    expect(pos.top).toBe(-250)
  })

  it('a page that changed no bubble leaves no position for a later window', async () => {
    const load = vi.fn(landed)
    const old = [msg('3', 'assistant', 'x')]
    const { rerender, container } = renderPaged({ messages: old, hasMoreOlder: false, onLoadOlder: load })
    const { pos } = scrollTopOf(container, -250)
    rerender({ messages: old, hasMoreOlder: true, onLoadOlder: load })
    await act(async () => {})
    rerender({ messages: old, hasMoreOlder: true, loadingOlder: true, onLoadOlder: load })
    rerender({ messages: old, hasMoreOlder: false, loadingOlder: false, onLoadOlder: load })
    pos.top = 0
    rerender({ messages: [msg('9', 'assistant', 'a new window')], hasMoreOlder: false, onLoadOlder: load })
    expect(pos.top).toBe(0)
  })

  it('the scroll-to-bottom button lets go of a page still loading', async () => {
    let land: (v: OlderPage) => void = () => {}
    const load = vi.fn(() => new Promise<OlderPage>((r) => { land = r }))
    const old = [msg('3', 'assistant', 'x')]
    const { rerender, container } = renderPaged({ messages: old, hasMoreOlder: false, onLoadOlder: load })
    const { root, pos } = scrollTopOf(container, -250)
    rerender({ messages: old, hasMoreOlder: true, onLoadOlder: load })
    expect(load).toHaveBeenCalledTimes(1)
    fireEvent.scroll(root)
    fireEvent.click(container.querySelector('.scroll-to-bottom-btn')!)
    pos.top = 0
    rerender({ messages: [msg('1', 'assistant', 'older'), ...old], hasMoreOlder: false, onLoadOlder: load })
    await act(async () => { land('landed') })
    expect(pos.top).toBe(0)
  })
})
