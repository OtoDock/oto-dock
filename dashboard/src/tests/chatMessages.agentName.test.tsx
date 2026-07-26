/**
 * Assistant header identity: the page agent's CURRENT display name (passed
 * down from AgentChat's already-loaded record) must win over the slug-derived
 * title-casing without depending on useAgents() timing; per-message frozen
 * identity (delegates, meetings) still wins over the page prop.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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
