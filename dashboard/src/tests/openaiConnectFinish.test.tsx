import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'

// ─── Connecting a ChatGPT account, the finish: a refused finish keeps the
//     form open with the server's sentence instead of closing as if the
//     account had been stored; a login an earlier finish already consumed
//     still closes it ───

const h = vi.hoisted(() => ({
  finishImpl: { current: async (_v: unknown): Promise<unknown> => ({}) },
}))

vi.mock('@/lib/nativeBridge', () => ({ setNativeAuthInProgress: vi.fn() }))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin' }, setUser: vi.fn() }),
}))
vi.mock('@/api/auth', () => ({ fetchCurrentUser: vi.fn(async () => null) }))
vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  const idle = () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false, isError: false, error: null })
  return {
    ...mod,
    useStartOpenAIOAuth: () => ({
      mutateAsync: async () => ({ url: 'https://auth.openai.com/device', user_code: 'ABCD-EFGH', login_id: 'login-1' }),
      isPending: false,
    }),
    useOpenAIOAuthStatus: () => ({ mutateAsync: async () => ({ status: 'completed' }), isPending: false }),
    useFinishOpenAIOAuth: () => ({ mutateAsync: (vars: unknown) => h.finishImpl.current(vars), isPending: false }),
    useStartClaudeOAuth: idle,
    useExchangeClaudeOAuth: idle,
    useAddSubscription: idle,
    useUserExecutionLayers: () => ({ data: [], isLoading: false }),
    useUserAddSubscription: idle,
    useUserDeleteSubscription: idle,
    useUserUpdateSubscription: idle,
  }
})

import { ConnectOAuth } from '@/pages/admin/ExecutionLayersTab.forms'
import { codexLike } from './fixtures/engines'

const codex = codexLike()

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

async function startAndComplete() {
  fireEvent.click(screen.getByRole('button', { name: 'Connect ChatGPT Account' }))
  await screen.findByText('ABCD-EFGH')
  // The status poll runs every two seconds; the first tick answers completed.
  await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
}

describe('admin Setup form: Connect ChatGPT, the finish', () => {
  it('a refused finish keeps the form open with the server sentence', async () => {
    const refusal = 'This login did not identify a ChatGPT account, so it was not stored.'
    h.finishImpl.current = async () => { throw new Error(refusal) }
    const onDone = vi.fn()
    render(<ConnectOAuth engine={codex} ownerType="platform" onDone={onDone} />)
    await startAndComplete()
    expect(await screen.findByText(refusal)).toBeInTheDocument()
    expect(onDone).not.toHaveBeenCalled()
  })

  it('a login an earlier finish consumed still closes the form', async () => {
    h.finishImpl.current = async () => { throw new Error('Login session not found or already finished') }
    const onDone = vi.fn()
    render(<ConnectOAuth engine={codex} ownerType="platform" onDone={onDone} />)
    await startAndComplete()
    await waitFor(() => expect(onDone).toHaveBeenCalled())
  })
})
