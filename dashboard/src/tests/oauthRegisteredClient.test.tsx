import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── An MCP server that names its own authorization server (1.7.1) ───
//
// The connect form says where the person signs in and that no OAuth app is
// needed, offers an optional label, and posts it; an account the server
// marks as needing a reconnect shows the reason on the card and in the
// form; the admin row shows the install's registration at the vendor with
// a Forget that posts the row's id.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/admin/McpInstanceManager', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))
vi.mock('@/lib/oauth', () => ({
  openOAuthWindow: async () => true,
  waitForDeepLink: async () => '',
}))

import * as authApi from '@/api/auth'
import type { Integration } from '@/api/credentials'
import { OAuthAccountForm } from '@/components/accounts/OAuthAccountForm'
import { AccountCard } from '@/components/accounts/AccountCard'
import McpServersPage from '@/pages/admin/McpServersPage'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

type Call = { url: string; method: string; body?: unknown }

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={qc}>{ui}</QueryClientProvider></MemoryRouter>)
}

const integration: Integration = {
  mcp_name: 'notion-hosted-mcp', display_name: 'Notion (hosted)', description: '',
  configured: false, required_keys: [], fields: [], oauth: true,
  oauth_services: [{ key: 'default', label: 'Workspace', description: 'd', scopes: ['default'] }],
  oauth_meta: {
    provider_id: 'notion-hosted', supports_multi_account: true, registered_app_required: false,
    bearer_required: true, proposed_hosts: ['mcp.notion.com'], flows: ['authorization_code_pkce'],
    authorization_server: { registration: 'dynamic', issuer: '', resource_host: 'mcp.notion.com', confidential: false, accepts_app_tokens: false },
  },
  supports_multi_account: true, accounts: [], candidate_agents: [],
}

const deadAccount = {
  account_label: 'ws-1', display_email: 'acme.com', is_default: true, created_at: '2026-10-01T10:00:00Z',
  configured_keys: [], connected_services: ['default'], agent_overrides: [], missing_scopes: [],
  needs_reconnect: true, reconnect_reason: 'expired',
}

function mockApi(calls: Call[], routes: Record<string, unknown>) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    for (const [suffix, body] of Object.entries(routes)) {
      if (url.endsWith(suffix)) return { ok: true, json: async () => body } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

afterEach(() => fetchSpy.mockReset())

describe('the connect form of a registered-client MCP', () => {
  it('says where the person signs in and posts the optional label', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/start': { url: 'https://mcp.notion.com/authorize?x=1' }, '/accounts?mcp_name=notion-hosted-mcp': { accounts: [], has_service_credentials_only: false } })
    wrap(<OAuthAccountForm integration={integration} account={null} onDone={() => {}} />)
    expect(screen.getByText(/Signs you in at/)).toBeInTheDocument()
    expect(screen.getByText('mcp.notion.com')).toBeInTheDocument()
    expect(screen.getByText(/no OAuth app to create/)).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('Account label (optional)'), { target: { value: ' Work ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/v1/oauth/notion-hosted/start'))).toBe(true))
    const start = calls.find(c => c.url.endsWith('/v1/oauth/notion-hosted/start'))!
    expect(start.body).toMatchObject({ mcp_name: 'notion-hosted-mcp', services: ['default'], account_label: 'Work' })
  })

  it('hides its copy and label when the registered client is not the active path', () => {
    mockApi([], { '/accounts?mcp_name=notion-hosted-mcp': { accounts: [], has_service_credentials_only: false } })
    const inactive = { ...integration, oauth_meta: { ...integration.oauth_meta!, authorization_server: { ...integration.oauth_meta!.authorization_server!, active: false } } }
    wrap(<OAuthAccountForm integration={inactive} account={null} onDone={() => {}} />)
    expect(screen.queryByText(/Signs you in at/)).toBeNull()
    expect(screen.queryByPlaceholderText('Account label (optional)')).toBeNull()
    expect(screen.getByRole('button', { name: 'Connect' })).toBeInTheDocument()
  })

  it('shows the reconnect reason for a dead account', () => {
    mockApi([], { '/accounts?mcp_name=notion-hosted-mcp': { accounts: [], has_service_credentials_only: false } })
    wrap(<OAuthAccountForm integration={{ ...integration, accounts: [deadAccount] }} account={deadAccount} onDone={() => {}} />)
    expect(screen.getByText(/Reconnect needed: its access expired and the vendor issued no refresh token/)).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('Account label (optional)')).toBeNull()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
  })
})

describe('the account card', () => {
  it('flags an account that needs a reconnect', () => {
    const noop = { mutate: () => {}, isPending: false } as never
    wrap(<AccountCard integration={{ ...integration, accounts: [deadAccount] }} account={deadAccount}
      ops={{ setIntegration: noop, deleteIntegration: noop, setDefault: noop }} isEditing={false} onEdit={() => {}} onClose={() => {}} />)
    expect(screen.getByText('Reconnect needed')).toBeInTheDocument()
  })
})

describe('the admin MCP row', () => {
  const MCPS = {
    mcps: [{
      name: 'notion-hosted-mcp', label: 'Notion (hosted)', description: 'Notion', version: '1.0.0',
      category: 'community', author: 'Notion', author_url: 'https://developers.notion.com',
      icon: false, runtime: '', transport: 'streamable_http', source: 'remote:mcp.notion.com',
      enabled: true, can_disable: true, patched: false, patch_note: '', credential_type: 'per_user',
      credential_label: 'Notion Account', skills: [], config_fields: [], config_values: {},
      assignment_mode: 'auto', agents: [], tool_filter_supported: false, tool_filter_arg_name: '',
      tool_filter_regex: '', provider_id: 'notion-hosted',
    }],
  }
  const schema = {
    'notion-hosted-mcp': {
      type: 'per_user', label: 'Notion Account', oauth: true,
      oauth_meta: {
        provider_id: 'notion-hosted', supports_multi_account: true, registered_app_required: false,
        bearer_required: true, proposed_hosts: ['mcp.notion.com'], flows: ['authorization_code_pkce'],
        authorization_server: { registration: 'dynamic', issuer: '', resource_host: 'mcp.notion.com', confidential: false, accepts_app_tokens: false },
      },
    },
  }
  const registrations = {
    registrations: [
      { id: 7, issuer: 'https://mcp.notion.com', redirect_uri: 'https://dash.example/v1/oauth/notion-hosted/callback',
        registration_endpoint: 'https://mcp.notion.com/register', client_id: 'cid-live', client_secret_expires_at: '',
        token_endpoint_auth_method: 'none', registration_client_uri: '', client_name: 'OtoDock (dash.example)', scope: 'default',
        created_at: '2026-10-01T10:00:00Z', last_used_at: '2026-10-01T11:00:00Z', revoked_at: '', revoked_reason: '',
        has_secret: false, mcps: ['notion-hosted-mcp'] },
      { id: 3, issuer: 'https://mcp.notion.com', redirect_uri: 'http://localhost:8400/v1/oauth/notion-hosted/callback',
        registration_endpoint: 'https://mcp.notion.com/register', client_id: 'cid-old', client_secret_expires_at: '',
        token_endpoint_auth_method: 'none', registration_client_uri: '', client_name: 'OtoDock (localhost)', scope: '',
        created_at: '2026-09-01T10:00:00Z', last_used_at: '', revoked_at: '2026-09-02T10:00:00Z', revoked_reason: 'forgotten',
        has_secret: false, mcps: ['notion-hosted-mcp'] },
    ],
  }

  it('shows the live registration and forgets it by id', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/v1/admin/mcps': MCPS, '/update-state': { updates: {}, checked: 0, checked_at: '' },
      '/v1/mcp-credential-schema': schema, '/v1/admin/oauth-client-registrations': registrations,
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    wrap(<McpServersPage />)
    fireEvent.click(await screen.findByText('Notion (hosted)'))
    expect(await screen.findByText('Signs in at mcp.notion.com')).toBeInTheDocument()
    expect(await screen.findByText('cid-live')).toBeInTheDocument()
    expect(screen.queryByText('cid-old')).toBeNull()
    expect(screen.getByText(/1 older registration kept/)).toBeInTheDocument()
    expect(screen.getByText(/no OAuth app is created/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Forget' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url.endsWith('/v1/admin/oauth-client-registrations/7/forget'))).toBe(true))
    confirm.mockRestore()
  })

  it('offers the registration as the fallback when the server accepts app tokens', async () => {
    const linear = {
      'notion-hosted-mcp': {
        ...schema['notion-hosted-mcp'],
        oauth_meta: {
          ...schema['notion-hosted-mcp'].oauth_meta,
          authorization_server: { registration: 'dynamic', issuer: '', resource_host: 'mcp.notion.com', confidential: false, accepts_app_tokens: true },
        },
      },
    }
    mockApi([], {
      '/v1/admin/mcps': MCPS, '/update-state': { updates: {}, checked: 0, checked_at: '' },
      '/v1/mcp-credential-schema': linear, '/v1/admin/oauth-client-registrations': registrations,
    })
    wrap(<McpServersPage />)
    fireEvent.click(await screen.findByText('Notion (hosted)'))
    expect(await screen.findByText('Sign in without an app')).toBeInTheDocument()
    expect(screen.getByText(/tools only, no events/)).toBeInTheDocument()
    expect(screen.queryByText('Signs in at mcp.notion.com')).toBeNull()
  })

  // A row with the hosted relay and an admin OAuth app declared, so the
  // hosted choice and the app-credentials form can render.
  const WITH_APP = {
    mcps: [{
      ...MCPS.mcps[0],
      hosted: { oauth_app: { available: true, default_mode: 'self_managed' } },
      app_credential: 'notion-hosted-app',
      app_credential_fields: [{ key: 'NOTION_CLIENT_ID', label: 'Client ID', input_type: 'text' }],
    }],
  }

  it('places the fallback card below the app credentials', async () => {
    const accepting = {
      'notion-hosted-mcp': {
        ...schema['notion-hosted-mcp'],
        oauth_meta: {
          ...schema['notion-hosted-mcp'].oauth_meta,
          authorization_server: { registration: 'dynamic', issuer: '', resource_host: 'mcp.notion.com', confidential: false, accepts_app_tokens: true },
        },
      },
    }
    mockApi([], {
      '/v1/admin/mcps': WITH_APP, '/update-state': { updates: {}, checked: 0, checked_at: '' },
      '/v1/mcp-credential-schema': accepting, '/v1/admin/oauth-client-registrations': registrations,
    })
    wrap(<McpServersPage />)
    fireEvent.click(await screen.findByText('Notion (hosted)'))
    const fallback = await screen.findByText('Sign in without an app')
    const appCreds = screen.getByText('OAuth App Credentials')
    expect(screen.getByText('Hosted via OtoDock')).toBeInTheDocument()
    // The app credentials come first; the registered client is the fallback under them.
    expect(appCreds.compareDocumentPosition(fallback) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows only the registration card when the server takes its own tokens only', async () => {
    mockApi([], {
      '/v1/admin/mcps': WITH_APP, '/update-state': { updates: {}, checked: 0, checked_at: '' },
      '/v1/mcp-credential-schema': schema, '/v1/admin/oauth-client-registrations': registrations,
    })
    wrap(<McpServersPage />)
    fireEvent.click(await screen.findByText('Notion (hosted)'))
    expect(await screen.findByText('Signs in at mcp.notion.com')).toBeInTheDocument()
    // Neither the hosted relay nor an admin OAuth app signs anyone in here.
    expect(screen.queryByText('Hosted via OtoDock')).toBeNull()
    expect(screen.queryByText('OAuth App Credentials')).toBeNull()
  })
})
