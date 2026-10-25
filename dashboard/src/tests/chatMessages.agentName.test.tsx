/**
 * Assistant header identity: the page agent's CURRENT display name (passed
 * down from AgentChat's already-loaded record) must win over the slug-derived
 * title-casing without depending on useAgents() timing; per-message frozen
 * identity (delegates, meetings) still wins over the page prop. A reply that
 * goes on from the bubble above (`continuation`) draws no header of its own.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SYSTEM_SUBTYPE } from '@/api/wireEvents'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessages from '@/components/chat/ChatMessages'
import type { DisplayMessage } from '@/components/chat/types'

class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal('ResizeObserver', ObserverStub)
vi.stubGlobal('IntersectionObserver', ObserverStub)

function renderMessages(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

function assistantMsg(over: Partial<DisplayMessage> = {}): DisplayMessage {
  return {
    id: 'db-1',
    role: 'assistant',
    blocks: [{ type: 'text', content: 'hello there' }],
    createdAt: '2026-07-26T00:00:00+00:00',
    ...over,
  }
}

describe('ChatMessages assistant header name', () => {
  it('prefers the page agent display name over the slug-derived one', () => {
    renderMessages(
      <ChatMessages
        messages={[assistantMsg()]}
        agentName="personal-assistant-lite"
        agentDisplayName="Villa Concierge"
        onPermissionRespond={() => {}}
      />,
    )
    expect(screen.getByText('Villa Concierge')).toBeInTheDocument()
    expect(screen.queryByText('Personal Assistant Lite')).toBeNull()
  })

  it("paints the avatar in the agent's colour, else the agent fill", () => {
    const { unmount } = renderMessages(
      <ChatMessages messages={[assistantMsg()]} agentName="personal-assistant-lite" onPermissionRespond={() => {}} />,
    )
    const plain = screen.getByText('P')
    expect(plain.className).toContain('bg-accent-agent-fill')
    unmount()
    renderMessages(
      <ChatMessages messages={[assistantMsg()]} agentName="personal-assistant-lite" agentColor="#336699" onPermissionRespond={() => {}} />,
    )
    const coloured = screen.getByText('P')
    expect(coloured.style.backgroundColor).toBe('rgb(51, 102, 153)')
    expect(coloured.className).not.toContain('bg-accent-agent-fill')
  })

  it('falls back to the title-cased slug without a display name', () => {
    renderMessages(
      <ChatMessages
        messages={[assistantMsg()]}
        agentName="personal-assistant-lite"
        onPermissionRespond={() => {}}
      />,
    )
    expect(screen.getByText('Personal Assistant Lite')).toBeInTheDocument()
  })

  it('per-message frozen identity still wins over the page prop', () => {
    renderMessages(
      <ChatMessages
        messages={[assistantMsg({ agentSlug: 'other-agent', agentDisplayName: 'Other One' })]}
        agentName="personal-assistant-lite"
        agentDisplayName="Villa Concierge"
        onPermissionRespond={() => {}}
      />,
    )
    expect(screen.getByText('Other One')).toBeInTheDocument()
  })
})

describe('a reply that goes on from the bubble above', () => {
  const review = (over: Partial<DisplayMessage> = {}) => assistantMsg({
    id: 'db-evt-4',
    continuation: true,
    blocks: [
      { type: 'system', subtype: SYSTEM_SUBTYPE.BG_COMMANDS_COMPLETED, count: 14 },
      { type: 'text', content: 'All fourteen passed.' },
    ],
    ...over,
  })

  it('draws no header and no separator, the nudge as one faint line, its own footer', () => {
    const { container } = renderMessages(
      <ChatMessages messages={[assistantMsg(), review()]} agentName="personal-assistant-lite"
                    agentDisplayName="Villa Concierge" onPermissionRespond={() => {}} />,
    )
    expect(screen.getAllByText('Villa Concierge')).toHaveLength(1)
    expect(container.querySelector('.border-t')).toBeNull()
    expect(screen.getByTestId('review-note')).toHaveTextContent('14 background commands finished')
    expect(screen.queryByText('Background commands completed')).toBeNull()
    expect(screen.getAllByTitle('Copy message')).toHaveLength(2)
    expect(container.querySelector('[data-msg-id="db-evt-4"]')).toHaveAttribute('data-continuation', 'true')
  })

  it('keeps its header under a person, under a delegate card, and when it starts the list', () => {
    const person: DisplayMessage = { id: 'db-0', role: 'user', blocks: [{ type: 'text', content: 'go' }], createdAt: '2026-07-26T00:00:00+00:00' }
    const card = assistantMsg({
      id: 'db-delresult-2', agentSlug: 'worker', agentDisplayName: 'Worker', badge: 'delegate response',
      delegateCard: { kind: 'result', key: 'r:k1:x' }, blocks: [{ type: 'text', content: 'the report' }],
    })
    for (const above of [[person], [card], []]) {
      const { container, unmount } = renderMessages(
        <ChatMessages messages={[...above, review()]} agentName="personal-assistant-lite"
                      agentDisplayName="Villa Concierge" onPermissionRespond={() => {}} />,
      )
      expect(screen.getAllByText('Villa Concierge')).toHaveLength(1)
      expect(container.querySelector('[data-msg-id="db-evt-4"]')).not.toHaveAttribute('data-continuation')
      expect(screen.getByTestId('review-note')).toHaveTextContent('14 background commands finished')
      unmount()
    }
  })
})
