/**
 * A message a card's say button sent for the person: its bubble wears the
 * caption under the time, its stored row sets the mark from `event_data.card`,
 * its queued chip carries the mark and offers no cancel (a returned answer
 * would leave a false one on the card), while another chip keeps its x.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage } from '@/components/chat/types'
import { dbMessagesToDisplay } from '@/lib/messageBlocks'
import { toQueuedMessage } from '@/store/types'
import type { DbMessageRow } from '@/api/wireEvents'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

const CARD = { card_id: 'card-1', button: 'no' }

function renderList(messages: DisplayMessage[], queued: ReturnType<typeof toQueuedMessage>[] = []) {
  const onCancel = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <ChatMessages messages={messages} agentName="alpha" queuedMessages={queued}
          onPermissionRespond={vi.fn()} onCancelQueued={onCancel} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return onCancel
}

function userMsg(id: string, text: string, fromCard?: boolean): DisplayMessage {
  return { id, role: 'user', blocks: [{ type: 'text', content: text }], createdAt: '2026-10-28T10:00:00Z', ...(fromCard ? { fromCard } : {}) }
}

describe('a message a card sent', () => {
  it('wears the caption under the time, and a typed message does not', () => {
    renderList([userMsg('db-1', 'Move it.'), userMsg('db-2', 'Not now, thanks.', true)])
    const captions = screen.getAllByTestId('message-from-card')
    expect(captions).toHaveLength(1)
    expect(captions[0]).toHaveTextContent('From a card')
    expect(captions[0].className).toContain('text-2xs')
    // Under the time: the caption sits in the time's own line, after it.
    const line = captions[0].parentElement!
    expect(line.textContent?.endsWith('From a card')).toBe(true)
    expect(line.textContent?.length).toBeGreaterThan('From a card'.length)
  })

  it('gets the mark from the stored row event_data.card', () => {
    const row = (id: number, eventData?: object): DbMessageRow =>
      ({ id, role: 'user', content: 'Not now, thanks.', event_type: '', event_data: eventData ? JSON.stringify(eventData) : '', created_at: '2026-10-28T10:00:00Z' } as DbMessageRow)
    const [typed, sent, odd] = dbMessagesToDisplay([row(1), row(2, { card: CARD }), row(3, { card: 'x' })], [])
    expect(typed.fromCard).toBeUndefined()
    expect(sent.fromCard).toBe(true)
    expect(odd.fromCard).toBeUndefined()
  })

  it('reads the mark on a queue frame and leaves it off a plain one', () => {
    expect(toQueuedMessage({ text: 'Not now, thanks.', queue_id: 'q1', author_sub: 'me', card: CARD }))
      .toEqual({ text: 'Not now, thanks.', queueId: 'q1', authorSub: 'me', card: CARD })
    expect(toQueuedMessage({ text: 'Hi', queue_id: 'q2' })).toEqual({ text: 'Hi', queueId: 'q2' })
  })

  it('shows the chip with the caption and no cancel, while another chip keeps its x', () => {
    renderList([], [
      { text: 'Not now, thanks.', queueId: 'q1', authorSub: 'me', card: CARD },
      { text: 'And then this.', queueId: 'q2', authorSub: 'me' },
    ])
    expect(screen.getByTestId('queued-from-card')).toHaveTextContent('From a card')
    const xs = screen.getAllByTestId('cancel-queued')
    expect(xs).toHaveLength(1)
    expect(xs[0].closest('.group')?.textContent).toContain('And then this.')
  })
})
