import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// ─── AI Engines settings card: status-truthful rows (an expired row must not
//     claim "Using your subscription", scope toggles die with the row, and
//     recovery is a per-row Reconnect — not "Connect Different Account") ─────

const { mutation, layersRef, added } = vi.hoisted(() => ({
  mutation: () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false }),
  layersRef: { current: [] as unknown[] },
  added: [] as unknown[],
}))

vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  return {
    ...mod,
    useUserExecutionLayers: () => ({ data: layersRef.current, isLoading: false }),
    useUserAddSubscription: () => ({
      mutate: (vars: unknown) => { added.push(vars) }, isPending: false, isError: false, error: null,
    }),
    useUserDeleteSubscription: mutation,
    useUserUpdateSubscription: mutation,
    useStartClaudeOAuth: mutation,
    useExchangeClaudeOAuth: mutation,
    useStartOpenAIOAuth: mutation,
    useOpenAIOAuthStatus: mutation,
    useFinishOpenAIOAuth: mutation,
  }
})
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'member' }, setUser: vi.fn() }),
}))
vi.mock('@/api/auth', () => ({ fetchCurrentUser: vi.fn(async () => null) }))
vi.mock('@/lib/nativeBridge', () => ({ setNativeAuthInProgress: vi.fn() }))

import { ExecutionLayersSection } from '@/pages/UserSettings.aiEngines'
import { claudeLike } from './fixtures/engines'

function layer(subs: object[], overrides: object = {}) {
  return {
    name: 'claude-code-cli',
    display_name: 'Anthropic Claude Code',
    // The card reads the engine's descriptor (vendor, account, login flow,
    // key provider), not its id.
    capabilities: claudeLike({ display_name: 'Anthropic Claude Code' }),
    user_subscriptions: subs,
    platform_available: true,
    allow_platform_auth: true,
    ...overrides,
  }
}

function sub(overrides: object = {}) {
  return {
    id: 's1', label: 'Claude Max', oauth_email: 'a@example.com', auth_type: 'oauth',
    status: 'active', use_personal: true, contribute_platform: false,
    ...overrides,
  }
}

function renderCard(subs: object[], overrides: object = {}) {
  layersRef.current = [layer(subs, overrides)]
  render(<ExecutionLayersSection />)
  // Expand the card so rows render.
  fireEvent.click(screen.getByText('Claude Code'))
}

describe('AI Engines settings card', () => {
  it('active row: subscription claimed, toggles live, no Reconnect', () => {
    renderCard([sub()])
    expect(screen.getByText('Using your subscription')).toBeInTheDocument()
    expect(screen.queryByText('Reconnect')).toBeNull()
    expect(screen.getByLabelText('Personal use')).not.toBeDisabled()
    expect(screen.getByText('active')).toBeInTheDocument()
  })

  it('expired row: truthful subtitle, amber state, Reconnect, dead toggles', () => {
    renderCard([sub({ status: 'expired' })])
    expect(screen.getByText('Your subscription needs reconnecting')).toBeInTheDocument()
    expect(screen.getByText('Reconnect Needed')).toBeInTheDocument()
    expect(screen.getByText('Reconnect')).toBeInTheDocument()
    expect(screen.getByLabelText('Personal use')).toBeDisabled()
    expect(screen.getByText(/reconnect the same account to revive/)).toBeInTheDocument()
    // The section button never says "Connect Different Account" anymore.
    expect(screen.getByText('Add Another Account')).toBeInTheDocument()
  })

  it('disabled row: admin note, no Reconnect', () => {
    renderCard([sub({ status: 'disabled' })])
    expect(screen.getByText('Disabled by an administrator.')).toBeInTheDocument()
    expect(screen.queryByText('Reconnect')).toBeNull()
    expect(screen.getByLabelText('Personal use')).toBeDisabled()
  })

  it('one expired + one active: subscription still claimed, only the dead row offers Reconnect', () => {
    renderCard([sub(), sub({ id: 's2', status: 'expired', oauth_email: 'b@example.com' })])
    expect(screen.getByText('Using your subscription')).toBeInTheDocument()
    expect(screen.getAllByText('Reconnect')).toHaveLength(1)
    // One usable account: nothing to balance, no hint.
    expect(screen.queryByTestId('balance-hint')).toBeNull()
  })

  it('two active accounts: the balance hint and each account\'s windows', () => {
    const windows = {
      five_hour: { pct: 34, resets_at: '2099-01-01T14:00:00+00:00' },
      seven_day: { pct: 71, resets_at: '2099-01-03T09:00:00+00:00' },
      scoped: [{ key: 'fable', label: 'Fable', pct: 100, resets_at: '2099-01-03T09:00:00+00:00', active: true }],
      reached: 'scoped:fable', plan: 'max', observed_at: new Date().toISOString(), source: 'poll',
    }
    renderCard([sub({ windows }), sub({ id: 's2', oauth_email: 'b@example.com', windows: null })])
    expect(screen.getByTestId('balance-hint')).toHaveTextContent(
      'Enable all your subscriptions to balance the load automatically.')
    expect(screen.getByText('Session')).toBeInTheDocument()
    expect(screen.getByText('71%')).toBeInTheDocument()
    expect(screen.getByText('Fable')).toBeInTheDocument()
    expect(screen.getByText('Usage not read yet.')).toBeInTheDocument()
  })

  it('an API-key row: its own label and badge, no windows, no balance hint', () => {
    renderCard([sub({ id: 'k1', label: '', oauth_email: '', auth_type: 'api_key' })])
    expect(screen.getByText('Using your API key')).toBeInTheDocument()
    expect(screen.getByText('Anthropic API key')).toBeInTheDocument()
    expect(screen.getByText('API key')).toBeInTheDocument()
    expect(screen.queryByText('Claude Subscription')).toBeNull()
    expect(screen.queryByText('Usage not read yet.')).toBeNull()
    expect(screen.getByLabelText('Personal use')).not.toBeDisabled()
    expect(screen.getByText('Remove')).toBeInTheDocument()
  })

  it('"Use an API key instead" posts the key to the user endpoint, out of the pool', () => {
    renderCard([])
    expect(screen.getByText('Connect Your Claude Account')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Use an API key instead'))
    // The engine's vendor is fixed on the user card: no provider select.
    expect(screen.queryByRole('combobox')).toBeNull()
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: ' sk-ant-test ' } })
    fireEvent.click(screen.getByText('Add'))
    expect(added).toEqual([{
      layer: 'claude-code-cli', provider: 'anthropic', auth_type: 'api_key',
      label: '', api_key: 'sk-ant-test', contribute_platform: false,
    }])
  })
})

// ─── warmup_failed reason → system-card subtype mapping (the amber setup card
//     must be reachable by the reasons the backend actually emits) ───────────

import { warmupFailSubtype } from '@/hooks/useChatStream'

describe('warmupFailSubtype', () => {
  it('maps every emitted subscription-block reason to the amber card', () => {
    for (const reason of ['auth_off', 'admin_oauth_only', 'no_pool', 'none', 'own_sub_expired']) {
      expect(warmupFailSubtype(reason)).toBe('no_subscription')
    }
  })
  it('keeps availability and crash reasons distinct', () => {
    expect(warmupFailSubtype('target_unavailable')).toBe('target_unavailable')
    expect(warmupFailSubtype('throttled')).toBe('session_error')
    expect(warmupFailSubtype(undefined)).toBe('session_error')
    expect(warmupFailSubtype('anything_else')).toBe('session_error')
  })
})
