import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

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
import { accountEmail, accountName, loginEndsIn, type Subscription } from '@/api/executionLayers'
import { claudeLike, codexLike } from './fixtures/engines'

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

function renderSection(rows: unknown[], path = '/user-settings/ai-engines') {
  layersRef.current = rows
  return render(<MemoryRouter initialEntries={[path]}><ExecutionLayersSection /></MemoryRouter>)
}

function renderCard(subs: object[], overrides: object = {}) {
  renderSection([layer(subs, overrides)])
  // Expand the card so rows render (the card's own span).
  fireEvent.click(screen.getByText('Claude Code', { selector: 'span' }))
}

const codexLayer = (over: object = {}) => ({
  name: 'codex-cli', display_name: 'OpenAI Codex', capabilities: codexLike(),
  user_subscriptions: [], platform_available: false, allow_platform_auth: true, can_run: true, ...over,
})

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
    expect(screen.getByText(/Reconnect the same account to revive/)).toBeInTheDocument()
    // The section button never says "Connect Different Account" anymore.
    expect(screen.getByText('Add another account')).toBeInTheDocument()
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
    expect(screen.getByText('Connect your Claude account')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Use an API key instead'))
    // The engine's vendor is fixed on the user card: no provider select.
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
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

// ─── Engines resolve per person: the order by use and the limit notice ─────

describe('A login that ends soon', () => {
  const H = 3_600_000
  const NOW = Date.parse('2026-10-29T12:00:00Z')
  const row = (over: object) => sub(over) as unknown as Subscription
  const ends = (hours: number, over: object = {}) =>
    loginEndsIn(row({ login_expires_at: new Date(NOW + hours * H).toISOString(), ...over }), NOW)
  // From the wall clock: the page times the row by Date.now().
  const inHours = (hours: number) => new Date(Date.now() + hours * H).toISOString()

  it('says when inside the notice window only, rounded half up with no gap', () => {
    expect(ends(72)).toBe('in 3 days')
    expect(ends(71)).toBe('in 3 days')
    expect(ends(49)).toBe('in 2 days')
    expect(ends(47 + 40 / 60)).toBe('in 2 days')
    expect(ends(47 + 20 / 60)).toBe('in 47 hours')
    expect(ends(5)).toBe('in 5 hours')
    expect(ends(1)).toBe('in 1 hour')
    expect(ends(40 / 60)).toBe('in 1 hour')
    expect(ends(20 / 60)).toBe('within the hour')
    expect(ends(-0.5)).toBe('now')
    expect(ends(72.1)).toBeNull()
    expect(ends(96)).toBeNull()
  })

  it('says nothing on a row it does not apply to', () => {
    expect(ends(10, { status: 'expired' })).toBeNull()
    expect(ends(10, { status: 'disabled' })).toBeNull()
    // An API-key row carries no login end at all.
    expect(loginEndsIn(row({ auth_type: 'api_key' }), NOW)).toBeNull()
    expect(loginEndsIn(row({ login_expires_at: null }), NOW)).toBeNull()
    expect(loginEndsIn(row({ login_expires_at: 'soon' }), NOW)).toBeNull()
    expect(loginEndsIn(row({}), NOW)).toBeNull()
  })

  it('names the account as the notice does', () => {
    expect(accountName({ label: 'Claude Max', oauth_email: 'a@example.com' })).toBe('Claude Max (a@example.com)')
    expect(accountName({ label: 'a@example.com', oauth_email: 'A@Example.com' })).toBe('a@example.com')
    expect(accountName({ label: 'Claude Max', oauth_email: '3f2a9c1e-0000-4000-8000-000000000000' })).toBe('Claude Max')
    expect(accountName({ label: '', oauth_email: 'a@example.com' })).toBe('a@example.com')
    expect(accountName({ label: '', oauth_email: '3f2a9c1e' }, 'the account')).toBe('the account')
    expect(accountEmail(' a@example.com ')).toBe('a@example.com')
    expect(accountEmail('3f2a9c1e-0000-4000-8000-000000000000')).toBe('')
    expect(accountEmail(null)).toBe('')
  })

  it('at 71 hours the card opens by itself, with the chip and Reconnect', () => {
    // No header click: the card is already open, and a click would close it.
    renderSection([layer([sub({ login_expires_at: inHours(71) })])])
    expect(screen.getByText('Login ends in 3 days')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    const dialog = screen.getByRole('dialog', { name: 'Reconnect a@example.com' })
    expect(dialog).toHaveTextContent('sign in again as a@example.com')
  })

  it('under 48 hours the chip counts hours', () => {
    renderSection([layer([sub({ login_expires_at: inHours(5) })])])
    expect(screen.getByText('Login ends in 5 hours')).toBeInTheDocument()
  })

  it('past the instant on a row still active, the chip says the login is ending', () => {
    renderSection([layer([sub({ login_expires_at: inHours(-1) })])])
    expect(screen.getByText('Login ending')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
  })

  it('at four days the card starts closed and the row is as today', () => {
    renderSection([layer([sub({ login_expires_at: inHours(96) })])])
    expect(screen.queryByText('Claude Max')).toBeNull()
    fireEvent.click(screen.getByText('Claude Code', { selector: 'span' }))
    expect(screen.getByText('Claude Max')).toBeInTheDocument()
    expect(screen.queryByText(/Login end/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull()
  })

  it('an expired row is as today, even with its instant inside the window', () => {
    renderCard([sub({ status: 'expired', login_expires_at: inHours(10) })])
    expect(screen.queryByText(/Login end/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
    expect(screen.getByText("This account's login expired. Reconnect the same account to revive it.")).toBeInTheDocument()
  })
})

describe('The engine order', () => {
  it('has no setting: the page says new chats follow use', () => {
    renderSection([layer([sub()], { can_run: true }), codexLayer({ can_run: true })])
    expect(screen.queryByText('Preferred engine')).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Preferred engine' })).toBeNull()
    expect(screen.getByText(/New chats list your engines by how much you use them on each agent\./)).toBeInTheDocument()
  })
})

describe('The limit notice', () => {
  it('the limit link names the account and says where to pick another engine, with no card', () => {
    renderSection([layer([sub()], { can_run: true }), codexLayer({ can_run: true })], '/user-settings/ai-engines?limit=codex-cli')
    const notice = screen.getByTestId('engine-limit-notice')
    expect(notice).toHaveTextContent('Your Codex account reached its limit.')
    expect(notice).toHaveTextContent('Until it resets, pick another engine in a chat’s Model menu. An open chat offers it once its session has ended.')
    expect(notice.querySelector('button')).toBeNull()
    expect(screen.queryByText(/everywhere\?/)).toBeNull()
  })

  it('the limit link with no other engine says to connect one', () => {
    renderSection([layer([sub()], { can_run: true })], '/user-settings/ai-engines?limit=claude-code-cli')
    expect(screen.getByTestId('engine-limit-notice')).toHaveTextContent('No other engine is connected.')
  })

  it('an old switch link draws the page as it is, with no card and no notice', () => {
    renderSection([layer([sub()], { can_run: true }), codexLayer()], '/user-settings/ai-engines?switch=claude-code-cli')
    expect(screen.getByText('AI Engines')).toBeInTheDocument()
    expect(screen.queryByTestId('engine-limit-notice')).toBeNull()
    expect(screen.queryByText(/everywhere\?/)).toBeNull()
  })
})

describe('warmupFailSubtype', () => {
  it('maps every emitted subscription-block reason to the amber card', () => {
    for (const reason of ['auth_off', 'admin_oauth_only', 'no_pool', 'none', 'own_sub_expired', 'local_only']) {
      expect(warmupFailSubtype(reason)).toBe('no_subscription')
    }
  })
  it('keeps availability and crash reasons distinct', () => {
    expect(warmupFailSubtype('target_unavailable')).toBe('target_unavailable')
    // A chat that runs as the agent, below the editor role: a role card.
    expect(warmupFailSubtype('below_editor')).toBe('below_editor')
    expect(warmupFailSubtype('throttled')).toBe('session_error')
    expect(warmupFailSubtype(undefined)).toBe('session_error')
    expect(warmupFailSubtype('anything_else')).toBe('session_error')
  })
})
