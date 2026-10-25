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
  // Set: the start waits for it (a slow or hung start).
  startGate: { current: null as Promise<void> | null },
  exchangePending: { current: false },
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
        if (h.startGate.current) await h.startGate.current
        return { url: `https://platform.claude.com/oauth/authorize?n=${h.startCalls.length}`, state: `st-${h.startCalls.length}` }
      },
      isPending: false,
    }),
    useExchangeClaudeOAuth: () => ({
      mutateAsync: (vars: unknown) => h.exchangeImpl.current(vars),
      isPending: h.exchangePending.current,
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
  h.startGate.current = null
  h.exchangePending.current = false
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
    expect(screen.getByText(/To add a second account, sign out of your Claude account there first/)).toBeInTheDocument()
    await pasteAndConnect()
    const notice = await screen.findByTestId('claude-connect-notice')
    expect(notice).toHaveTextContent('That is the account you already connected: a@example.com.')
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

describe('User Settings card — Add another account', () => {
  it('the same account again keeps the connect box open with the message', async () => {
    h.exchangeImpl.current = async () => matched('active')
    renderCard([sub()])
    fireEvent.click(screen.getByText('Add another account'))
    expect(screen.getByText(/To add a second account, sign out of your Claude account there first/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByText(/That is the account you already connected: a@example.com\./)).toBeInTheDocument()
    // The box is still open at its first step.
    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument()
  })

  it('a new account closes the box', async () => {
    renderCard([sub()])
    fireEvent.click(screen.getByText('Add another account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    await waitFor(() => expect(screen.queryByPlaceholderText('Paste authorization code here')).toBeNull())
    expect(screen.queryByText(/already connected/)).toBeNull()
  })

  it('Reconnect of an expired A that signs in as already-connected B says so, inside the dialog', async () => {
    h.exchangeImpl.current = async () => matched('active', 'b@example.com', 's2')
    renderCard([sub({ status: 'expired' }), sub({ id: 's2', oauth_email: 'b@example.com' })])
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    // The dialog names the account to sign in as; the popup opens on its click.
    const dialog = screen.getByRole('dialog', { name: 'Reconnect a@example.com' })
    expect(dialog).toHaveTextContent('sign in again as a@example.com')
    expect(h.opened).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByText(/You signed in as b@example.com, which is already connected/)).toBeInTheDocument()
    // The dialog stays open with the sentence until Done.
    expect(dialog).toHaveTextContent('“Claude Max” is still expired')
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Reconnect of the same account closes the dialog', async () => {
    h.exchangeImpl.current = async () => matched('expired', 'a@example.com', 's1')
    renderCard([sub({ status: 'expired' })])
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('on a desktop the paste step focuses its field', async () => {
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(screen.getByPlaceholderText('Paste authorization code here')).toHaveFocus()
  })

  it('on a phone the paste field is the next tap, not focused by code', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q === '(pointer: coarse)', media: q, addEventListener: () => {}, removeEventListener: () => {},
    }))
    try {
      renderCard([])
      fireEvent.click(screen.getByText('Connect your Claude account'))
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
      await waitFor(() => expect(h.opened).toHaveLength(1))
      await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
      const field = screen.getByPlaceholderText('Paste authorization code here')
      expect(field).not.toHaveFocus()
      // The focus stays inside the dialog, never on the page behind it.
      expect(screen.getByRole('dialog')).toContainElement(document.activeElement as HTMLElement)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('a 403 paste is not retried in the dialog either', async () => {
    h.exchangeImpl.current = async () => { throw new ClaudeExchangeError(403, 'OAuth state mismatch') }
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await pasteAndConnect()
    expect(await screen.findByText('OAuth state mismatch')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(h.startCalls).toHaveLength(1)
  })

  it('a start that answers after a Cancel opens no popup', async () => {
    let release: () => void = () => {}
    h.startGate.current = new Promise<void>((r) => { release = r })
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.startCalls).toHaveLength(1))
    // A slow start does not lock the dialog: Cancel stays.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { release() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(h.opened).toHaveLength(0)
  })

  it("the Android back closes the dialog, except while the code is being exchanged", async () => {
    const w = window as unknown as { __otodockDialogClose?: () => void }
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    h.exchangePending.current = true
    fireEvent.change(screen.getByPlaceholderText('Paste authorization code here'), { target: { value: 'c' } })
    act(() => { w.__otodockDialogClose?.() })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    h.exchangePending.current = false
    fireEvent.change(screen.getByPlaceholderText('Paste authorization code here'), { target: { value: 'cd' } })
    act(() => { w.__otodockDialogClose?.() })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('Cancel before the popup opens leaves no popup and no dialog', async () => {
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
    expect(screen.getByRole('dialog', { name: 'Connect your Claude account' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(h.opened).toHaveLength(0)
  })

  it('a cancel while the popup opens drops the paste step that would follow', async () => {
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(h.opened).toHaveLength(1))
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    await act(async () => { await vi.advanceTimersByTimeAsync(2100) })
    expect(screen.queryByPlaceholderText('Paste authorization code here')).toBeNull()
  })

  it('a refused paste offers Try again with a fresh flow and no popup until the click', async () => {
    h.exchangeImpl.current = async () => { throw new ClaudeExchangeError(400, 'Invalid or expired OAuth state') }
    renderCard([])
    fireEvent.click(screen.getByText('Connect your Claude account'))
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
