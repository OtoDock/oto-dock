import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'

// The conversations query is mocked to an empty external list — we only assert
// the source filter + empty-state copy (phone is the only external source;
// dashboard chats live on the chat-history page).
vi.mock('@/api/chats', () => ({
  useAgentConversations: () => ({ data: { conversations: [], total: 0 }, isLoading: false }),
}))

import AgentConversations from '@/pages/agent/AgentConversations'

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/agents/demo/conversations']}>
      <Routes>
        <Route path="/agents/:name/conversations" element={<AgentConversations />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AgentConversations', () => {
  it('shows the external-only empty state', () => {
    renderPage()
    expect(screen.getByText(/No phone or external conversations yet/i)).toBeInTheDocument()
  })

  it('only offers Phone sources — no Chat or Task filter (phone is the only external source)', () => {
    renderPage()
    const options = Array.from(document.querySelectorAll('option')).map(o => o.textContent)
    expect(options).toContain('All Sources')
    expect(options).toContain('Phone')
    expect(options).not.toContain('Task')
    expect(options).not.toContain('Chat')
  })
})
