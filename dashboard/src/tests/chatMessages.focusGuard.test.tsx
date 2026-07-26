/**
 * Collabora focus-steal guard: COOL grabbing focus at iframe boot makes the
 * browser scroll the chat to reveal the frame — the guard restores the last
 * settled position unless a real pointer interaction preceded the focus, and
 * it ignores non-Collabora iframes (inline artifacts hold real content).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/hooks/useCollaboraLiveReload', () => ({
  useCollaboraLiveReload: () => ({
    iframeRef: { current: null },
    reloadAvailable: false,
    doReload: () => {},
    modifiedRef: { current: false },
  }),
}))

import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage } from '@/components/chat/types'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
// No IntersectionObserver stub: its absence makes DocumentPreview latch (and
// mount its iframe) immediately, which is what these tests need.

afterEach(() => {
  vi.restoreAllMocks()
})

function previewMessage(): DisplayMessage {
  return {
    id: 'db-1',
    role: 'assistant',
    blocks: [{
      type: 'document_preview',
      wopiUrl: `${window.location.origin}/collabora/browser/dist/cool.html?WOPISrc=x&_t=1`,
      filename: 'report.xlsx',
      fileId: 'f1',
      downloadUrl: '/v1/media/tok',
      generation: Date.now(),
    }],
    createdAt: '2026-07-26T00:00:00+00:00',
  }
}

function renderChat() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { container } = render(
    <QueryClientProvider client={qc}>
      <ChatMessages
        messages={[previewMessage()]}
        agentName="dev-agent"
        onPermissionRespond={() => {}}
      />
    </QueryClientProvider>,
  )
  // The component's root is a positioning wrapper — the scroller (which owns
  // the guard listeners and the onScroll handler) is the flex-col-reverse div.
  const scroller = container.querySelector('.flex-col-reverse') as HTMLElement
  expect(scroller).not.toBeNull()
  const iframe = container.querySelector('[data-collabora-frame]') as HTMLIFrameElement
  expect(iframe).not.toBeNull()
  return { scroller, iframe }
}

function settleAt(scroller: HTMLElement, top: number) {
  scroller.scrollTop = top
  fireEvent.scroll(scroller)  // handleScroll records the settled position
}

describe('ChatMessages — Collabora focus-steal guard', () => {
  it('restores the settled position when focus arrives without a pointer', () => {
    const { scroller, iframe } = renderChat()
    settleAt(scroller, 500)
    scroller.scrollTop = 100  // the browser's reveal-scroll already happened
    fireEvent.focusIn(iframe)
    expect(scroller.scrollTop).toBe(500)
  })

  it('leaves the scroll alone when a pointer interaction preceded the focus', () => {
    const { scroller, iframe } = renderChat()
    settleAt(scroller, 500)
    fireEvent.pointerDown(scroller)
    scroller.scrollTop = 100
    fireEvent.focusIn(iframe)
    expect(scroller.scrollTop).toBe(100)
  })

  it('skips the restore for a slow boot after a click inside the same preview', () => {
    const { scroller, iframe } = renderChat()
    settleAt(scroller, 500)
    const host = iframe.closest('[data-collabora-host]') as HTMLElement
    fireEvent.pointerDown(host.querySelector('button')!)
    // The pointer window has long passed by the time COOL finishes booting.
    const realNow = Date.now()
    vi.spyOn(Date, 'now').mockReturnValue(realNow + 60_000)
    scroller.scrollTop = 100
    fireEvent.focusIn(iframe)
    expect(scroller.scrollTop).toBe(100)
  })

  it('ignores non-Collabora iframes (inline artifacts)', () => {
    const { scroller } = renderChat()
    settleAt(scroller, 500)
    const foreign = document.createElement('iframe')
    scroller.appendChild(foreign)
    scroller.scrollTop = 100
    fireEvent.focusIn(foreign)
    expect(scroller.scrollTop).toBe(100)
  })
})
