import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'

// ─── Connecting a Claude account: a paste that matched an account already
//     connected says so instead of closing quietly, a refused paste offers a
//     fresh try without opening a popup on its own, and the popup boolean is
//     honoured ───

const h = vi.hoisted(() => ({
  startCalls: [] as unknown[],
  exchangeImpl: { current: async (_v: unknown): Promise<unknown> => ({}) },
  opened: [] as string[],
  openResult: { current: true },
  layersRef: { current: [] as unknown[] },
}))

vi.mock('@/lib/oauth', () => ({
  openOAuthWindow: async (url: string) => { h.opened.push(url); return h.openResult.current },
}))
vi.mock('@/lib/nativeBridge', () => ({ setNativeAuthInProgress: vi.fn() }))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'member' }, setUser: vi.fn() }),
}))
vi.mock('@/api/auth', () => ({ fetchCurrentUser: vi.fn(async () => null) }))
vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  const idle = () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false, isError: false, error: null })
  return {
    ...mod,
    useStartClaudeOAuth: () => ({
      mutateAsync: async (vars: unknown) => {
        h.startCalls.push(vars)
        return { url: `https://platform.claude.com/oauth/authorize?n=${h.startCalls.length}`, state: `st-${h.startCalls.length}` }
      },
      isPending: false,
    }),
    useExchangeClaudeOAuth: () => ({
      mutateAsync: (vars: unknown) => h.exchangeImpl.current(vars),
      isPending: false,
    }),
    useAddSubscription: idle,
    useUserExecutionLayers: () => ({ data: h.layersRef.current, isLoading: false }),
    useUserAddSubscription: idle,
    useUserDeleteSubscription: idle,
    useUserUpdateSubscription: idle,
    useStartOpenAIOAuth: idle,
    useOpenAIOAuthStatus: idle,
    useFinishOpenAIOAuth: idle,
  }
})

import { ClaudeExchangeError } from '@/api/executionLayers'
import { ConnectOAuth } from '@/pages/admin/ExecutionLayersTab.forms'
import { ExecutionLayersSection } from '@/pages/UserSettings.aiEngines'
import { claudeLike } from './fixtures/engines'

// The form reads the engine's descriptor: a code-paste login on a vendor
// called Anthropic whose account is "Claude".
const claude = claudeLike()

const matched = (previous_status: string | null, email = 'a@example.com', id = 's1') => ({
  subscription: { id, oauth_email: email, label: 'Claude Max', status: 'active' },
  subscription_type: 'max', rate_limit_tier: '', created: false, previous_status,
})
const fresh = (email = 'b@example.com') => ({
  subscription: { id: 's2', oauth_email: email, label: 'Claude Max', status: 'active' },
  subscription_type: 'max', rate_limit_tier: '', created: true, previous_status: null,
})

beforeEach(() => {
  h.startCalls.length = 0
  h.opened.length = 0
  h.openResult.current = true
  h.exchangeImpl.current = async () => fresh()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

async function pasteAndConnect(code = 'code-1') {
  // The code box appears two seconds after the popup opened.
  await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
  fireEvent.change(await screen.findByPlaceholderText('Paste authorization code here'), { target: { value: code } })
  fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
}

describe('admin Setup form — Connect Claude', () => {
  it('a new account closes the form', async () => {
    const onDone = vi.fn()
    render(<ConnectOAuth engine={claude} ownerType="platform" onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect Claude Account' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    await waitFor(() => expect(onDone).toHaveBeenCalled())
  })

  it('the same account again stays open and says so, with a Start again that opens a fresh popup', async () => {
    h.exchangeImpl.current = async () => matched('active')
    const onDone = vi.fn()
    render(<ConnectOAuth engine={claude} ownerType="platform" onDone={onDone} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect Claude Account' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    expect(screen.getByText(/to add a second account, sign out of your Claude account there first/)).toBeInTheDocument()
    await pasteAndConnect()
    const notice = await screen.findByTestId('claude-connect-notice')
    expect(notice).toHaveTextContent('That is the account you already connected (a@example.com)')
    expect(notice).toHaveTextContent('sign out of platform.claude.com in this browser first')
    expect(onDone).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Start again' }))
    await waitFor(() => expect(h.opened).toHaveLength(2))
    expect(h.startCalls).toHaveLength(2)
  })

  it('an expired row revived through Connect says reconnected, not already connected', async () => {
    h.exchangeImpl.current = async () => matched('expired')
    render(<ConnectOAuth engine={claude} ownerType="platform" onDone={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect Claude Account' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByTestId('claude-connect-notice')).toHaveTextContent('Reconnected a@example.com')
  })

  it('a refused paste starts a fresh flow and opens the popup only on Try again', async () => {
    h.exchangeImpl.current = async () => { throw new ClaudeExchangeError(400, 'Invalid or expired OAuth state') }
    render(<ConnectOAuth engine={claude} ownerType="platform" onDone={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect Claude Account' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    const retry = await screen.findByRole('button', { name: 'Try again' })
    expect(screen.getByText('Invalid or expired OAuth state')).toBeInTheDocument()
    // A fresh state was fetched, no popup yet.
    expect(h.startCalls).toHaveLength(2)
    expect(h.opened).toHaveLength(1)
    fireEvent.click(retry)
    await waitFor(() => expect(h.opened).toHaveLength(2))
    expect(h.opened[1]).toContain('n=2')
  })

  it('a 403 is not retried', async () => {
    h.exchangeImpl.current = async () => { throw new ClaudeExchangeError(403, 'OAuth state mismatch') }
    render(<ConnectOAuth engine={claude} ownerType="platform" onDone={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect Claude Account' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByText('OAuth state mismatch')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(h.startCalls).toHaveLength(1)
  })

  it('a blocked popup is reported and offers Try again', async () => {
    h.openResult.current = false
    render(<ConnectOAuth engine={claude} ownerType="platform" onDone={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect Claude Account' }))
    expect(await screen.findByText(/The login popup was blocked/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})

function layer(subs: object[]) {
  return {
    name: 'claude-code-cli', display_name: 'Anthropic Claude Code', user_subscriptions: subs,
    platform_available: true, allow_platform_auth: true,
    capabilities: claudeLike({ display_name: 'Anthropic Claude Code' }),
  }
}
const sub = (over: object = {}) => ({
  id: 's1', label: 'Claude Max', oauth_email: 'a@example.com', auth_type: 'oauth',
  status: 'active', use_personal: true, contribute_platform: false, ...over,
})

function renderCard(subs: object[]) {
  h.layersRef.current = [layer(subs)]
  render(<ExecutionLayersSection />)
  fireEvent.click(screen.getByText('Claude Code'))
}

describe('User Settings card — Add Another Account', () => {
  it('the same account again keeps the connect box open with the message', async () => {
    h.exchangeImpl.current = async () => matched('active')
    renderCard([sub()])
    fireEvent.click(screen.getByText('Add Another Account'))
    expect(screen.getByText(/to add a second account, sign out of your Claude account there first/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByText(/That is the account you already connected \(a@example.com\)/)).toBeInTheDocument()
    // The box is still open at its first step.
    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument()
  })

  it('a new account closes the box', async () => {
    renderCard([sub()])
    fireEvent.click(screen.getByText('Add Another Account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    await waitFor(() => expect(screen.queryByPlaceholderText('Paste authorization code here')).toBeNull())
    expect(screen.queryByText(/already connected/)).toBeNull()
  })

  it('Reconnect of an expired A that signs in as already-connected B says so', async () => {
    h.exchangeImpl.current = async () => matched('active', 'b@example.com', 's2')
    renderCard([sub({ status: 'expired' }), sub({ id: 's2', oauth_email: 'b@example.com' })])
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByText(/You signed in as b@example.com, which is already connected/)).toBeInTheDocument()
  })

  it('a refused paste offers Try again with a fresh flow and no popup until the click', async () => {
    h.exchangeImpl.current = async () => { throw new ClaudeExchangeError(400, 'Invalid or expired OAuth state') }
    renderCard([])
    fireEvent.click(screen.getByText('Connect Your Claude Account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    const retry = await screen.findByRole('button', { name: 'Try again' })
    expect(h.startCalls).toHaveLength(2)
    expect(h.opened).toHaveLength(1)
    fireEvent.click(retry)
    await waitFor(() => expect(h.opened).toHaveLength(2))
  })
})
