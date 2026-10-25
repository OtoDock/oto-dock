import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── An agent's GitHub reach (1.9.0) ───
//
// Under the service-account select of a GitHub binding: the repositories
// the agent may reach (a checklist with a search, from what the account
// reaches) and Read only. A token account has no list, a co-manager's
// binding is shown and not changed, a hosted account is offered as the
// account alone, and the server's words reach the manager. A new binding
// that started from the manager's own repositories says so once.
//
// On a person's own GitHub account card the same view sits under each
// agent the account may take: the person narrows their own connection for
// that agent, and doing so for an unchecked agent checks it.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))

import * as authApi from '@/api/auth'
import { ServiceAccountBindingDropdown } from '@/components/ServiceAccountBindingDropdown'
import {
  RECONNECT_REASONS,
  useRemoveAccountAgentBinding,
  useSetAccountAgentBinding,
  type AccountSummary,
  type Integration,
} from '@/api/credentials'
import { AgentOverrideList } from '@/components/accounts/AgentOverrideList'
import { AccountForm } from '@/components/accounts/AccountForm'
import { OAuthAccountForm } from '@/components/accounts/OAuthAccountForm'
import { findAlertDialog, renderWithDialogs } from './dialogsHarness'
import { OAUTH_APP_KIND } from '@/lib/kinds/oauthApp'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

type Call = { url: string; method: string; body?: Record<string, unknown> }

function wrap(ui: React.ReactElement, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<MemoryRouter><QueryClientProvider client={qc}>{ui}</QueryClientProvider></MemoryRouter>)
}

function mockApi(calls: Call[], routes: Record<string, unknown>) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const path = url.split('?')[0]
    for (const [suffix, body] of Object.entries(routes)) {
      if (!path.endsWith(suffix)) continue
      if (body && typeof body === 'object' && 'ok' in body) return body as Response
      return { ok: true, json: async () => body } as Response
    }
    return { ok: true, json: async () => ({}) } as Response
  })
}

afterEach(() => { fetchSpy.mockReset() })

const appAccount = { label: 'alice', display_email: 'alice', is_default: true, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [] }

function options(binding: Record<string, unknown>, accounts: unknown[] = [appAccount]) {
  return {
    reach_supported: true,
    my_accounts: accounts,
    current_binding: {
      label: 'alice', owner_sub: 'u1', owner_name: 'Alice', owner_email: '', set_by: '', set_at: '',
      installation_id: '', installation_account: '', repositories: [], read_only: false, problem: '', ...binding,
    },
  }
}

const listing = {
  pickable: true, from_meta: false,
  installations: [
    { id: 10, account: 'alice', repositories: ['alice/site', 'alice/tools'], truncated: false },
    { id: 11, account: 'octo', repositories: ['octo/hello'], truncated: true },
  ],
}

describe('the GitHub reach editor', () => {
  it('says the agent reaches every repository, then narrows it with the checklist and its search', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options({}),
      '/service-account-repositories': listing,
      '/service-binding': { status: 'ok', repositories: ['octo/hello'] },
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('button', { name: 'Change' }))
    expect(await within(reach).findByText('octo/hello')).toBeInTheDocument()
    expect(within(reach).getByText('The list stopped here. More repositories exist.')).toBeInTheDocument()
    fireEvent.change(within(reach).getByLabelText('Search repositories'), { target: { value: 'HEL' } })
    expect(within(reach).queryByText('alice/site')).not.toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'octo/hello' }))
    fireEvent.click(within(reach).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toMatch(/\/v1\/agents\/a1\/mcps\/github-mcp\/service-binding$/)
    expect(put.body).toEqual({ account_label: 'alice', installation_id: '', repositories: ['octo/hello'] })
    const asked = calls.find((c) => c.url.includes('/service-account-repositories'))!
    expect(asked.url).toContain('account_label=alice')
  })

  it('shows a chosen list, turns Read only on, and words a refusal', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options({
        installation_id: '10', repositories: ['alice/site', 'alice/tools', 'octo/a', 'octo/b'],
      }),
      '/service-binding': { ok: false, status: 400, text: async () => JSON.stringify({ detail: 'alice/zzz is not among the repositories this account reaches.' }) },
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const reach = await screen.findByTestId('github-reach')
    // Sorted and in lower case, as the server's sentences list them.
    expect(within(reach).getByText('Repositories: Only alice/site, alice/tools, octo/a and octo/b.')).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'Read only' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual(
      { account_label: 'alice', installation_id: '10', read_only: true })
    expect(await within(reach).findByText('alice/zzz is not among the repositories this account reaches.')).toBeInTheDocument()
  })

  it('offers a token account Read only alone', async () => {
    mockApi([], {
      '/service-account-options': options({ read_only: true }, [{ label: 'alice', display_email: 'alice', is_default: true }]),
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByText('This account has no list of repositories to pick from.')).toBeInTheDocument()
    // One sentence for "all", with a list to pick from or without.
    expect(within(reach).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    expect(within(reach).queryByRole('button', { name: 'Change' })).not.toBeInTheDocument()
    expect((within(reach).getByRole('checkbox', { name: 'Read only' }) as HTMLInputElement).checked).toBe(true)
  })

  it('lets a token account clear a list kept from the account bound before', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options({ repositories: ['octo/hello'] }, [{ label: 'alice', display_email: 'alice', is_default: true }]),
      '/service-account-repositories': { pickable: false, installations: [], from_meta: false },
      '/service-binding': { status: 'ok', repositories: [] },
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const reach = await screen.findByTestId('github-reach')
    fireEvent.click(within(reach).getByRole('button', { name: 'Change' }))
    expect(await within(reach).findByText(/The list kept from the account bound before still applies/)).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'All repositories the account reaches' }))
    fireEvent.click(within(reach).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ account_label: 'alice', installation_id: '', repositories: [] })
  })

  it("shows a co-manager's reach without letting it change", async () => {
    mockApi([], {
      '/service-account-options': options({ owner_sub: 'u2', owner_name: 'Bob', repositories: ['octo/hello'], read_only: true }),
    })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByText('Repositories: Only octo/hello.')).toBeInTheDocument()
    expect(within(reach).queryByRole('button', { name: 'Change' })).not.toBeInTheDocument()
    expect((within(reach).getByRole('checkbox', { name: 'Read only' }) as HTMLInputElement).disabled).toBe(true)
  })

  it('is absent where the MCP is not GitHub', async () => {
    mockApi([], { '/service-account-options': { ...options({}), reach_supported: false } })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="slack" callerSub="u1" />)
    await screen.findByRole('combobox')
    expect(screen.queryByTestId('github-reach')).not.toBeInTheDocument()
  })
})

describe('hosted sign-in in the service-account select', () => {
  it('offers a hosted account as the account alone and says what to do with a binding left from before', async () => {
    const hosted = { ...appAccount, hosted: true, installations: [
      { id: 10, account: 'alice', target_type: 'User', repository_selection: 'all', repositories_count: 0, suspended: false, admin: true },
    ] }
    const problem = 'hosted sign-in gives an agent no app identity; a manager binds the account itself in Agent Settings, MCPs'
    mockApi([], { '/service-account-options': options({ installation_id: '10', problem }, [hosted]) })
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />)
    const select = await screen.findByRole('combobox')
    const texts = Array.from(select.querySelectorAll('option')).map((o) => o.textContent || '')
    expect(texts.filter((t) => t.startsWith('Installation: alice (')).length).toBe(0)
    expect(screen.getByText(problem)).toBeInTheDocument()
    expect(RECONNECT_REASONS.installation_hosted).toContain('A manager binds the account itself')
    expect(RECONNECT_REASONS.installation_reach_outdated).toContain('picks the repositories again')
  })
})

describe('a new binding of the manager\'s own account', () => {
  it('says once that it starts from the repositories they chose for their own chats', async () => {
    const calls: Call[] = []
    let puts = 0
    fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const path = url.split('?')[0]
      let body: unknown = {}
      if (path.endsWith('/service-account-options')) {
        body = puts
          ? options({ repositories: ['alice/site'], read_only: true })
          : { ...options({}), current_binding: null }
      } else if (path.endsWith('/service-binding')) {
        puts += 1
        if (puts === 2) {
          return { ok: false, status: 503, text: async () => JSON.stringify(
            { detail: 'GitHub could not be reached to check the repositories. Try again in a minute.' }) } as Response
        }
        body = { status: 'ok', reach_copied: puts === 1, repositories: ['alice/site'], read_only: puts === 1 }
      }
      return { ok: true, json: async () => body } as Response
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />, qc)
    fireEvent.change(await screen.findByRole('combobox'), { target: { value: 'bind::u1::alice' } })
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByTestId('github-reach-notice')).toHaveTextContent(
      'Starts from the repositories you chose for your own chats with this agent. Change it here.')
    expect(within(reach).getByText('Repositories: Only alice/site.')).toBeInTheDocument()
    // A refused change leaves the copied list, and the notice, as they were.
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'Read only' }))
    expect(await within(reach).findByText(/GitHub could not be reached/)).toBeInTheDocument()
    expect(within(reach).getByTestId('github-reach-notice')).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'Read only' }))
    await waitFor(() => expect(within(reach).queryByTestId('github-reach-notice')).not.toBeInTheDocument())
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.body)).toEqual([
      { account_label: 'alice', installation_id: '' },
      { account_label: 'alice', installation_id: '', read_only: false },
      { account_label: 'alice', installation_id: '', read_only: false },
    ])
    // The answer of that change cleared the notice for good, not only this view.
    view.unmount()
    wrap(<ServiceAccountBindingDropdown agentName="a1" mcpName="github-mcp" callerSub="u1" />, qc)
    expect(await screen.findByTestId('github-reach')).toBeInTheDocument()
    expect(screen.queryByTestId('github-reach-notice')).not.toBeInTheDocument()
  })
})

const ownAccount: AccountSummary = {
  account_label: 'alice', display_email: 'alice', is_default: true, created_at: '2026-10-21T10:00:00Z',
  configured_keys: [], connected_services: [], missing_scopes: [], kind: OAUTH_APP_KIND.GITHUB_APP,
  agent_overrides: ['a1'], agent_reaches: [{ agent: 'a1', repositories: ['alice/site'], read_only: false }],
}
const workAccount: AccountSummary = {
  ...ownAccount, account_label: 'work', display_email: 'work', is_default: false,
  agent_overrides: ['a3'], agent_reaches: [{ agent: 'a3', repositories: [], read_only: true }],
}
const ownIntegration: Integration = {
  mcp_name: 'github-mcp', display_name: 'GitHub', description: '', configured: true,
  required_keys: [], fields: [], oauth: false, supports_multi_account: true,
  accounts: [ownAccount, workAccount], candidate_agents: ['a1', 'a2', 'a3'], reach_supported: true,
}

function OwnList({ integration, account }: { integration: Integration; account: AccountSummary }) {
  const noop = { mutate: () => {}, isPending: false } as never
  const ops = {
    setIntegration: noop, deleteIntegration: noop, setDefault: noop,
    setBinding: useSetAccountAgentBinding(), removeBinding: useRemoveAccountAgentBinding(),
  }
  return <AgentOverrideList integration={integration} account={account} ops={ops} />
}

function agentRow(name: string): HTMLElement {
  // The label also reads "uses <account>" for an agent set to another one.
  return screen.getByRole('checkbox', { name: new RegExp(`^${name}(?!\\d)`) }).closest('label')!
    .parentElement as HTMLElement
}

describe('a person\'s own GitHub account card', () => {
  it('shows each agent\'s repositories, narrows a checked one from the account\'s own list', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/v1/users/me/integrations/github-mcp/repositories': listing,
      '/agent-binding': { status: 'ok' },
    })
    wrap(<OwnList integration={ownIntegration} account={ownAccount} />)
    fireEvent.click(screen.getByRole('button', { name: 'About Use this account for specific agents' }), { detail: 0 })
    expect(screen.getByRole('tooltip').textContent).toMatch(/You can limit the repositories an agent reaches, or make it read only\./)
    fireEvent.click(screen.getByRole('button', { name: 'About Use this account for specific agents' }), { detail: 0 })
    expect(within(agentRow('a1')).getByText('Repositories: Only alice/site.')).toBeInTheDocument()
    expect(within(agentRow('a2')).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    // An agent set to another account: its repositories live on that card.
    expect(within(agentRow('a3')).queryByTestId('github-reach')).not.toBeInTheDocument()
    expect(within(agentRow('a1')).queryByText(/GitHub reach/)).not.toBeInTheDocument()
    const a1 = agentRow('a1')
    fireEvent.click(within(a1).getByRole('button', { name: 'Change repositories for a1' }))
    fireEvent.click(await within(a1).findByRole('checkbox', { name: 'octo/hello' }))
    fireEvent.click(within(a1).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.url.includes('/repositories'))!.url).toContain('account_label=alice')
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toMatch(/\/v1\/users\/me\/integrations\/github-mcp\/agent-binding$/)
    expect(put.body).toEqual({ agent_name: 'a1', account_label: 'alice', repositories: ['alice/site', 'octo/hello'] })
  })

  it('checks an unchecked agent when it is made read only', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/agent-binding': { status: 'ok', repositories: [], read_only: true } })
    wrap(<OwnList integration={ownIntegration} account={ownAccount} />)
    fireEvent.click(within(agentRow('a2')).getByRole('checkbox', { name: 'Read only for a2' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual(
      { agent_name: 'a2', account_label: 'alice', read_only: true })
  })

  it('offers a token account Read only alone and shows the server\'s refusal', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/agent-binding': { ok: false, status: 404, text: async () => JSON.stringify({ detail: "Account 'alice' not found for github-mcp" }) },
    })
    const token: AccountSummary = { ...ownAccount, kind: undefined, agent_reaches: [{ agent: 'a1', repositories: [], read_only: false }] }
    wrap(<OwnList integration={{ ...ownIntegration, accounts: [token] }} account={token} />)
    const a1 = agentRow('a1')
    expect(within(a1).getByText('This account has no list of repositories to pick from.')).toBeInTheDocument()
    expect(within(a1).queryByRole('button', { name: /Change/ })).not.toBeInTheDocument()
    fireEvent.click(within(a1).getByRole('checkbox', { name: 'Read only for a1' }))
    expect(await within(a1).findByText("Account 'alice' not found for github-mcp")).toBeInTheDocument()
  })

  it('offers a Shared-only agent no checkbox and no repositories, and says where its limit is', () => {
    mockApi([], {})
    wrap(<OwnList integration={{ ...ownIntegration, shared_only_agents: ['a2'] }} account={ownAccount} />)
    const line = "Its chats use the agent's own GitHub account. Limit it under Agent Settings, MCPs."
    const a2 = screen.getByText(line).parentElement as HTMLElement
    expect(within(a2).getByText('a2')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /^a2/ })).not.toBeInTheDocument()
    expect(within(a2).queryByTestId('github-reach')).not.toBeInTheDocument()
    expect(within(agentRow('a1')).getByTestId('github-reach')).toBeInTheDocument()
  })

  it('keeps the checkbox of a Shared-only agent ticked before, so it can be cleared', () => {
    mockApi([], {})
    wrap(
      <OwnList
        integration={{ ...ownIntegration, shared_only_agents: ['a2'] }}
        account={{ ...ownAccount, agent_overrides: ['a2'] }}
      />,
    )
    const a2 = agentRow('a2')
    expect(within(a2).getByRole('checkbox', { name: /^a2/ })).toBeChecked()
    expect(within(a2).getByText(/Limit it under Agent Settings, MCPs/)).toBeInTheDocument()
    expect(within(a2).queryByTestId('github-reach')).not.toBeInTheDocument()
  })

  it('changes nothing on another MCP\'s card', () => {
    mockApi([], {})
    wrap(<OwnList integration={{ ...ownIntegration, reach_supported: false }} account={ownAccount} />)
    expect(screen.queryByTestId('github-reach')).not.toBeInTheDocument()
    expect(screen.queryByText(/limit the repositories an agent reaches/)).not.toBeInTheDocument()
  })

  it('shows the agents on a GitHub card with one account, and not elsewhere', () => {
    mockApi([], {})
    const one = { ...ownIntegration, accounts: [ownAccount] }
    const noop = { mutate: () => {}, mutateAsync: async () => ({}), isPending: false } as never
    const ops = { setIntegration: noop, deleteIntegration: noop, setDefault: noop, setBinding: noop, removeBinding: noop }
    const { unmount } = wrap(<AccountForm integration={one} account={ownAccount} ops={ops} onDone={() => {}} onCancel={() => {}} />)
    expect(screen.getByText('Use this account for specific agents')).toBeInTheDocument()
    unmount()
    wrap(<AccountForm integration={{ ...one, reach_supported: false }} account={ownAccount} ops={ops}
      onDone={() => {}} onCancel={() => {}} />)
    expect(screen.queryByText('Use this account for specific agents')).not.toBeInTheDocument()
  })
})

describe('disconnecting a GitHub account', () => {
  const formIntegration: Integration = {
    ...ownIntegration, oauth: true, oauth_services: [],
    oauth_meta: {
      provider_id: 'github', supports_multi_account: true, registered_app_required: true,
      bearer_required: true, proposed_hosts: [], flows: ['authorization_code'],
      app_kind: OAUTH_APP_KIND.GITHUB_APP,
    },
  }

  async function confirmText(account: AccountSummary): Promise<string> {
    mockApi([], {})
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = renderWithDialogs(
      <MemoryRouter><QueryClientProvider client={qc}>
        <OAuthAccountForm integration={formIntegration} account={account} onDone={() => {}} />
      </QueryClientProvider></MemoryRouter>)
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    const text = (await findAlertDialog(/^Disconnect /)).textContent || ''
    view.unmount()
    return text
  }

  it('says the repositories and Read only chosen with it go too, only when there are some', async () => {
    expect(await confirmText(ownAccount)).toContain(
      'Agents bound to this account fall back to the default account. The repositories and Read only you '
      + 'chose for them with it are cleared.')
    const plain = { ...ownAccount, agent_reaches: [{ agent: 'a1', repositories: [], read_only: false }] }
    const text = await confirmText(plain)
    expect(text).toContain('Agents bound to this account fall back to the default account.')
    expect(text).not.toContain('are cleared')
  })
})
