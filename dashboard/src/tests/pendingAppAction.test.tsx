import { describe, it, expect, vi } from 'vitest'
import type { ReactNode } from 'react'
import { renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import { usePendingAppAction } from '@/pages/agent/chat/useOverlayPanels'

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

  it('leaves an open chat alone', () => {
    const send = vi.fn(async () => ({ status: 'sent' }))
    renderHook(
      () => usePendingAppAction({ chatId: 'c-1', agentName: 'dev', connected: true, handleAppSendPrompt: send }),
      { wrapper },
    )
    expect(send).not.toHaveBeenCalled()
  })
})
