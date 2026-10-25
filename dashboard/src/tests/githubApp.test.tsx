import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, renderHook, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── GitHub Apps (1.9.0) ───
//
// The connect form of a GitHub-App kind asks for no services and says what
// to do; a GitHub-App account's card lists its installations and opens the
// install page; the service-identity dropdown offers an installation and
// posts its id; the admin row picks the kind, pastes the key into a
// textarea and posts the kind with the fields; the subscribe modal's app
// mode says events come through the app's webhook. The panel re-reads the
// list when the setup page reports; a bound installation that left the list
// shows as unavailable; the bind's warning and refusal reach the manager; a
// cut repository list says so; a pick list with nothing to pick cannot submit.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))
const opened: string[] = []
vi.mock('@/lib/oauth', () => ({
  openOAuthWindow: async (url: string) => { opened.push(url); return true },
  waitForDeepLink: async () => '',
}))

import * as authApi from '@/api/auth'
import type { AccountSummary, Integration } from '@/api/credentials'
import { OAuthAccountForm } from '@/components/accounts/OAuthAccountForm'
import { AccountCard } from '@/components/accounts/AccountCard'
import { ServiceAccountBindingDropdown } from '@/components/ServiceAccountBindingDropdown'
import { SubscribeToEventsModal } from '@/components/accounts/SubscribeToEventsModal'
import McpServersPage from '@/pages/admin/McpServersPage'
import { InstallationsPanel } from '@/components/accounts/InstallationsPanel'
import { useClearAgentServiceBinding, useSetAgentServiceBinding } from '@/api/serviceAccounts'
import { OAUTH_APP_KIND, REGISTRATION_MODE_APP } from '@/lib/kinds/oauthApp'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

type Call = { url: string; method: string; body?: unknown }

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={qc}>{ui}</QueryClientProvider></MemoryRouter>)
}

function mockApi(calls: Call[], routes: Record<string, unknown>) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    for (const [suffix, body] of Object.entries(routes)) {
      if (!url.endsWith(suffix)) continue
      // A route may answer a whole Response (a refusal).
      if (body && typeof body === 'object' && 'ok' in body) return body as Response
      return { ok: true, json: async () => body } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

afterEach(() => { fetchSpy.mockReset(); opened.length = 0 })

const integration: Integration = {
  mcp_name: 'github-mcp', display_name: 'GitHub', description: '',
  configured: false, required_keys: [], fields: [], oauth: true,
  oauth_services: [{ key: 'repos', label: 'Repositories', description: 'd', scopes: ['repo'] }],
  oauth_meta: {
    provider_id: 'github', supports_multi_account: true, registered_app_required: true,
    bearer_required: true, proposed_hosts: ['api.githubcopilot.com'],
    flows: ['authorization_code', 'personal_access_token'],
    app_kind: OAUTH_APP_KIND.GITHUB_APP, app_install_available: true,
  },
  supports_multi_account: true, accounts: [], candidate_agents: [],
}

const account: AccountSummary = {
  account_label: 'alice', display_email: 'alice', is_default: true, created_at: '2026-10-15T10:00:00Z',
  configured_keys: [], connected_services: [], agent_overrides: [], missing_scopes: [],
  kind: OAUTH_APP_KIND.GITHUB_APP,
  installations: [
    { id: 10, account: 'alice', target_type: 'User', repository_selection: 'selected', repositories: ['alice/repo'], suspended_at: null, admin: true },
    { id: 11, account: 'octo', target_type: 'Organization', repository_selection: 'all', repositories: [], suspended_at: null, admin: false },
  ],
}

describe('the connect form of a GitHub-App kind', () => {
  it('asks for no services, says what to do and starts with none', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/start': { url: 'https://github.com/login/oauth/authorize?x=1' }, '/accounts?mcp_name=github-mcp': { accounts: [], has_service_credentials_only: false } })
    wrap(<OAuthAccountForm integration={integration} account={null} onDone={() => {}} />)
    expect(screen.queryByText('Select which services to connect:')).toBeNull()
    expect(screen.getByText('Sign in with GitHub and install the app on the repositories you choose.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/v1/oauth/github/start'))).toBe(true))
    expect(calls.find(c => c.url.endsWith('/start'))!.body).toMatchObject({ app_kind: OAUTH_APP_KIND.GITHUB_APP })
  })

  it('keeps the services for the OAuth-App kind', () => {
    mockApi([], { '/accounts?mcp_name=github-mcp': { accounts: [], has_service_credentials_only: false } })
    const oauthApp = { ...integration, oauth_meta: { ...integration.oauth_meta!, app_kind: OAUTH_APP_KIND.OAUTH_APP } }
    wrap(<OAuthAccountForm integration={oauthApp} account={null} onDone={() => {}} />)
    expect(screen.getByText('Select which services to connect:')).toBeInTheDocument()
  })
})

// ─── The ways to connect: GitHub's three on a hosted install, the app first;
//     one browser way on a self-managed one; plain sentences elsewhere ───

const ways = () => screen.getAllByRole('radio').map((r) => r.closest('label')!.textContent)
const accountsRoute = { '/accounts?mcp_name=github-mcp': { accounts: [], has_service_credentials_only: false } }
const withKinds = (connect_kinds: string[], app_hosted: boolean) => ({
  ...integration,
  oauth_meta: { ...integration.oauth_meta!, app_kind: connect_kinds[0], connect_kinds, app_hosted },
})

describe('the ways to connect GitHub', () => {
  it('lists the GitHub App first, then GitHub sign-in and the token, on a hosted install', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/start': { url: 'https://github.com/login/oauth/authorize?x=1' }, ...accountsRoute })
    wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.GITHUB_APP, OAUTH_APP_KIND.OAUTH_APP], true)}
      account={null} onDone={() => {}} />)
    expect(ways()).toEqual([
      'GitHub AppRecommendedSign in with GitHub and install the OtoDock app on the repositories you choose.',
      'GitHub sign-inSign in with your personal GitHub account.',
      'Personal Access TokenCreate a personal access token on GitHub and paste it here.',
    ])
    expect((screen.getAllByRole('radio')[0] as HTMLInputElement).checked).toBe(true)
    expect(screen.queryByText('Select which services to connect:')).toBeNull()
    expect(screen.queryByText(/Best for production/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/start'))).toBe(true))
    expect(calls.find(c => c.url.endsWith('/start'))!.body).toMatchObject({ app_kind: OAUTH_APP_KIND.GITHUB_APP })
  })

  it('sends the OAuth App with its services when GitHub sign-in is picked', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/start': { url: 'https://github.com/login/oauth/authorize?x=1' }, ...accountsRoute })
    wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.GITHUB_APP, OAUTH_APP_KIND.OAUTH_APP], true)}
      account={null} onDone={() => {}} />)
    fireEvent.click(screen.getAllByRole('radio')[1])
    expect(screen.getByText('Select which services to connect:')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/start'))).toBe(true))
    expect(calls.find(c => c.url.endsWith('/start'))!.body).toMatchObject({
      app_kind: OAUTH_APP_KIND.OAUTH_APP, services: ['repos'],
    })
  })

  it('reconnects an OAuth App account through the OAuth App', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/start': { url: 'https://github.com/login/oauth/authorize?x=1' }, ...accountsRoute })
    const oauthAccount: AccountSummary = { ...account, kind: undefined, installations: undefined, connected_services: ['repos'] }
    wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.GITHUB_APP, OAUTH_APP_KIND.OAUTH_APP], true)}
      account={oauthAccount} onDone={() => {}} />)
    expect(screen.getByText('Select which services to connect:')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/start'))).toBe(true))
    expect(calls.find(c => c.url.endsWith('/start'))!.body).toMatchObject({
      app_kind: OAUTH_APP_KIND.OAUTH_APP, services: ['repos'], account_label: 'alice',
    })
  })

  it('warns a GitHub-App account that a reconnect without the app loses its installations', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/start': { url: 'https://github.com/login/oauth/authorize?x=1' }, ...accountsRoute })
    wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.OAUTH_APP], true)} account={account} onDone={() => {}} />)
    expect(screen.getByText(/Reconnecting uses GitHub sign-in now\. The account then loses its installations\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/start'))).toBe(true))
    expect(calls.find(c => c.url.endsWith('/start'))!.body).toMatchObject({
      app_kind: OAUTH_APP_KIND.OAUTH_APP, services: ['repos'],
    })
  })

  it('warns an OAuth App account that a reconnect through the app narrows its reach', () => {
    mockApi([], accountsRoute)
    const oauthAccount: AccountSummary = { ...account, kind: undefined, installations: undefined, connected_services: ['repos'] }
    wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.GITHUB_APP], false)} account={oauthAccount} onDone={() => {}} />)
    expect(screen.getByText(/Reconnecting uses the GitHub App now\. The account then reaches only the repositories you install it on\./))
      .toBeInTheDocument()
    expect(screen.queryByText('Select which services to connect:')).toBeNull()
  })

  it('offers the one browser way a self-managed install registered, with the token', () => {
    mockApi([], accountsRoute)
    const { unmount } = wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.GITHUB_APP], false)}
      account={null} onDone={() => {}} />)
    expect(ways()).toEqual([
      'GitHub AppRecommendedSign in with GitHub and install the app on the repositories you choose.',
      'Personal Access TokenCreate a personal access token on GitHub and paste it here.',
    ])
    unmount()
    wrap(<OAuthAccountForm integration={withKinds([OAUTH_APP_KIND.OAUTH_APP], false)} account={null} onDone={() => {}} />)
    expect(ways()).toEqual([
      'GitHub sign-inSign in with your personal GitHub account.',
      'Personal Access TokenCreate a personal access token on GitHub and paste it here.',
    ])
  })

  it('words every other provider in plain sentences', () => {
    mockApi([], { '/accounts?mcp_name=other-mcp': { accounts: [], has_service_credentials_only: false } })
    const other: Integration = {
      ...integration, mcp_name: 'other-mcp', display_name: 'Other',
      oauth_meta: {
        provider_id: 'other', supports_multi_account: true, registered_app_required: false,
        bearer_required: false, proposed_hosts: [],
        flows: ['authorization_code', 'device_code', 'personal_access_token', 'client_credentials'],
      },
    }
    wrap(<OAuthAccountForm integration={other} account={null} onDone={() => {}} />)
    expect(ways()).toEqual([
      'OAuth in the browserSign in with your account in a browser window.',
      'Device codeOpen a page on another device and type the code shown.',
      'Personal Access TokenCreate a token in your account settings and paste it here.',
    ])
  })
})

// ─── What a finished connect re-reads: the account list, and the subscribe
//     dialog's catalog (its picker and its scope line) ───

const CATALOG_KEY = { queryKey: ['webhook-event-catalog', 'github-mcp'] }
const LIST_KEY = { queryKey: ['my-integrations'] }

function spiedForm(form: Integration, acc: AccountSummary | null = null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(qc, 'invalidateQueries')
  render(<MemoryRouter><QueryClientProvider client={qc}>
    <OAuthAccountForm integration={form} account={acc} onDone={() => {}} />
  </QueryClientProvider></MemoryRouter>)
  return invalidate
}

describe('the connect form re-reads what a connect changes', () => {
  const oauthOnly = withKinds([OAUTH_APP_KIND.OAUTH_APP], false)

  it('after the popup reports', () => {
    mockApi([], accountsRoute)
    const invalidate = spiedForm(oauthOnly)
    window.dispatchEvent(new MessageEvent('message', { data: { type: 'oauth-complete' }, origin: window.location.origin }))
    expect(invalidate).toHaveBeenCalledWith(LIST_KEY)
    expect(invalidate).toHaveBeenCalledWith(CATALOG_KEY)
  })

  it('after a saved token', async () => {
    mockApi([], { ...accountsRoute, '/pat/save': { status: 'ok', email: 'alice', account_label: 'alice' } })
    const invalidate = spiedForm(oauthOnly)
    fireEvent.click(screen.getAllByRole('radio')[1])
    fireEvent.change(screen.getByPlaceholderText('Paste your token…'), { target: { value: 'ghp_x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith(CATALOG_KEY))
  })

  it('after a device code', async () => {
    mockApi([], {
      '/accounts?mcp_name=github-mcp': { accounts: [], has_service_credentials_only: false },
      '/device-code/start': { device_code: 'd', user_code: 'U-1', verification_uri: 'https://github.com/login/device', interval: 0.01, expires_in: 60 },
      '/device-code/poll': { status: 'ok', email: 'alice', account_label: 'alice' },
    })
    sessionStorage.clear()
    const form = { ...oauthOnly, oauth_meta: { ...oauthOnly.oauth_meta!, flows: ['device_code'], connect_kinds: undefined, app_kind: undefined } }
    const invalidate = spiedForm(form)
    fireEvent.click(screen.getByRole('button', { name: 'Get device code' }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith(CATALOG_KEY))
    sessionStorage.clear()
  })

  it('after the exchange of a native sign-in', async () => {
    mockApi([], { ...accountsRoute, '/start': { url: 'https://github.com/login/oauth/authorize?x=1' } })
    const w = window as unknown as { Capacitor?: unknown }
    w.Capacitor = { isNativePlatform: () => true }
    try {
      const invalidate = spiedForm(oauthOnly)
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
      await waitFor(() => expect(invalidate).toHaveBeenCalledWith(CATALOG_KEY))
      expect(invalidate).toHaveBeenCalledWith(LIST_KEY)
    } finally {
      delete w.Capacitor
    }
  })

  it('re-reads the ways to connect after a refused start', async () => {
    const refused = { ok: false, status: 409, statusText: 'Conflict',
      text: async () => JSON.stringify({ detail: 'This way to sign in is not set up here. Pick another one.' }),
      json: async () => ({ detail: 'This way to sign in is not set up here. Pick another one.' }) }
    mockApi([], { ...accountsRoute, '/start': refused })
    const invalidate = spiedForm(withKinds([OAUTH_APP_KIND.GITHUB_APP], false))
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByText('This way to sign in is not set up here. Pick another one.')).toBeInTheDocument()
    expect(invalidate).toHaveBeenCalledWith(LIST_KEY)
  })
})

describe('the account card of a GitHub-App account', () => {
  it('lists the installations and opens the install page', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/app/install-url': { url: 'https://github.com/apps/otodock-dev/installations/new?state=s1' } })
    const noop = { mutate: () => {}, isPending: false } as never
    wrap(<AccountCard integration={{ ...integration, accounts: [account] }} account={account}
      ops={{ setIntegration: noop, deleteIntegration: noop, setDefault: noop }} isEditing={false} onEdit={() => {}} onClose={() => {}} />)
    expect(screen.getByText('2 installations')).toBeInTheDocument()
    expect(screen.getByText('octo')).toBeInTheDocument()
    expect(screen.getByText('Not an owner')).toBeInTheDocument()
    expect(screen.getByText(/alice\/repo/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Change on GitHub' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/v1/oauth/github/app/install-url'))).toBe(true))
    const start = calls.find(c => c.url.endsWith('/v1/oauth/github/app/install-url'))!
    expect(start.body).toMatchObject({ mcp_name: 'github-mcp', account_label: 'alice' })
    await waitFor(() => expect(opened[0]).toContain('/installations/new?state=s1'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/v1/oauth/github/app/installations/refresh'))).toBe(true))
  })
})

describe('the installations panel', () => {
  it('re-reads the list when the setup page reports, from our origin alone', () => {
    mockApi([], {})
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    render(<MemoryRouter><QueryClientProvider client={qc}>
      <InstallationsPanel integration={integration} account={account} />
    </QueryClientProvider></MemoryRouter>)
    window.dispatchEvent(new MessageEvent('message', { data: { type: 'oauth-complete' }, origin: 'https://evil.example' }))
    expect(invalidate).not.toHaveBeenCalled()
    window.dispatchEvent(new MessageEvent('message', { data: { type: 'oauth-complete' }, origin: window.location.origin }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['my-integrations'] })
    // The subscribe dialog's installation picker reads the same list.
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['webhook-event-catalog', 'github-mcp'] })
  })

  it('re-reads the subscribe pickers after a Refresh and a binding change', async () => {
    mockApi([], { '/app/installations/refresh': { status: 'ok', installations: [] }, '/service-binding': { status: 'ok' } })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    render(<MemoryRouter><QueryClientProvider client={qc}>
      <InstallationsPanel integration={integration} account={account} />
    </QueryClientProvider></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['webhook-event-catalog', 'github-mcp'] }))
    invalidate.mockClear()
    const { result } = renderHook(() => ({ set: useSetAgentServiceBinding(), clear: useClearAgentServiceBinding() }), {
      wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>,
    })
    await act(() => result.current.set.mutateAsync({ agentName: 'a1', mcpName: 'github-mcp', accountLabel: 'alice' }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['webhook-event-catalog', 'github-mcp'] })
    invalidate.mockClear()
    await act(() => result.current.clear.mutateAsync({ agentName: 'a1', mcpName: 'github-mcp' }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['webhook-event-catalog', 'github-mcp'] })
  })

  it('says a cut repository list has more', () => {
    mockApi([], {})
    const cut = { ...account, installations: [{ ...account.installations![0], repositories: ['a/1', 'a/2'], repositories_truncated: true }] }
    wrap(<InstallationsPanel integration={integration} account={cut} />)
    expect(screen.getByText(/a\/1, a\/2 and more/)).toBeInTheDocument()
  })
})

const bindingOf = (installationId: string) => ({
  label: 'alice', owner_sub: 'u1', owner_name: 'Alice', owner_email: '', set_by: '', set_at: '',
  installation_id: installationId, installation_account: 'alice',
})

describe('the service-identity dropdown', () => {
  it('shows a bound installation that left the list as unavailable, and clears it', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': {
        my_accounts: [{ label: 'alice', display_email: 'alice', is_default: true, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [
          { id: 10, account: 'alice', target_type: 'User', repository_selection: 'all', repositories_count: 0, suspended: true, admin: true },
        ] }],
        current_binding: bindingOf('10'),
      },
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const select = await screen.findByRole('combobox') as HTMLSelectElement
    const selected = select.options[select.selectedIndex]
    expect(selected.textContent).toContain('Installation: alice via alice, unavailable')
    expect(selected.disabled).toBe(true)
    fireEvent.change(select, { target: { value: '__none__' } })
    await waitFor(() => expect(calls.some(c => c.method === 'DELETE' && c.url.endsWith('/service-binding'))).toBe(true))
  })

  it('shows the bind\'s warning and a refusal', async () => {
    const options = {
      my_accounts: [{ label: 'alice', display_email: 'alice', is_default: true, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [
        { id: 10, account: 'alice', target_type: 'User', repository_selection: 'selected', repositories_count: 200, repositories_truncated: true, suspended: false, admin: true },
      ] }],
      current_binding: null,
    }
    mockApi([], { '/service-account-options': options, '/service-binding': { status: 'ok', installation_id: '10', warning: 'GitHub could not be reached; try again in a minute' } })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const select = await screen.findByRole('combobox')
    const inst = Array.from(select.querySelectorAll('option')).find(o => o.textContent?.includes('Installation: alice'))!
    expect(inst.textContent).toContain('200+ repositories')
    fireEvent.change(select, { target: { value: inst.getAttribute('value') } })
    expect(await screen.findByText('GitHub could not be reached; try again in a minute')).toBeInTheDocument()

    mockApi([], { '/service-account-options': options, '/service-binding': { ok: false, status: 404, text: async () => JSON.stringify({ detail: 'Installation 10 cannot be this agent\'s identity: this installation is not one you own on GitHub' }) } })
    const { container } = wrap(<ServiceAccountBindingDropdown agentName="a2" mcpName="github-mcp" callerSub="u1" />)
    const second = await within(container).findByRole('combobox')
    const pick = Array.from(second.querySelectorAll('option')).find(o => o.textContent?.includes('Installation: alice'))!
    fireEvent.change(second, { target: { value: pick.getAttribute('value') } })
    expect(await within(container).findByText(/not one you own on GitHub/)).toBeInTheDocument()
  })

  it('offers an owned installation and posts its id', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': {
        my_accounts: [{ label: 'alice', display_email: 'alice', is_default: true, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [
          { id: 10, account: 'alice', target_type: 'User', repository_selection: 'selected', repositories_count: 1, suspended: false, admin: true },
          { id: 11, account: 'octo', target_type: 'Organization', repository_selection: 'all', repositories_count: 0, suspended: false, admin: false },
        ] }],
        current_binding: null,
      },
      '/service-binding': { status: 'ok' },
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const select = await screen.findByRole('combobox')
    const options = Array.from(select.querySelectorAll('option')).map(o => o.textContent)
    expect(options.some(o => o?.includes('Installation: alice (1 repository) via alice'))).toBe(true)
    expect(options.some(o => o?.includes('Installation: octo'))).toBe(false)
    const inst = Array.from(select.querySelectorAll('option')).find(o => o.textContent?.includes('Installation: alice'))!
    fireEvent.change(select, { target: { value: inst.getAttribute('value') } })
    await waitFor(() => expect(calls.some(c => c.method === 'PUT' && c.url.endsWith('/service-binding'))).toBe(true))
    const put = calls.find(c => c.method === 'PUT')!
    expect(put.body).toEqual({ account_label: 'alice', installation_id: '10' })
  })
})

describe('the subscribe modal', () => {
  it('cannot submit a pick list with nothing to pick, and says what to do', () => {
    wrap(<SubscribeToEventsModal mcpName="github-mcp" accountLabel="alice" providerId="github"
      eventCatalog={[{ key: 'issues', label: 'Issues', description: 'd' }]}
      vendorTargetSpec={{ kind: 'static_list', label: 'Installation', static_options: [],
        help_text: 'Install the app on GitHub first; only installations you own can be subscribed to.' }}
      registrationMode={REGISTRATION_MODE_APP} onClose={() => {}} />)
    expect(screen.getByText(/Install the app on GitHub first/)).toBeInTheDocument()
    expect((screen.getByRole('button', { name: 'Create subscription' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows no delivery line on an installation, in the app mode or the relay one', () => {
    const props = {
      mcpName: 'github-mcp', accountLabel: 'alice', providerId: 'github',
      eventCatalog: [{ key: 'issues', label: 'Issues', description: 'd' }],
      vendorTargetSpec: { kind: 'static_list' as const, label: 'Installation', static_options: [{ value: '10', label: 'alice (1 repository)' }],
        help_text: 'One subscription covers every repository the installation reaches, now and later.' },
      onClose: () => {},
    }
    const { unmount } = wrap(<SubscribeToEventsModal {...props} registrationMode={REGISTRATION_MODE_APP} installationTarget />)
    expect(screen.getByText(/One subscription covers every repository the installation reaches/)).toBeInTheDocument()
    expect(screen.queryByText(/Events come/)).toBeNull()
    expect(screen.queryByText(/paste the webhook URL/)).toBeNull()
    unmount()
    const relay = wrap(<SubscribeToEventsModal {...props} registrationMode="relay" installationTarget />)
    expect(screen.queryByText(/Events come/)).toBeNull()
    relay.unmount()
    // A relay OAuth account (GitHub sign-in, Slack, Notion, Zoom) keeps its line.
    wrap(<SubscribeToEventsModal {...props} vendorTargetSpec={{ kind: 'free_text', label: 'Repository' }} registrationMode="relay" />)
    expect(screen.getByText('Events come through OtoDock. There is nothing to set up anywhere else.')).toBeInTheDocument()
  })
})

describe('the admin MCP row', () => {
  const kinds = {
    key: 'GITHUB_APP_KIND',
    kinds: [
      { kind: OAUTH_APP_KIND.OAUTH_APP, label: 'OAuth App', fields: [{ key: 'GITHUB_CLIENT_ID', label: 'OAuth App Client ID', input_type: 'text' }] },
      { kind: OAUTH_APP_KIND.GITHUB_APP, label: 'GitHub App', mcp_token: 'user', fields: [
        { key: 'GITHUB_APP_ID', label: 'App ID', input_type: 'text' },
        { key: 'GITHUB_APP_PRIVATE_KEY', label: 'Private key (PEM)', input_type: 'multiline_secret' },
      ] },
    ],
  }
  const MCPS = {
    mcps: [{
      name: 'github-mcp', label: 'GitHub', description: 'GitHub', version: '2.1.0',
      category: 'community', author: 'GitHub', author_url: 'https://github.com',
      icon: false, runtime: '', transport: 'streamable_http', source: 'remote:api.githubcopilot.com',
      enabled: true, can_disable: true, patched: false, patch_note: '', credential_type: 'per_user',
      credential_label: 'GitHub Account', skills: [], config_fields: [], config_values: {},
      assignment_mode: 'auto', agents: [], tool_filter_supported: false, tool_filter_arg_name: '',
      tool_filter_regex: '', provider_id: 'github',
      hosted: { oauth_app: { available: true, default_mode: 'self_managed' } },
      app_credential: 'github-oauth-app',
      app_credential_fields: kinds.kinds[0].fields,
      app_kinds: kinds, app_credential_kind: OAUTH_APP_KIND.OAUTH_APP,
    }],
  }
  const schema = {
    'github-mcp': {
      type: 'per_user', label: 'GitHub Account', oauth: true,
      oauth_meta: { provider_id: 'github', supports_multi_account: true, registered_app_required: true,
        bearer_required: true, proposed_hosts: ['api.githubcopilot.com'], flows: ['authorization_code'] },
      app_credential: 'github-oauth-app', app_credential_fields: kinds.kinds[0].fields, app_kinds: kinds,
    },
  }

  it('picks the GitHub App kind, pastes the key and posts the kind with the fields', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/v1/admin/mcps': MCPS, '/update-state': { updates: {}, checked: 0, checked_at: '' },
      '/v1/mcp-credential-schema': schema, '/v1/admin/oauth-client-registrations': { registrations: [] },
    })
    wrap(<McpServersPage />)
    fireEvent.click(await screen.findByText('GitHub'))
    expect(await screen.findByText('OAuth App Credentials')).toBeInTheDocument()
    expect(screen.getByText('OAuth App Client ID')).toBeInTheDocument()
    expect(document.querySelector('textarea')).toBeNull()
    fireEvent.click(screen.getByLabelText('GitHub App'))
    expect(screen.queryByText('OAuth App Client ID')).toBeNull()
    const appId = screen.getByText('App ID').nextElementSibling as HTMLInputElement
    expect(appId.tagName).toBe('INPUT')
    const pem = screen.getByText('Private key (PEM)').nextElementSibling as HTMLTextAreaElement
    expect(pem.tagName).toBe('TEXTAREA')
    expect(pem.getAttribute('spellcheck')).toBe('false')
    expect(screen.getByText(/\/v1\/oauth\/github\/app\/setup/)).toBeInTheDocument()
    expect(screen.getByText(/\/v1\/webhooks\/github\/app/)).toBeInTheDocument()
    fireEvent.change(appId, { target: { value: '4242' } })
    fireEvent.change(pem, { target: { value: '-----BEGIN PRIVATE KEY----- x -----END PRIVATE KEY-----' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some(c => c.method === 'PUT' && c.url.endsWith('/v1/admin/integrations/infra/github-oauth-app'))).toBe(true))
    const put = calls.find(c => c.method === 'PUT')!
    expect(put.body).toMatchObject({ credentials: { GITHUB_APP_KIND: OAUTH_APP_KIND.GITHUB_APP, GITHUB_APP_ID: '4242' } })
  })
})
