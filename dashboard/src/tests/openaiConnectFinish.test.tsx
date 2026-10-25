import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'

// ─── Connecting a ChatGPT account, the finish: a refused finish keeps the
//     form open with the server's sentence instead of closing as if the
//     account had been stored; a login an earlier finish already consumed
//     still closes it ───

const h = vi.hoisted(() => ({
  finishImpl: { current: async (_v: unknown): Promise<unknown> => ({}) },
  statusImpl: { current: async (): Promise<unknown> => ({ status: 'completed' }) },
  statusCalls: { current: 0 },
  finishCalls: { current: 0 },
  layersRef: { current: [] as unknown[] },
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
    useOpenAIOAuthStatus: () => ({
      mutateAsync: () => { h.statusCalls.current += 1; return h.statusImpl.current() },
      isPending: false,
    }),
    useFinishOpenAIOAuth: () => ({
      mutateAsync: (vars: unknown) => { h.finishCalls.current += 1; return h.finishImpl.current(vars) },
      isPending: false,
    }),
    useStartClaudeOAuth: idle,
    useExchangeClaudeOAuth: idle,
    useAddSubscription: idle,
    useUserExecutionLayers: () => ({ data: h.layersRef.current, isLoading: false }),
    useUserAddSubscription: idle,
    useUserDeleteSubscription: idle,
    useUserUpdateSubscription: idle,
  }
})

import { ConnectOAuth } from '@/pages/admin/ExecutionLayersTab.forms'
import { ExecutionLayersSection } from '@/pages/UserSettings.aiEngines'
import { codexLike } from './fixtures/engines'

const codex = codexLike()

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  h.finishImpl.current = async () => ({})
  h.statusImpl.current = async () => ({ status: 'completed' })
  h.statusCalls.current = 0
  h.finishCalls.current = 0
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

// ─── The same login from User Settings, AI Engines: the steps live in a
//     dialog, and closing it ends the wait for good ───

const sub = (over: object = {}) => ({
  id: 's1', label: 'ChatGPT Plus', oauth_email: 'a@example.com', auth_type: 'oauth',
  status: 'active', use_personal: true, contribute_platform: false, ...over,
})

function renderCodexCard(subs: object[]) {
  h.layersRef.current = [{
    name: 'codex-cli', display_name: 'OpenAI Codex', user_subscriptions: subs,
    platform_available: true, allow_platform_auth: true, capabilities: codex,
  }]
  render(<ExecutionLayersSection />)
  fireEvent.click(screen.getByText('Codex'))
}

describe('User Settings: Connect ChatGPT in a dialog', () => {
  it('the device code shows in the dialog, a finished login closes it', async () => {
    renderCodexCard([])
    fireEvent.click(screen.getByText('Connect your ChatGPT account'))
    const dialog = screen.getByRole('dialog', { name: 'Connect your ChatGPT account' })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByText('ABCD-EFGH')).toBeInTheDocument()
    expect(dialog).toContainElement(screen.getByText('ABCD-EFGH'))
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.finishCalls.current).toBe(1)
  })

  it("the verification page opens without a handle back to this page", async () => {
    h.statusImpl.current = async () => ({ status: 'pending' })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      renderCodexCard([])
      fireEvent.click(screen.getByText('Connect your ChatGPT account'))
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
      await screen.findByText('ABCD-EFGH')
      fireEvent.click(screen.getByRole('button', { name: 'Open' }))
      expect(open).toHaveBeenCalledWith('https://auth.openai.com/device', '_blank', 'noopener,noreferrer')
    } finally {
      open.mockRestore()
    }
  })

  it('a status call slower than the poll is never overlapped, so the login finishes once', async () => {
    let answer: (v: unknown) => void = () => {}
    h.statusImpl.current = () => new Promise((resolve) => { answer = resolve })
    renderCodexCard([])
    fireEvent.click(screen.getByText('Connect your ChatGPT account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await screen.findByText('ABCD-EFGH')
    // Three periods pass while the first status call is still out.
    await act(async () => { await vi.advanceTimersByTimeAsync(6100) })
    expect(h.statusCalls.current).toBe(1)
    await act(async () => { answer({ status: 'completed' }) })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.finishCalls.current).toBe(1)
  })

  it('Cancel stops the poll: no status call after it', async () => {
    h.statusImpl.current = async () => ({ status: 'pending' })
    renderCodexCard([])
    fireEvent.click(screen.getByText('Connect your ChatGPT account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await screen.findByText('ABCD-EFGH')
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    const calls = h.statusCalls.current
    expect(calls).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(6100) })
    expect(h.statusCalls.current).toBe(calls)
  })

  it('a status answer that lands after a cancel finishes nothing', async () => {
    let answer: (v: unknown) => void = () => {}
    h.statusImpl.current = () => new Promise((resolve) => { answer = resolve })
    renderCodexCard([])
    fireEvent.click(screen.getByText('Connect your ChatGPT account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await screen.findByText('ABCD-EFGH')
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(h.statusCalls.current).toBe(1)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { answer({ status: 'completed' }) })
    expect(h.finishCalls.current).toBe(0)
  })

  it('a Reconnect that signs in as another account says so in the dialog', async () => {
    h.finishImpl.current = async () => ({ subscription: sub({ id: 's2', oauth_email: 'b@example.com' }) })
    renderCodexCard([sub({ status: 'expired' })])
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await screen.findByText('ABCD-EFGH')
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(await screen.findByText(/You signed in as b@example.com, so a new subscription was added/)).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
