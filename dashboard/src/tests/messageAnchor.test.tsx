/**
 * `?message=<id>`: the chat opens at one reply. The parameter leaves the URL
 * alone (the others stay); once the chat's rows are on the page the bubble
 * the row starts is scrolled into view and marked; older pages load while the
 * reply is above them; nothing happens on another chat or with no list.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import { MemoryRouter, useLocation, useSearchParams } from 'react-router-dom'

import type { DisplayMessage } from '@/components/chat/types'
import type { OlderPage } from '@/hooks/useChatMessages'
import { ANCHOR_MARK_MS, MAX_PAGES, anchorBubble, useMessageAnchor } from '@/pages/agent/chat/useMessageAnchor'
import { useFindBar } from '@/pages/agent/chat/useFindBar'

const bubble = (id: string): DisplayMessage => ({ id, role: 'assistant', createdAt: 't', blocks: [] })

let where = ''
let reported: ReturnType<typeof useMessageAnchor> = { pending: false, landed: null }
function Probe({ messages, chatId = 'c1', hasMoreOlder = false, loadingOlder = false, listShown = true,
  loadOlder = async (): Promise<OlderPage> => 'nothing' }: {
  messages: DisplayMessage[]; chatId?: string; hasMoreOlder?: boolean; loadingOlder?: boolean
  listShown?: boolean; loadOlder?: () => Promise<OlderPage>
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  where = `${location.pathname}${location.search}`
  reported = useMessageAnchor({
    searchParams, setSearchParams, urlChatId: 'c1', chatId, messages, loadOlder, hasMoreOlder,
    loadingOlder, listShown,
  })
  return <>{messages.map((m) => <div key={m.id} data-msg-id={m.id}>{m.id}</div>)}</>
}

function renderAt(url: string, props: Parameters<typeof Probe>[0]) {
  const view = render(<MemoryRouter initialEntries={[url]}><Probe {...props} /></MemoryRouter>)
  return {
    ...view,
    update: (next: Parameters<typeof Probe>[0]) =>
      view.rerender(<MemoryRouter initialEntries={[url]}><Probe {...next} /></MemoryRouter>),
  }
}

const scrolled = vi.fn()
beforeEach(() => {
  scrolled.mockReset()
  Element.prototype.scrollIntoView = function (this: Element) { scrolled(this.getAttribute('data-msg-id')) }
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('the bubble a row belongs to', () => {
  it('is the one with the largest first row at or below it', () => {
    const list = [bubble('db-40'), bubble('db-evt-44'), bubble('stream-1'), bubble('db-delresult-50')]
    expect(anchorBubble(list, 44)?.id).toBe('db-evt-44')
    expect(anchorBubble(list, 47)?.id).toBe('db-evt-44')
    expect(anchorBubble(list, 60)?.id).toBe('db-delresult-50')
    expect(anchorBubble(list, 39)).toBeNull()
  })
})

describe('opening a chat at a reply', () => {
  it('scrolls to the reply, marks it for a moment and leaves the other parameters', () => {
    // The mark's timeout only: the frame callback stays the stub above.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const { container } = renderAt('/chat/a/c1?tasks=1&message=41', {
      messages: [bubble('db-40'), bubble('db-41'), bubble('db-45')],
    })
    expect(scrolled).toHaveBeenCalledWith('db-41')
    const el = container.querySelector('[data-msg-id="db-41"]')!
    expect(el.hasAttribute('data-anchored')).toBe(true)
    expect(where).toBe('/chat/a/c1?tasks=1')
    act(() => { vi.advanceTimersByTime(ANCHOR_MARK_MS) })
    expect(el.hasAttribute('data-anchored')).toBe(false)
  })

  it('waits for the chat\'s rows, then loads older pages while the reply is above them', async () => {
    const loadOlder = vi.fn(async (): Promise<OlderPage> => 'landed')
    const view = renderAt('/chat/a/c1?message=41', { messages: [], loadOlder, hasMoreOlder: true })
    expect(loadOlder).not.toHaveBeenCalled()
    await act(async () => { view.update({ messages: [bubble('db-60'), bubble('db-61')], loadOlder, hasMoreOlder: true }) })
    expect(loadOlder).toHaveBeenCalledTimes(1)
    expect(scrolled).not.toHaveBeenCalled()
    await act(async () => {
      view.update({ messages: [bubble('db-40'), bubble('db-41'), bubble('db-60'), bubble('db-61')], loadOlder, hasMoreOlder: true })
    })
    expect(scrolled).toHaveBeenCalledWith('db-41')
    expect(loadOlder).toHaveBeenCalledTimes(1)
  })

  it('does nothing on another chat, nor while a terminal stands in for the list', async () => {
    const loadOlder = vi.fn(async (): Promise<OlderPage> => 'landed')
    const view = renderAt('/chat/a/c1?message=41', { messages: [bubble('db-60')], chatId: 'c2', loadOlder, hasMoreOlder: true })
    expect(loadOlder).not.toHaveBeenCalled()
    await act(async () => { view.update({ messages: [bubble('db-60')], listShown: false, loadOlder, hasMoreOlder: true }) })
    expect(loadOlder).not.toHaveBeenCalled()
    expect(scrolled).not.toHaveBeenCalled()
  })

  it('stops at the oldest row when the reply is gone', async () => {
    const view = renderAt('/chat/a/c1?message=12', { messages: [bubble('db-30'), bubble('db-31')] })
    await act(async () => { view.update({ messages: [bubble('db-30'), bubble('db-31')] }) })
    expect(scrolled).not.toHaveBeenCalled()
  })

  it('loads at most MAX_PAGES older pages, then gives up quietly', async () => {
    const loadOlder = vi.fn(async (): Promise<OlderPage> => 'landed')
    const view = renderAt('/chat/a/c1?message=5', { messages: [bubble('db-1000')], loadOlder, hasMoreOlder: true })
    for (let k = 1; k <= MAX_PAGES + 5; k++) {
      await act(async () => {
        view.update({ messages: [bubble(`db-${1000 - k * 10}`)], loadOlder, hasMoreOlder: true })
      })
    }
    expect(loadOlder).toHaveBeenCalledTimes(MAX_PAGES)
    await act(async () => { view.update({ messages: [bubble('db-5'), bubble('db-6')], loadOlder, hasMoreOlder: true }) })
    expect(scrolled).not.toHaveBeenCalled()
  })

  it('asks a failed page once more, then lands on the reply', async () => {
    const answers: OlderPage[] = ['nothing', 'landed']
    const loadOlder = vi.fn(async (): Promise<OlderPage> => answers.shift() ?? 'landed')
    const view = renderAt('/chat/a/c1?message=41', { messages: [bubble('db-60')], loadOlder, hasMoreOlder: true })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(loadOlder).toHaveBeenCalledTimes(2)
    await act(async () => {
      view.update({ messages: [bubble('db-40'), bubble('db-41'), bubble('db-60')], loadOlder, hasMoreOlder: true })
    })
    expect(scrolled).toHaveBeenCalledWith('db-41')
  })

  it('gives up after a second failed page', async () => {
    const loadOlder = vi.fn(async (): Promise<OlderPage> => 'nothing')
    const view = renderAt('/chat/a/c1?message=41', { messages: [bubble('db-60')], loadOlder, hasMoreOlder: true })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(loadOlder).toHaveBeenCalledTimes(2)
    await act(async () => { view.update({ messages: [bubble('db-59'), bubble('db-60')], loadOlder, hasMoreOlder: true }) })
    expect(loadOlder).toHaveBeenCalledTimes(2)
    expect(scrolled).not.toHaveBeenCalled()
  })
})

describe('what the anchor reports', () => {
  it('is pending until the reply is found, then names the bubble it landed on', async () => {
    const view = renderAt('/chat/a/c1?message=41', { messages: [] })
    expect(reported).toEqual({ pending: true, landed: null })
    await act(async () => { view.update({ messages: [bubble('db-40'), bubble('db-41')] }) })
    expect(reported.pending).toBe(false)
    expect(reported.landed?.bubbleId).toBe('db-41')
  })

  it('is not pending on another chat, and lands nowhere for a reply that is gone', async () => {
    renderAt('/chat/a/c1?message=41', { messages: [], chatId: 'c2' })
    expect(reported.pending).toBe(false)
    const view = renderAt('/chat/a/c1?message=12', { messages: [] })
    await act(async () => { view.update({ messages: [bubble('db-30')] }) })
    expect(reported).toEqual({ pending: false, landed: null })
  })
})

function FindProbe() {
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  where = `${location.pathname}${location.search}`
  useFindBar({ searchParams, setSearchParams })
  return null
}

describe('a search result opening with the find bar', () => {
  it('takes the find query off the URL and leaves the anchor and the rest', () => {
    render(<MemoryRouter initialEntries={['/chat/a/c1?q=needle&message=41&tasks=1']}><FindProbe /></MemoryRouter>)
    expect(where).toBe('/chat/a/c1?message=41&tasks=1')
  })
})
