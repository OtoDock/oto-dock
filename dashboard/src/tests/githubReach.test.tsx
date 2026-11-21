import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── An agent's GitHub reach (1.9.0) ───
//
// Under each of the agent's own GitHub accounts on Agent Settings, MCPs:
// the repositories the agent may reach on it (a checklist with a search,
// from what the account reaches) and Read only. A token account has no
// list, a co-manager's account is shown and not changed, a hosted account
// is offered as the account alone, and the server's words reach the
// manager. A new account that started from the manager's own repositories
// says so once.
//
// On a person's own GitHub account card the same view sits under each
// agent the account may take: the person narrows their own connection for
// that agent, and doing so for an unchecked agent checks it. An agent may
// use several of the person's accounts: each card ticks it on its own, and
// one of them is the agent's default.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))

import * as authApi from '@/api/auth'
import { AgentAccounts } from '@/components/accounts/AgentAccounts'
import {
  RECONNECT_REASONS,
  useAddAgentAccount,
  useRemoveAgentAccount,
  useSetAgentDefaultAccount,
  type AccountSummary,
  type Integration,
} from '@/api/credentials'
import { AgentOverrideList } from '@/components/accounts/AgentOverrideList'
import { AccountForm } from '@/components/accounts/AccountForm'
import { AccountCard } from '@/components/accounts/AccountCard'
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

/** The options of an agent holding the one account `alice` (a GitHub-App
 * one unless `row` says otherwise), with `mine` the caller's own accounts. */
function options(row: Record<string, unknown>, mine: unknown[] = [appAccount]) {
  return {
    reach_supported: true,
    my_accounts: mine,
    accounts: [{
      label: 'alice', display_email: 'alice', owner_sub: 'u1', owner_name: 'Alice', is_default: true,
      stands: true, mine: true, installation_id: '', installation_account: '', kind: OAUTH_APP_KIND.GITHUB_APP,
      repositories: [], read_only: false, problem: '', ...row,
    }],
  }
}

const tokenRow = { kind: undefined }

/** The agent's accounts of GitHub, as Agent Settings, MCPs shows them. */
const accounts = <AgentAccounts agentName="a1" mcpName="github-mcp" chatsUseThem={false} />

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
      '/service-accounts/reach': { status: 'ok', accounts: [] },
    })
    wrap(accounts)
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByText('GitHub reach')).toBeInTheDocument()
    expect(within(reach).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('button', { name: 'Change repositories for alice' }))
    expect(await within(reach).findByText('octo/hello')).toBeInTheDocument()
    expect(within(reach).getByText('The list stopped here. More repositories exist.')).toBeInTheDocument()
    fireEvent.change(within(reach).getByLabelText('Search repositories'), { target: { value: 'HEL' } })
    expect(within(reach).queryByText('alice/site')).not.toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'octo/hello' }))
    fireEvent.click(within(reach).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toMatch(/\/v1\/agents\/a1\/mcps\/github-mcp\/service-accounts\/reach$/)
    expect(put.body).toEqual({ account_label: 'alice', installation_id: '', repositories: ['octo/hello'] })
    const asked = calls.find((c) => c.url.includes('/service-account-repositories'))!
    expect(asked.url).toContain('account_label=alice')
  })

  it('shows a chosen list, turns Read only on, and words a refusal', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options({
        installation_id: '10', installation_account: 'alice', repositories: ['alice/site', 'alice/tools', 'octo/a', 'octo/b'],
      }),
      '/service-accounts/reach': { ok: false, status: 400, text: async () => JSON.stringify({ detail: 'alice/zzz is not among the repositories this account reaches.' }) },
    })
    wrap(accounts)
    const reach = await screen.findByTestId('github-reach')
    // Sorted and in lower case, as the server's sentences list them.
    expect(within(reach).getByText('Repositories: Only alice/site, alice/tools, octo/a and octo/b.')).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'Read only for alice' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    // The reach is the installation's the row acts as.
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual(
      { account_label: 'alice', installation_id: '10', read_only: true })
    expect(await within(reach).findByText('alice/zzz is not among the repositories this account reaches.')).toBeInTheDocument()
  })

  it('offers a token account Read only alone', async () => {
    mockApi([], {
      '/service-account-options': options({ ...tokenRow, read_only: true }, [{ label: 'alice', display_email: 'alice', is_default: true }]),
    })
    wrap(accounts)
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByText('This account has no list of repositories to pick from.')).toBeInTheDocument()
    // One sentence for "all", with a list to pick from or without.
    expect(within(reach).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    expect(within(reach).queryByRole('button', { name: /Change/ })).not.toBeInTheDocument()
    expect((within(reach).getByRole('checkbox', { name: 'Read only for alice' }) as HTMLInputElement).checked).toBe(true)
  })

  it('lets a token account clear a list kept from the account bound before', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options({ ...tokenRow, repositories: ['octo/hello'] }, [{ label: 'alice', display_email: 'alice', is_default: true }]),
      '/service-account-repositories': { pickable: false, installations: [], from_meta: false },
      '/service-accounts/reach': { status: 'ok', accounts: [] },
    })
    wrap(accounts)
    const reach = await screen.findByTestId('github-reach')
    fireEvent.click(within(reach).getByRole('button', { name: 'Change repositories for alice' }))
    expect(await within(reach).findByText(/The list kept from the account bound before still applies/)).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'All repositories the account reaches' }))
    fireEvent.click(within(reach).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ account_label: 'alice', installation_id: '', repositories: [] })
  })

  it("shows a co-manager's reach without letting it change", async () => {
    mockApi([], {
      '/service-account-options': options({ owner_sub: 'u2', owner_name: 'Bob', mine: false, repositories: ['octo/hello'], read_only: true }),
    })
    wrap(accounts)
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByText('Repositories: Only octo/hello.')).toBeInTheDocument()
    expect(within(reach).queryByRole('button', { name: /Change/ })).not.toBeInTheDocument()
    expect((within(reach).getByRole('checkbox', { name: 'Read only for alice' }) as HTMLInputElement).disabled).toBe(true)
  })

  it('is absent where the MCP is not GitHub', async () => {
    mockApi([], { '/service-account-options': { ...options({}), reach_supported: false } })
    wrap(<AgentAccounts agentName="a1" mcpName="slack" chatsUseThem={false} />)
    await screen.findByTestId('service-account-row')
    expect(screen.queryByTestId('github-reach')).not.toBeInTheDocument()
  })
})

describe('hosted sign-in in the Add an account select', () => {
  it('offers a hosted account as the account alone and says what to do with an installation left from before', async () => {
    const hosted = (label: string, id: number) => ({ ...appAccount, label, display_email: label, hosted: true, installations: [
      { id, account: label, target_type: 'User', repository_selection: 'all', repositories_count: 0, suspended: false, admin: true },
    ] })
    const problem = 'hosted sign-in gives an agent no app identity; a manager adds the account itself in Agent Settings, MCPs'
    mockApi([], {
      '/service-account-options': options({ installation_id: '10', installation_account: 'alice', problem }, [hosted('alice', 10), hosted('team', 11)]),
    })
    wrap(accounts)
    const select = await screen.findByRole('combobox', { name: 'Add an account' })
    const texts = Array.from(select.querySelectorAll('option')).map((o) => o.textContent || '')
    expect(texts).toEqual(['Add an account', 'team', 'Connect another account'])
    expect(texts.filter((t) => t.startsWith('Installation:')).length).toBe(0)
    const row = screen.getByTestId('service-account-row')
    expect(within(row).getByText('Installation: alice')).toBeInTheDocument()
    expect(within(row).getByText(problem)).toBeInTheDocument()
    expect(RECONNECT_REASONS.installation_hosted).toContain('A manager adds the account itself')
    expect(RECONNECT_REASONS.installation_reach_outdated).toContain('picks the repositories again')
  })
})

describe("a new GitHub account of the manager's own", () => {
  it('says once that it starts from the repositories they chose for their own chats', async () => {
    const calls: Call[] = []
    let added = false
    let reachPuts = 0
    fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const path = url.split('?')[0]
      let body: unknown = {}
      if (path.endsWith('/service-account-options')) {
        body = added
          ? options({ repositories: ['alice/site'], read_only: reachPuts < 2 })
          : { ...options({}), accounts: [] }
      } else if (method === 'PUT' && path.endsWith('/service-accounts')) {
        added = true
        body = { status: 'ok', accounts: [], reach_copied: true }
      } else if (method === 'PUT' && path.endsWith('/service-accounts/reach')) {
        reachPuts += 1
        if (reachPuts === 1) {
          return { ok: false, status: 503, text: async () => JSON.stringify(
            { detail: 'GitHub could not be reached to check the repositories. Try again in a minute.' }) } as Response
        }
        body = { status: 'ok', accounts: [] }
      }
      return { ok: true, json: async () => body } as Response
    })
    wrap(accounts)
    const add = await screen.findByRole('combobox', { name: 'Add an account' })
    const alice = Array.from(add.querySelectorAll('option')).find((o) => o.textContent === 'alice')!
    fireEvent.change(add, { target: { value: alice.getAttribute('value') } })
    const reach = await screen.findByTestId('github-reach')
    expect(within(reach).getByTestId('github-reach-notice')).toHaveTextContent(
      'Starts from the repositories you chose for your own chats with this agent. Change it here.')
    expect(within(reach).getByText('Repositories: Only alice/site.')).toBeInTheDocument()
    // A refused change leaves the copied list, and the notice, as they were.
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'Read only for alice' }))
    expect(await within(reach).findByText(/GitHub could not be reached/)).toBeInTheDocument()
    expect(within(reach).getByTestId('github-reach-notice')).toBeInTheDocument()
    fireEvent.click(within(reach).getByRole('checkbox', { name: 'Read only for alice' }))
    await waitFor(() => expect(within(reach).queryByTestId('github-reach-notice')).not.toBeInTheDocument())
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.body)).toEqual([
      { account_label: 'alice' },
      { account_label: 'alice', installation_id: '', read_only: false },
      { account_label: 'alice', installation_id: '', read_only: false },
    ])
  })
})

const ownAccount: AccountSummary = {
  account_label: 'alice', display_email: 'alice', is_default: true, created_at: '2026-10-21T10:00:00Z',
  configured_keys: [], connected_services: [], missing_scopes: [], kind: OAUTH_APP_KIND.GITHUB_APP,
  agent_overrides: ['a1'], agent_defaults: ['a1'],
  agent_reaches: [{ agent: 'a1', repositories: ['alice/site'], read_only: false }],
}
const workAccount: AccountSummary = {
  ...ownAccount, account_label: 'work', display_email: 'work', is_default: false,
  agent_overrides: ['a3'], agent_defaults: ['a3'],
  agent_reaches: [{ agent: 'a3', repositories: [], read_only: true }],
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
    addAgentAccount: useAddAgentAccount(), removeAgentAccount: useRemoveAgentAccount(),
    setAgentDefault: useSetAgentDefaultAccount(),
  }
  return <AgentOverrideList integration={integration} account={account} ops={ops} />
}

function agentRow(name: string): HTMLElement {
  return screen.getByRole('checkbox', { name }).closest('[data-testid="agent-account-row"]') as HTMLElement
}

describe('a person\'s own GitHub account card', () => {
  it('shows each agent\'s repositories, narrows a checked one from the account\'s own list', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/v1/users/me/integrations/github-mcp/repositories': listing,
      '/agent-accounts': { status: 'ok', is_default: true },
    })
    wrap(<OwnList integration={ownIntegration} account={ownAccount} />)
    fireEvent.click(screen.getByRole('button', { name: 'About Use this account for specific agents' }), { detail: 0 })
    expect(screen.getByRole('tooltip').textContent).toMatch(/You can limit the repositories an agent reaches, or make it read only\./)
    fireEvent.click(screen.getByRole('button', { name: 'About Use this account for specific agents' }), { detail: 0 })
    expect(within(agentRow('a1')).getByText('Repositories: Only alice/site.')).toBeInTheDocument()
    expect(within(agentRow('a2')).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    // An agent that uses another account may take this one too: its reach
    // with this account is offered here, and nothing greys it out.
    expect(within(agentRow('a3')).getByRole('checkbox', { name: 'a3' })).not.toBeChecked()
    expect(within(agentRow('a3')).getByTestId('github-reach')).toBeInTheDocument()
    expect(screen.queryByText(/uses work/)).not.toBeInTheDocument()
    expect(within(agentRow('a1')).queryByText(/GitHub reach/)).not.toBeInTheDocument()
    const a1 = agentRow('a1')
    fireEvent.click(within(a1).getByRole('button', { name: 'Change repositories for a1' }))
    fireEvent.click(await within(a1).findByRole('checkbox', { name: 'octo/hello' }))
    fireEvent.click(within(a1).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.url.includes('/repositories'))!.url).toContain('account_label=alice')
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toMatch(/\/v1\/users\/me\/integrations\/github-mcp\/agent-accounts$/)
    expect(put.body).toEqual({ agent_name: 'a1', account_label: 'alice', repositories: ['alice/site', 'octo/hello'] })
  })

  it('checks an unchecked agent when it is made read only', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/agent-accounts': { status: 'ok', repositories: [], read_only: true, is_default: true } })
    wrap(<OwnList integration={ownIntegration} account={ownAccount} />)
    fireEvent.click(within(agentRow('a2')).getByRole('checkbox', { name: 'Read only for a2' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual(
      { agent_name: 'a2', account_label: 'alice', read_only: true })
  })

  it('offers a token account Read only alone and shows the server\'s refusal', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/agent-accounts': { ok: false, status: 404, text: async () => JSON.stringify({ detail: "Account 'alice' not found for github-mcp" }) },
    })
    const token: AccountSummary = { ...ownAccount, kind: undefined, agent_reaches: [{ agent: 'a1', repositories: [], read_only: false }] }
    wrap(<OwnList integration={{ ...ownIntegration, accounts: [token] }} account={token} />)
    const a1 = agentRow('a1')
    expect(within(a1).getByText('This account has no list of repositories to pick from.')).toBeInTheDocument()
    expect(within(a1).queryByRole('button', { name: /Change/ })).not.toBeInTheDocument()
    fireEvent.click(within(a1).getByRole('checkbox', { name: 'Read only for a1' }))
    expect(await within(a1).findByText("Account 'alice' not found for github-mcp")).toBeInTheDocument()
  })

  it('tells a person below the team account floor that their chats get no account', () => {
    mockApi([], {})
    wrap(<OwnList integration={{ ...ownIntegration, agents_on_own_accounts: ['a2'], agents_below_floor: ['a2'],
                                agent_accounts: { a2: 'team@mail.example' } }} account={ownAccount} />)
    const line = "Your chats with it get no account for this tool. Your role is below its team account's starting role."
    const a2 = screen.getByText(line).parentElement as HTMLElement
    expect(within(a2).getByText('a2')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /^a2/ })).not.toBeInTheDocument()
  })

  it('offers a Shared-only agent no checkbox and no repositories, and says who sets its account', () => {
    mockApi([], {})
    wrap(<OwnList integration={{ ...ownIntegration, agents_on_own_accounts: ['a2'] }} account={ownAccount} />)
    const line = "Its chats use the agent's own account. A manager sets it in Agent Settings, MCPs."
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
        integration={{ ...ownIntegration, agents_on_own_accounts: ['a2'] }}
        account={{ ...ownAccount, agent_overrides: ['a2'] }}
      />,
    )
    const a2 = agentRow('a2')
    expect(within(a2).getByRole('checkbox', { name: /^a2/ })).toBeChecked()
    expect(within(a2).getByText(/A manager sets it in Agent Settings, MCPs/)).toBeInTheDocument()
    expect(within(a2).queryByTestId('github-reach')).not.toBeInTheDocument()
  })

  it('names the account a Shared-only agent\'s chats use on any tool\'s card', () => {
    mockApi([], {})
    wrap(
      <OwnList
        integration={{
          ...ownIntegration, reach_supported: false, agents_on_own_accounts: ['a2'],
          agent_accounts: { a2: 'team@mail.example' },
        }}
        account={ownAccount}
      />,
    )
    expect(screen.getByText(
      "Its chats use the agent's own account, team@mail.example. A manager sets it in Agent Settings, MCPs.",
    )).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /^a2/ })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /^a1/ })).toBeInTheDocument()
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
    const ops = {
      setIntegration: noop, deleteIntegration: noop, setDefault: noop,
      addAgentAccount: noop, removeAgentAccount: noop, setAgentDefault: noop,
    }
    const { unmount } = wrap(<AccountForm integration={one} account={ownAccount} ops={ops} onDone={() => {}} onCancel={() => {}} />)
    expect(screen.getByText('Use this account for specific agents')).toBeInTheDocument()
    unmount()
    wrap(<AccountForm integration={{ ...one, reach_supported: false }} account={ownAccount} ops={ops}
      onDone={() => {}} onCancel={() => {}} />)
    expect(screen.queryByText('Use this account for specific agents')).not.toBeInTheDocument()
  })
})

describe("an agent's set of accounts", () => {
  // a3 uses both accounts and starts on work; a1 uses alice alone.
  const both: AccountSummary = { ...ownAccount, agent_overrides: ['a1', 'a3'], agent_defaults: ['a1'] }
  const twoAccounts: Integration = {
    ...ownIntegration, reach_supported: false, accounts: [both, workAccount],
  }

  it('ticks an agent under two accounts and marks the one it starts on', () => {
    mockApi([], {})
    const { unmount } = wrap(<OwnList integration={twoAccounts} account={both} />)
    expect(screen.getByText(/Agents that may use this account\. An agent starts on its default account\./))
      .toBeInTheDocument()
    expect(within(agentRow('a1')).getByRole('checkbox', { name: 'a1' })).toBeChecked()
    expect(within(agentRow('a1')).getByText('Default')).toBeInTheDocument()
    expect(within(agentRow('a1')).queryByRole('button', { name: /Make default/ })).not.toBeInTheDocument()
    // a3 is ticked here too, with no greying, and offers this account as its default.
    expect(within(agentRow('a3')).getByRole('checkbox', { name: 'a3' })).toBeChecked()
    expect(within(agentRow('a3')).queryByText('Default')).not.toBeInTheDocument()
    expect(within(agentRow('a3')).getByRole('button', { name: 'Make default for a3' })).toBeInTheDocument()
    expect(screen.queryByText(/^uses /)).not.toBeInTheDocument()
    // An unticked agent has neither.
    expect(within(agentRow('a2')).queryByText('Default')).not.toBeInTheDocument()
    expect(within(agentRow('a2')).queryByRole('button', { name: /Make default/ })).not.toBeInTheDocument()
    unmount()
    // On the other card a3 is ticked as well, and marked as its default there.
    wrap(<OwnList integration={twoAccounts} account={workAccount} />)
    expect(within(agentRow('a3')).getByRole('checkbox', { name: 'a3' })).toBeChecked()
    expect(within(agentRow('a3')).getByText('Default')).toBeInTheDocument()
    expect(within(agentRow('a1')).getByRole('checkbox', { name: 'a1' })).not.toBeChecked()
  })

  it('makes this account the default of an agent through the default route', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/default': { status: 'ok' } })
    wrap(<OwnList integration={twoAccounts} account={both} />)
    fireEvent.click(within(agentRow('a3')).getByRole('button', { name: 'Make default for a3' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toMatch(/\/v1\/users\/me\/integrations\/github-mcp\/agent-accounts\/a3\/default$/)
    expect(put.body).toEqual({ account_label: 'alice' })
  })

  it('adds the account to an agent when ticked, and takes it out by its label when unticked', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/agent-accounts': { status: 'ok', repositories: [], read_only: false, is_default: false } })
    wrap(<OwnList integration={twoAccounts} account={both} />)
    fireEvent.click(within(agentRow('a2')).getByRole('checkbox', { name: 'a2' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toMatch(/\/v1\/users\/me\/integrations\/github-mcp\/agent-accounts$/)
    expect(put.body).toEqual({ agent_name: 'a2', account_label: 'alice' })
    fireEvent.click(within(agentRow('a3')).getByRole('checkbox', { name: 'a3' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    const del = calls.find((c) => c.method === 'DELETE')!
    expect(del.url).toBe('/v1/users/me/integrations/github-mcp/agent-accounts/a3?account_label=alice')
    expect(del.body).toBeUndefined()
  })
})

describe('the account card', () => {
  it('counts the agents that may use the account', () => {
    mockApi([], {})
    const noop = { mutate: () => {}, mutateAsync: async () => ({}), isPending: false } as never
    const ops = { setIntegration: noop, deleteIntegration: noop, setDefault: noop }
    const card = (account: AccountSummary) => (
      <AccountCard integration={ownIntegration} account={account} ops={ops} isEditing={false}
        onEdit={() => {}} onClose={() => {}} />
    )
    const { unmount } = wrap(card({ ...ownAccount, agent_overrides: ['a1', 'a3'] }))
    expect(screen.getByText('2 agents may use it')).toBeInTheDocument()
    unmount()
    wrap(card(ownAccount))
    expect(screen.getByText('1 agent may use it')).toBeInTheDocument()
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
      'An agent that had only this account goes back to your default account. The repositories and Read only you '
      + 'chose for them with it are cleared.')
    const plain = { ...ownAccount, agent_reaches: [{ agent: 'a1', repositories: [], read_only: false }] }
    const text = await confirmText(plain)
    expect(text).toContain('An agent that also has another of your accounts keeps working on that one.')
    expect(text).toContain('An agent that had only this account goes back to your default account.')
    expect(text).not.toContain('are cleared')
  })
})
