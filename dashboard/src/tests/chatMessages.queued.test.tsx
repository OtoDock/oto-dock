/**
 * The queued chips' cancel x: shown without hover on a touch screen (the
 * hover-only opacity applies under a hover-capable pointer only), a 32 px
 * target with a title, only on a chip this person may cancel, and it fires
 * with the chip's index (the page maps it to the chip's queue id).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import { QUEUE_WAITING } from '@/api/wireEvents'
import { toQueuedMessage } from '@/store/types'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

function renderChips(mayCancel?: (m: { authorSub?: string }) => boolean) {
  const onCancel = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}><ChatMessages
      messages={[]}
      agentName="a"
      queuedMessages={[
        { text: 'mine', queueId: 'q1', authorSub: 'me' },
        { text: 'theirs', queueId: 'q2', authorSub: 'them' },
      ]}
      onPermissionRespond={vi.fn()}
      onCancelQueued={onCancel}
      mayCancelQueued={mayCancel}
    /></QueryClientProvider>,
  )
  return onCancel
}

describe('the queued chip cancel', () => {
  it('is visible on touch and hover-revealed only with a mouse', () => {
    renderChips()
    const [x] = screen.getAllByTestId('cancel-queued')
    expect(x.className).toContain('[@media(hover:hover)]:opacity-0')
    expect(x.className).toContain('[@media(hover:hover)]:group-hover:opacity-100')
    expect(x.className).toContain('focus-visible:opacity-100')
    expect(x.className).not.toMatch(/(^|\s)opacity-0(\s|$)/)
    expect(x.className).toContain('w-8 h-8')
    expect(x.getAttribute('title')).toBe('Cancel')
  })

  it('shows only on a chip this person may cancel and fires its index', () => {
    const onCancel = renderChips((m) => m.authorSub === 'me')
    const xs = screen.getAllByTestId('cancel-queued')
    expect(xs).toHaveLength(1)
    fireEvent.click(xs[0])
    expect(onCancel).toHaveBeenCalledWith(0)
  })
})

describe('a chip waiting for its machine', () => {
  function renderWaiting(waiting?: string) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><ChatMessages
        messages={[]} agentName="a"
        queuedMessages={[toQueuedMessage({ text: 'later', queue_id: 'q1', author_sub: 'me', waiting })]}
        onPermissionRespond={vi.fn()}
      /></QueryClientProvider>,
    )
  }

  it('says the message goes out when the machine is back', () => {
    renderWaiting(QUEUE_WAITING.RECONNECT)
    expect(screen.getByTestId('queued-waiting').textContent).toBe(
      'The machine running this chat is reconnecting. Your message goes out when it is back.')
  })

  it('says nothing more once the machine is back', () => {
    renderWaiting(undefined)
    expect(screen.getByText('later')).toBeTruthy()
    expect(screen.queryByTestId('queued-waiting')).toBeNull()
  })
})

describe('the turn-ended card in the list', () => {
  const ended = [{ id: 'a1', role: 'assistant', blocks: [
    { type: 'system', subtype: 'turn_ended', reason: 'exited', message: 'gone' },
  ] }] as unknown as Parameters<typeof ChatMessages>[0]['messages']

  function renderCard(streaming: boolean) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><ChatMessages
        messages={ended} agentName="a" streaming={streaming}
        onPermissionRespond={vi.fn()} onSendAgain={vi.fn()}
      /></QueryClientProvider>,
    )
  }

  it('offers no Send again while a turn streams', () => {
    renderCard(true)
    expect(screen.queryByRole('button', { name: 'Send again' })).toBeNull()
  })

  it('offers Send again once the chat is idle', () => {
    renderCard(false)
    expect(screen.getByRole('button', { name: 'Send again' })).toBeTruthy()
  })
})
