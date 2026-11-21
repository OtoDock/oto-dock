import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const pinned = vi.hoisted(() => ({ apps: [] as unknown[] }))
vi.mock('@/api/apps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/apps')>()),
  useApps: () => ({ data: pinned.apps }),
  useChatPins: (chatId?: string) => ({ data: chatId ? { chat: { id: 'dock-app' }, files: [] } : undefined }),
}))
vi.mock('@/hooks/useActiveChats', () => ({ useActiveChats: () => [] }))

import { useOverlayPanels } from '@/pages/agent/chat/useOverlayPanels'
import { emitAppLive } from '@/lib/appLive'
import { WIRE } from '@/api/wireEvents'

// The chat page's three panels (the workspace, the apps, the chat's Dock)
// show one at a time. Each button turns its own panel on and off, a panel
// turned off shows the chat whatever was open before it, and opening one
// while another shows switches to it.

function Probe() {
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const { name, chatId } = { name: 'dev', chatId: location.pathname.split('/')[3] || null }
  const o = useOverlayPanels({
    messages: [], agentName: name,
    user: { username: 'admin', role: 'admin', agent_roles: {} } as unknown as Parameters<typeof useOverlayPanels>[0]['user'],
    chatId, searchParams, setSearchParams, urlChatId: chatId ?? undefined, chats: [], taskChats: [],
    keepAppsOnChatEntryRef: { current: false },
  })
  const shown = o.workspace.state.open ? 'workspace' : o.appsActive ? 'apps' : o.projectsActive ? 'dock' : 'chat'
  return (
    <div>
      <span data-testid="shown">{shown}</span>
      <span data-testid="address">{location.pathname + location.search}</span>
      <button onClick={o.workspace.toggleWorkspace}>workspace</button>
      <button onClick={o.toggleApps}>apps</button>
      <button onClick={o.toggleDock}>dock</button>
      <button onClick={() => navigate(-1)}>back</button>
      <button onClick={() => navigate(location.pathname + '?recover=1')}>recover link</button>
    </div>
  )
}

function renderAt(address: string, path = '/chat/:name/:chatId') {
  const qc = new QueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[address]}>
        <Routes><Route path={path} element={<Probe />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const press = (label: string) => act(() => { fireEvent.click(screen.getByRole('button', { name: label })) })
const shown = () => screen.getByTestId('shown').textContent

describe('the three panel buttons', () => {
  it('each button turns its own panel on and off', () => {
    renderAt('/chat/dev/c1')
    for (const b of ['workspace', 'apps', 'dock'] as const) {
      press(b)
      expect(shown()).toBe(b)
      press(b)
      expect(shown()).toBe('chat')
    }
  })

  it('opening one while another shows switches to it, and turning it off shows the chat', () => {
    const orders: [string, string][] = [
      ['workspace', 'apps'], ['workspace', 'dock'], ['apps', 'workspace'],
      ['apps', 'dock'], ['dock', 'workspace'], ['dock', 'apps'],
    ]
    for (const [first, second] of orders) {
      const { unmount } = renderAt('/chat/dev/c1')
      press(first)
      press(second)
      expect(shown(), `${first} then ${second}`).toBe(second)
      press(second)
      expect(shown(), `${first} then ${second}, ${second} off`).toBe('chat')
      unmount()
    }
  })

  it('all three in a row, each turned off at the end, show the chat', () => {
    renderAt('/chat/dev/c1')
    press('dock')
    press('apps')
    press('workspace')
    press('workspace')
    expect(shown()).toBe('chat')
    press('apps')
    expect(shown()).toBe('apps')
  })

  it('Back onto the workspace closes the apps under it, and the workspace off then shows the chat', () => {
    renderAt('/chat/dev/c1')
    press('workspace')
    press('apps')
    expect(shown()).toBe('apps')
    press('back')
    expect(screen.getByTestId('address').textContent).toBe('/chat/dev/c1?ws=1')
    expect(shown()).toBe('workspace')
    press('workspace')
    expect(shown()).toBe('chat')
  })

  it('a link that opens the Recover bin closes the apps, and the workspace off shows the chat', () => {
    renderAt('/chat/dev/c1')
    press('apps')
    press('recover link')
    expect(screen.getByTestId('address').textContent).toBe('/chat/dev/c1?ws=1')
    expect(shown()).toBe('workspace')
    press('workspace')
    expect(shown()).toBe('chat')
  })

  it("an agent's request to show its Dock app switches from the workspace to the Dock", () => {
    renderAt('/chat/dev/c1')
    press('workspace')
    act(() => {
      emitAppLive({ type: WIRE.OPEN_APP, app_id: 'dock-app', title: 'Dock app', agent: 'dev', scope_chat_id: 'c1', scope_project_id: '' })
    })
    expect(shown()).toBe('dock')
    press('dock')
    expect(shown()).toBe('chat')
  })

  it("an arrival on the agent's home with the workspace open opens no apps under it", () => {
    pinned.apps = [{ id: 'a1', slug: 'board', title: 'Board' }]
    try {
      renderAt('/chat/dev?ws=1', '/chat/:name')
      expect(shown()).toBe('workspace')
      press('workspace')
      expect(shown()).toBe('chat')
      press('apps')
      expect(shown()).toBe('apps')
    } finally {
      pinned.apps = []
    }
  })
})
