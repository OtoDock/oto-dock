import { describe, it, expect, vi } from 'vitest'
import { useCallback, useState, type ReactNode } from 'react'
import { act, render, renderHook } from '@testing-library/react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'

import { useAppSendPrompt, usePendingAppAction } from '@/pages/agent/chat/useOverlayPanels'

// ─── A send_prompt pressed on the full-screen app page arrives on the agent's
//     home in router state. The page's first render has no open socket yet:
//     delivering then sent on a closed socket, the action was refused, and the
//     new chat stayed empty. It waits for the socket, and delivers once. ─────

const pending = {
  app: { id: 'app-1', slug: 'board', title: 'Board' },
  action: { id: 'go', label: 'Go', prompt: 'do it' },
  args: { n: 1 },
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={[{ pathname: '/chat/dev', state: { pendingAppAction: pending } }]}>
      {children}
    </MemoryRouter>
  )
}

describe('usePendingAppAction', () => {
  it('waits for the socket, then delivers the action once', () => {
    const send = vi.fn(async () => ({ status: 'sent' }))
    const hook = renderHook(
      ({ connected, chatId }: { connected: boolean; chatId: string | null }) =>
        usePendingAppAction({ chatId, agentName: 'dev', connected, handleAppSendPrompt: send }),
      { wrapper, initialProps: { connected: false, chatId: null } },
    )
    expect(send).not.toHaveBeenCalled()
    hook.rerender({ connected: true, chatId: null })
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenCalledWith(pending.app, pending.action, pending.args)
    hook.rerender({ connected: false, chatId: null })
    hook.rerender({ connected: true, chatId: null })
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('delivers a second press too: the guard is the press, not the page', () => {
    // The chat page is not remounted between a chat and the agent's home,
    // so a guard of "once per page" lost every press after the first.
    const send = vi.fn(async () => ({ status: 'sent' }))
    let go: (state: object) => void = () => {}
    function Nav({ children }: { children: ReactNode }) {
      const navigate = useNavigate()
      go = (state) => navigate('/chat/dev', { state })
      return <>{children}</>
    }
    const first = { pendingAppAction: { ...pending, key: 'press-1' } }
    renderHook(
      () => usePendingAppAction({ chatId: null, agentName: 'dev', connected: true, handleAppSendPrompt: send }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <MemoryRouter initialEntries={[{ pathname: '/chat/dev', state: first }]}><Nav>{children}</Nav></MemoryRouter>
        ),
      },
    )
    expect(send).toHaveBeenCalledTimes(1)
    act(() => go({ pendingAppAction: { ...pending, key: 'press-2' } }))
    expect(send).toHaveBeenCalledTimes(2)
    act(() => go({ pendingAppAction: { ...pending, key: 'press-2' } }))   // the same press seen again
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('leaves an open chat alone', () => {
    const send = vi.fn(async () => ({ status: 'sent' }))
    renderHook(
      () => usePendingAppAction({ chatId: 'c-1', agentName: 'dev', connected: true, handleAppSendPrompt: send }),
      { wrapper },
    )
    expect(send).not.toHaveBeenCalled()
  })
})

// ─── A button that always starts a new chat, pressed over a chat that drives
//     a terminal. The chat page is one component for a chat and for the
//     agent's home: moving to the home alone left the open chat's id in
//     place, so the press was answered "sent" and never delivered. ──────────

describe('a new_chat press over a terminal chat', () => {
  it('is delivered once into a new terminal, and navigates no further', async () => {
    const routeSend = vi.fn(() => true)
    const sendPtyInput = vi.fn()
    const seen: Array<string | undefined> = []
    let press: () => Promise<{ status: string; reason?: string }> = async () => ({ status: 'unset' })
    let shown: { chatId: string | null; sessionId: string | null; path: string } = { chatId: '', sessionId: '', path: '' }

    function Page() {
      // The chat page's own state, as AgentChat holds it: a chat that drives a terminal is open.
      const [chatId, setChatId] = useState<string | null>('c-1')
      const [sessionId, setSessionId] = useState<string | null>('s-1')
      const navigate = useNavigate()
      const location = useLocation()
      const handleNewChat = useCallback(() => {
        setChatId(null)
        setSessionId(null)
        navigate('/chat/dev', { replace: true })
      }, [navigate])
      const { handleAppSendPrompt } = useAppSendPrompt({
        interactive: { sessionInteractive: true, interactiveMode: true, routeSend, setShowRichView: vi.fn() },
        chatId, ws: { sendPtyInput }, sessionId, warmingUp: false, mode: 'ask', model: 'm',
        chatActiveLayer: null, selectedLayer: null, setAppsOpen: vi.fn(), setWarmingUp: vi.fn(),
        serverKickPendingRef: { current: false }, sendAppAction: vi.fn(), agentName: 'dev',
        keepAppsOnChatEntryRef: { current: false }, handleSelectChat: vi.fn(), handleNewChat,
      } as never)
      usePendingAppAction({ chatId, agentName: 'dev', connected: true, handleAppSendPrompt })
      press = () => handleAppSendPrompt(pending.app as never, { ...pending.action, new_chat: true } as never, {})
      const key = (location.state as { pendingAppAction?: { key?: string } } | null)?.pendingAppAction?.key
      if (key && !seen.includes(key)) seen.push(key)
      shown = { chatId, sessionId, path: location.pathname }
      return null
    }

    render(<MemoryRouter initialEntries={['/chat/dev/c-1']}><Page /></MemoryRouter>)
    let ack = { status: '' } as { status: string; reason?: string }
    await act(async () => { ack = await press() })
    await act(async () => { await Promise.resolve() })
    expect(ack.status).toBe('sent')
    expect(shown).toEqual({ chatId: null, sessionId: null, path: '/chat/dev' })   // the home, with no chat open
    expect(routeSend).toHaveBeenCalledTimes(1)                                    // one delivery, by the cold start
    const [text, route] = routeSend.mock.calls[0] as unknown as [string, { chatId: string | null; sessionId: string | null }]
    expect(text).toContain('do it')
    expect(route.chatId).toBeNull()
    expect(route.sessionId).toBeNull()
    expect(sendPtyInput).not.toHaveBeenCalled()                                   // never typed into the open terminal
    expect(seen).toHaveLength(1)                                                  // the press travelled once
  })
})

