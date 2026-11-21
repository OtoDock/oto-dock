import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NotificationDelivery } from '@/api/notifications'

const h = vi.hoisted(() => ({
  deliveries: [] as unknown[], toasts: [] as unknown[], closePanel: vi.fn(), markRead: vi.fn(),
}))
vi.mock('@/hooks/useNotifications', () => ({
  useNotifications: () => ({
    unreadCount: 1, setCount: vi.fn(), deliveries: h.deliveries, toasts: h.toasts, panelOpen: true,
    loading: false, addToast: vi.fn(), addSilent: vi.fn(), dismissToast: vi.fn(), togglePanel: vi.fn(),
    closePanel: h.closePanel, markRead: h.markRead, markAllRead: vi.fn(), dismiss: vi.fn(),
  }),
}))
vi.mock('@/hooks/useNotificationSound', () => ({
  useNotificationSound: () => ({ playForSeverity: vi.fn(), stopDangerAlarm: vi.fn(), playPing: vi.fn() }),
}))
vi.mock('@/hooks/usePushSubscription', () => ({ usePushSubscription: () => {} }))
vi.mock('@/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/agents')>()),
  useAgents: () => ({ data: [] }),
}))
vi.mock('@/api/shares', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/shares')>()),
  useShareInbox: () => ({ data: { items: [], pending: 0 }, refetch: vi.fn() }),
  useAcceptShare: () => ({ mutateAsync: vi.fn() }),
  useDeclineShare: () => ({ mutateAsync: vi.fn() }),
  useHideShare: () => ({ mutateAsync: vi.fn() }),
}))
vi.mock('@/contexts/AuthContext', () => ({ useAuth: () => ({ user: null }) }))

import { useChatNotifications } from '@/hooks/useChatNotifications'
import ToastViewport from '@/components/ui/Toast'

// A file-conflict notification names the agent and no chat: what it speaks
// of is the agent's Recover bin. From a chat of that agent its button opens
// the bin over the chat, which stays on screen with its terminal. It used to
// go to the agent's new-chat address, where the chat showed as not active.

function delivery(over: Partial<NotificationDelivery>): NotificationDelivery {
  return {
    id: 'd1', notification_id: null, title: 'Recover your files', body: 'Three files.',
    severity: 'info', scope: 'user', source: 'file_conflict', delivered_at: new Date().toISOString(),
    read: 0, dismissed: 0, read_at: null, dismissed_at: null, agent_slug: 'dev', chat_id: null,
    ...over,
  }
}

function Page() {
  const { notificationBell, notificationToast } = useChatNotifications()
  const location = useLocation()
  return (
    <div>
      {notificationBell}
      {notificationToast}
      <span data-testid="address">{location.pathname + location.search}</span>
    </div>
  )
}

function renderAt(address: string) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ToastViewport />
      <MemoryRouter initialEntries={[address]}>
        <Routes>
          <Route path="/chat/:name" element={<Page />} />
          <Route path="/chat/:name/:chatId" element={<Page />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const address = () => screen.getByTestId('address').textContent

beforeEach(() => {
  h.toasts = []
  h.closePanel.mockClear()
  h.markRead.mockClear()
})

describe("a notification's button that names the agent alone", () => {
  it('opens the Recover bin over the chat of that agent on screen', () => {
    h.deliveries = [delivery({})]
    renderAt('/chat/dev/c1?ws=1')
    fireEvent.click(screen.getByTitle('Open Recover bin'))
    expect(address()).toBe('/chat/dev/c1?ws=1&recover=1')
    expect(h.closePanel).toHaveBeenCalled()
  })

  it("opens the agent's page from another agent's chat", () => {
    h.deliveries = [delivery({})]
    renderAt('/chat/ceo/c9')
    fireEvent.click(screen.getByTitle('Open Recover bin'))
    expect(address()).toBe('/chat/dev?recover=1')
  })

  it('a button that names a chat still opens that chat', () => {
    h.deliveries = [delivery({ source: 'mcp', title: 'Done', chat_id: 'c2' })]
    renderAt('/chat/dev/c1')
    fireEvent.click(screen.getByTitle('Open chat'))
    expect(address()).toBe('/chat/dev/c2')
  })
})

describe("a notification's toast", () => {
  it('a file conflict opens the Recover bin, as its row does', () => {
    h.deliveries = []
    h.toasts = [{ id: 't1', delivery: delivery({}) }]
    renderAt('/chat/dev/c1')
    fireEvent.click(screen.getByText('Recover your files'))
    expect(address()).toBe('/chat/dev/c1?recover=1')
  })
})
