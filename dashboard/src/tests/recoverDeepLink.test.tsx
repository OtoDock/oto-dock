import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useSearchParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useApps: () => ({ data: [] }),
  useChatPins: () => ({ data: undefined }),
}))
vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))
vi.mock('@/hooks/useAppsAutoOpen', () => ({ useAppsAutoOpen: () => {} }))

import { useOverlayPanels } from '@/pages/agent/chat/useOverlayPanels'

// A file-conflict notification's button opens the agent's Recover bin
// through `?recover=1`: the workspace overlay opens (`?ws=1`, the overlay's
// open state is the address) and the flag goes, in one write. Two writes in
// one pass each start from the same address, so the second dropped the
// first's `ws=1` and the bin never showed.

function Probe() {
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  const overlays = useOverlayPanels({
    messages: [], agentName: 'dev',
    user: { username: 'admin', role: 'admin', agent_roles: {} } as unknown as Parameters<typeof useOverlayPanels>[0]['user'],
    chatId: 'c1', searchParams, setSearchParams, urlChatId: 'c1', chats: [], taskChats: [],
    keepAppsOnChatEntryRef: { current: false },
  })
  return (
    <div>
      <span data-testid="address">{location.pathname + location.search}</span>
      <span data-testid="open">{String(overlays.workspace.state.open)}</span>
      <span data-testid="recover">{String(overlays.recoverRequested)}</span>
    </div>
  )
}

function renderAt(address: string) {
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[address]}>
        <Routes><Route path="/chat/:name/:chatId" element={<Probe />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('the ?recover=1 deep link', () => {
  it('opens the workspace with the Recover bin asked for, and drops the flag', () => {
    renderAt('/chat/dev/c1?recover=1')
    expect(screen.getByTestId('address').textContent).toBe('/chat/dev/c1?ws=1')
    expect(screen.getByTestId('open').textContent).toBe('true')
    expect(screen.getByTestId('recover').textContent).toBe('true')
  })

  it('keeps an open workspace and its preview', () => {
    renderAt('/chat/dev/c1?ws=1&ws_preview=notes.md&recover=1')
    expect(screen.getByTestId('address').textContent).toBe('/chat/dev/c1?ws=1&ws_preview=notes.md')
    expect(screen.getByTestId('open').textContent).toBe('true')
    expect(screen.getByTestId('recover').textContent).toBe('true')
  })
})
