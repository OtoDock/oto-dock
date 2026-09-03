import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// ─── AI Engines settings card: status-truthful rows (an expired row must not
//     claim "Using your subscription", scope toggles die with the row, and
//     recovery is a per-row Reconnect — not "Connect Different Account") ─────

const { mutation, layersRef } = vi.hoisted(() => ({
  mutation: () => ({ mutate: () => {}, mutateAsync: async () => ({}), isPending: false }),
  layersRef: { current: [] as unknown[] },
}))

vi.mock('@/api/executionLayers', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/api/executionLayers')>()
  return {
    ...mod,
    useUserExecutionLayers: () => ({ data: layersRef.current, isLoading: false }),
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

function layer(subs: object[], overrides: object = {}) {
  return {
    name: 'claude-code-cli',
    display_name: 'Anthropic Claude Code',
    user_subscriptions: subs,
    platform_available: true,
    allow_platform_auth: true,
    ...overrides,
  }
}

function sub(overrides: object = {}) {
  return {
    id: 's1', label: 'Claude Max', oauth_email: 'a@example.com',
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
