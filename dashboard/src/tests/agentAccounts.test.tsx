import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── An agent's own accounts of a tool ───
//
// On Agent Settings, MCPs every agent lists its own accounts of a tool, in
// every mode: whose each is, the default, Make default and Remove (Remove
// asks first and says what it does to the chats and to the account's event
// subscriptions), Add an account from the manager's own, a same-named one
// included; a refusal reaches the manager in the server's words. An account
// whose owner left cannot be the default. The hint says who uses them: on a
// Shared-only agent its chats, which editors and managers may switch, on any
// other agent the runs with no person. On GitHub each account is a row with
// its own reach, and Add offers an owned installation. Where the mode offers
// the agent's own space each account keeps its own event subscriptions, and
// the agent's subscriptions on no account it holds show apart.

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ authConfig: {}, user: { sub: 'u1', role: 'admin', agent_roles: {} } }),
}))
vi.mock('@/components/CommunityMcpsBrowser', () => ({ default: () => null }))
vi.mock('@/components/McpIcon', () => ({ default: () => null }))

import * as authApi from '@/api/auth'
import type {
  AgentServiceAccountOptionsResponse,
  AgentServiceSetAccount,
  ServiceAccountInstallation,
  ServiceSetResponse,
} from '@/api/serviceAccounts'
import type { WebhookSubscription } from '@/api/subscriptions'
import { AgentAccounts } from '@/components/accounts/AgentAccounts'
import AgentMcps from '@/pages/agent/AgentMcps'
import { OAUTH_APP_KIND } from '@/lib/kinds/oauthApp'
import { answerDialog, personClick, renderWithDialogs } from './dialogsHarness'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

type Call = { url: string; method: string; body?: unknown }
type Answer = unknown | ((url: string) => unknown)

const refusal = (status: number, detail: string) =>
  ({ ok: false, status, text: async () => JSON.stringify({ detail }) }) as unknown as Response

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<MemoryRouter><QueryClientProvider client={qc}>{ui}</QueryClientProvider></MemoryRouter>)
}

/** Routes as `'VERB /path-suffix'` (GET when no verb), matched on the path
 * without its query. An MCP with no events answers its catalog with a 404. */
function mockApi(calls: Call[], routes: Record<string, Answer>) {
  fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const path = url.split('?')[0]
    for (const [key, answer] of Object.entries(routes)) {
      const [verb, suffix] = key.includes(' ') ? key.split(' ') : ['GET', key]
      if (verb !== method || !path.endsWith(suffix)) continue
      const body = typeof answer === 'function' ? (answer as (u: string) => unknown)(url) : answer
      if (body && typeof body === 'object' && 'ok' in body) return body as Response
      return { ok: true, json: async () => body } as Response
    }
    if (path.endsWith('/webhook-event-catalog')) return refusal(404, 'This tool has no events.')
    return { ok: true, json: async () => ({}) } as Response
  })
}

afterEach(() => { fetchSpy.mockReset() })

const account = (label: string, extra: Partial<AgentServiceSetAccount> = {}): AgentServiceSetAccount => ({
  label, display_email: `${label}@mail.example`, owner_sub: 'u1', owner_name: 'Ann',
  is_default: false, stands: true, mine: true, installation_id: '', ...extra,
})

const options: AgentServiceAccountOptionsResponse = {
  my_accounts: [
    { label: 'work', display_email: 'work@mail.example', is_default: true },
    { label: 'home', display_email: 'home@mail.example', is_default: false },
  ],
  accounts: [
    account('work', { is_default: true }),
    account('team', { owner_sub: 'u2', owner_name: 'Bob', mine: false }),
    account('gone', { owner_sub: 'u3', owner_name: 'Eve', mine: false, stands: false }),
  ],
}

const done: ServiceSetResponse = { status: 'ok', accounts: [] }

const CATALOG = {
  provider_id: 'mail',
  event_catalog: [{ key: 'message', label: 'New message', default_selected: true }],
  vendor_target_spec: { kind: 'free_text', label: 'Mailbox', placeholder: 'Mailbox name' },
  webhook_base: '',
  registration: { mode: 'auto' },
  per_subscription_secret: false,
  vendor_target_prefill: '',
}

/** One of the agent's own event subscriptions, on the account `label`. */
const sub = (id: string, label: string, target: string): WebhookSubscription => ({
  id, scope: 'service', owner: '', agent: 'pool', mcp_name: 'mail-x', provider_id: 'mail',
  account_label: label, vendor_target: target, vendor_subscription_id: null,
  selected_events: ['message'], selected_subevents: {}, status: 'active', last_error: null,
  last_event_at: null, event_count: 0, expires_at: null, created_by: 'u1', created_by_name: 'Ann',
  created_at: '', updated_at: '', delivery_mode: 'vendor',
})

const addSelect = async () => await screen.findByRole('combobox', { name: 'Add an account' }) as HTMLSelectElement
const optionTexts = (select: HTMLSelectElement) => Array.from(select.options).map((o) => o.textContent)

/** Choose the option a person reads as `text`: its value is its place in the list. */
function choose(select: HTMLSelectElement, text: string) {
  const option = Array.from(select.options).find((o) => o.textContent === text)
  if (!option) throw new Error(`no option "${text}" in ${optionTexts(select).join(', ')}`)
  fireEvent.change(select, { target: { value: option.value } })
}

const SHARED_ONLY_HINT = 'Chats start on the default. Editors and managers can switch a chat to any of these.'
const OTHER_HINT = "Runs with no person use the default. People's own chats use their own accounts."

describe("an agent's own accounts", () => {
  it('lists each account with whose it is and the default, and says who can switch a chat', async () => {
    mockApi([], { '/service-account-options': options })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem />)
    const rows = await screen.findAllByTestId('service-account-row')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('work@mail.example'), expect.stringContaining('team@mail.example'),
      expect.stringContaining('gone@mail.example'),
    ])
    expect(within(rows[0]).getByText('Yours')).toBeInTheDocument()
    expect(within(rows[0]).getByText('Default')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Bob')).toBeInTheDocument()
    expect(within(rows[1]).getByRole('button', { name: 'Make team@mail.example the default' })).toBeInTheDocument()
    expect(within(rows[2]).getByText('Not in use. Its owner no longer manages this agent.')).toBeInTheDocument()
    // An account whose owner left cannot be the default, and can still go.
    expect(within(rows[2]).queryByRole('button', { name: /the default/ })).not.toBeInTheDocument()
    expect(within(rows[2]).getByRole('button', { name: 'Remove gone@mail.example' })).toBeInTheDocument()
    expect(screen.getByText(SHARED_ONLY_HINT)).toBeInTheDocument()
    // The add select offers what the set lacks, each by its place in the list.
    const add = await addSelect()
    expect(optionTexts(add)).toEqual(['Add an account', 'home', 'Connect another account'])
    expect(add.options[1].value).toBe('0')
    // An MCP with no events shows no subscriptions.
    expect(screen.queryByTestId('agent-service-subscriptions')).not.toBeInTheDocument()
  })

  it.each([
    ['a Shared-only agent', true, SHARED_ONLY_HINT, OTHER_HINT],
    ['any other agent', false, OTHER_HINT, SHARED_ONLY_HINT],
  ])('says who uses the accounts on %s, in the same list', async (_what, chatsUseThem, hint, notHint) => {
    mockApi([], { '/service-account-options': options })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem={chatsUseThem} />)
    expect(await screen.findByText(hint)).toBeInTheDocument()
    expect(screen.queryByText(notHint)).not.toBeInTheDocument()
    expect(screen.getAllByTestId('service-account-row')).toHaveLength(3)
    expect(screen.getByRole('button', { name: 'Make team@mail.example the default' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Add an account' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'About Accounts' }), { detail: 0 })
    const tip = screen.getByRole('tooltip').textContent ?? ''
    expect(tip).toContain('Tasks, triggers, calls and delegated work use the default.')
    expect(tip).toContain('Each account has its own event subscriptions. Removing an account removes them.')
    expect(tip.includes('When the default changes, the chats on it are told. They move at their next start.'))
      .toBe(chatsUseThem)
    // Only a Shared-only agent's chats switch among the accounts.
    expect(tip.includes('A switch is for everyone in that chat.')).toBe(chatsUseThem)
  })

  it('adds an account, makes one the default and removes one after asking', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options,
      'PUT /service-accounts': done,
      'PUT /service-accounts/default': done,
      'DELETE /service-accounts': done,
    })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem />)
    choose(await addSelect(), 'home')
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && c.url.endsWith('/service-accounts'))).toBe(true))
    // The account itself: no installation is sent.
    expect(calls.find((c) => c.method === 'PUT' && c.url.endsWith('/service-accounts'))?.body)
      .toEqual({ account_label: 'home' })

    const makeDefault = screen.getByRole('button', { name: 'Make team@mail.example the default' })
    await waitFor(() => expect(makeDefault).toBeEnabled())
    personClick(makeDefault)
    await waitFor(() => expect(calls.find((c) => c.url.endsWith('/service-accounts/default'))?.body)
      .toEqual({ account_label: 'team' }))
    // Each account keeps its own subscriptions: a new default removes none, and says nothing of them.
    const remove = screen.getByRole('button', { name: 'Remove team@mail.example' })
    await waitFor(() => expect(remove).toBeEnabled())
    expect(screen.queryByText(/event subscriptions/i)).not.toBeInTheDocument()

    personClick(remove)
    expect(await screen.findByText(
      'Chats on it stop what they are doing and move to work@mail.example. Each chat is told.',
    )).toBeInTheDocument()
    await answerDialog('Remove')
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    expect(calls.find((c) => c.method === 'DELETE')?.url).toContain('/v1/agents/pool/mcps/mail-x/service-accounts?account_label=team')
  })

  it('shows a refusal in the server\'s words', async () => {
    mockApi([], {
      '/service-account-options': options,
      'PUT /service-accounts': refusal(409, 'This agent already has an account named home for Mail X. Connect yours under another name in Settings, Integrations, then add it.'),
    })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem />)
    choose(await addSelect(), 'home')
    const said = await screen.findByText(/This agent already has an account named home for Mail X/)
    expect(said).toHaveClass('text-status-danger-fg')
  })

  it('offers the manager\'s own account of a name another person\'s account holds, and shows why it is refused', async () => {
    const words = 'This agent already has an account named team for Mail X. Connect yours under another name in Settings, Integrations, then add it.'
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': {
        ...options,
        my_accounts: [...options.my_accounts, { label: 'team', display_email: 'mine@mail.example', is_default: false }],
      },
      'PUT /service-accounts': refusal(409, words),
    })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem />)
    const add = await addSelect()
    expect(optionTexts(add)).toEqual(['Add an account', 'home', 'team', 'Connect another account'])
    choose(add, 'team')
    expect(await screen.findByText(words)).toBeInTheDocument()
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ account_label: 'team' })
  })

  it.each([
    [
      'the default, with another account to move to',
      [account('work', { is_default: true }), account('team', { mine: false })], 'work',
      'Chats on it stop what they are doing and move to team@mail.example. Each chat is told.',
    ],
    [
      'the default, passing over an account whose owner left',
      [account('work', { is_default: true }), account('gone', { stands: false }), account('team')], 'work',
      'Chats on it stop what they are doing and move to team@mail.example. Each chat is told.',
    ],
    [
      'another account, with a default in use',
      [account('work', { is_default: true }), account('team', { mine: false })], 'team',
      'Chats on it stop what they are doing and move to work@mail.example. Each chat is told.',
    ],
    [
      'the last account',
      [account('work', { is_default: true })], 'work',
      'Chats on it stop what they are doing. This agent then has no account for this tool.',
    ],
    [
      'the default, with no other account in use',
      [account('work', { is_default: true }), account('gone', { stands: false })], 'work',
      'Chats on it stop what they are doing. This agent then has no account for this tool.',
    ],
    [
      'another account, with a default whose owner left',
      [account('gone', { is_default: true, stands: false }), account('team')], 'team',
      'Chats on it stop what they are doing. This agent then has no account for this tool.',
    ],
  ])('says what removing %s does', async (_what, accounts, label, body) => {
    mockApi([], { '/service-account-options': { ...options, accounts } })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem />)
    personClick(await screen.findByRole('button', { name: `Remove ${label}@mail.example` }))
    expect(await screen.findByText(body)).toBeInTheDocument()
    await answerDialog('Cancel')
  })
})

describe("an agent's event subscriptions, by account", () => {
  it('lists under each account only its own, and the ones on no account it holds apart', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': options,
      '/v1/subscriptions': {
        subscriptions: [sub('s-work', 'work', 'Work inbox'), sub('s-gone', 'gone', 'Gone inbox'), sub('s-old', 'old', 'Old inbox')],
      },
      '/webhook-event-catalog': CATALOG,
      'POST /v1/subscriptions': sub('s-team', 'team', 'Team inbox'),
    })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem={false} />)
    expect(await screen.findByText('Work inbox')).toBeInTheDocument()
    const [work, team, gone] = screen.getAllByTestId('service-account-row')
    const subsOf = (row: HTMLElement) => within(row).getByTestId('agent-service-subscriptions')

    expect(within(subsOf(work)).getByText('Event subscriptions')).toBeInTheDocument()
    expect(within(subsOf(work)).getByRole('button', { name: 'Subscribe to events' })).toBeInTheDocument()
    expect(within(work).queryByText('Gone inbox')).not.toBeInTheDocument()
    expect(within(work).queryByText('Old inbox')).not.toBeInTheDocument()
    expect(within(subsOf(team)).getByText('None yet.')).toBeInTheDocument()
    expect(within(subsOf(team)).getByRole('button', { name: 'Subscribe to events' })).toBeInTheDocument()
    // An account whose owner left keeps its rows, and takes no new one.
    expect(within(subsOf(gone)).getByText('Gone inbox')).toBeInTheDocument()
    expect(within(gone).queryByRole('button', { name: 'Subscribe to events' })).not.toBeInTheDocument()
    // A trigger from a subscription runs on the default: said under any other
    // account that has some, never under the default nor an account with none.
    const triggers = 'Triggers from these run on the default account.'
    expect(within(subsOf(gone)).getByText(triggers)).toBeInTheDocument()
    expect(within(subsOf(work)).queryByText(triggers)).not.toBeInTheDocument()
    expect(within(subsOf(team)).queryByText(triggers)).not.toBeInTheDocument()

    const leftover = screen.getByTestId('agent-leftover-subscriptions')
    expect(within(leftover).getByText('On an account this agent no longer has')).toBeInTheDocument()
    expect(within(leftover).getAllByTestId('subscription-row')).toHaveLength(1)
    expect(within(leftover).getByText('Old inbox')).toBeInTheDocument()
    expect(screen.getAllByTestId('service-account-row').some((r) => r.contains(leftover))).toBe(false)

    // Each account's events are asked for with its own label, in the agent's space.
    const asked = calls.filter((c) => c.url.includes('/webhook-event-catalog'))
      .map((c) => new URLSearchParams(c.url.split('?')[1]))
    expect(asked.map((q) => q.get('account_label')).sort()).toEqual(['gone', 'team', 'work'])
    for (const q of asked) expect([q.get('scope'), q.get('agent')]).toEqual(['service', 'pool'])

    // Subscribing under an account subscribes with that account.
    fireEvent.click(within(team).getByRole('button', { name: 'Subscribe to events' }))
    expect(await screen.findByText(/with this account\. The agent's shared triggers and apps fire on these events\./))
      .toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('Mailbox name'), { target: { value: 'Team inbox' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({
      scope: 'service', agent: 'pool', account_label: 'team', vendor_target: 'Team inbox',
    })
  })

  it('names the subscriptions in the Remove dialog when the account has some', async () => {
    mockApi([], {
      '/service-account-options': { ...options, accounts: [account('work', { is_default: true }), account('team')] },
      '/v1/subscriptions': { subscriptions: [sub('s-work', 'work', 'Work inbox')] },
      '/webhook-event-catalog': CATALOG,
    })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem />)
    expect(await screen.findByText('Work inbox')).toBeInTheDocument()
    // Every subscription is on an account the agent holds: no group apart.
    expect(screen.queryByTestId('agent-leftover-subscriptions')).not.toBeInTheDocument()

    personClick(screen.getByRole('button', { name: 'Remove work@mail.example' }))
    expect(await screen.findByText(
      'Chats on it stop what they are doing and move to team@mail.example. Each chat is told. Its event subscriptions are removed.',
    )).toBeInTheDocument()
    await answerDialog('Cancel')
    personClick(screen.getByRole('button', { name: 'Remove team@mail.example' }))
    expect(await screen.findByText(
      'Chats on it stop what they are doing and move to work@mail.example. Each chat is told.',
    )).toBeInTheDocument()
    await answerDialog('Cancel')
  })

  it('says it of the last account too', async () => {
    mockApi([], {
      '/service-account-options': { ...options, accounts: [account('work', { is_default: true })] },
      '/v1/subscriptions': { subscriptions: [sub('s-work', 'work', 'Work inbox')] },
      '/webhook-event-catalog': CATALOG,
    })
    wrap(<AgentAccounts agentName="pool" mcpName="mail-x" chatsUseThem={false} />)
    expect(await screen.findByText('Work inbox')).toBeInTheDocument()
    personClick(screen.getByRole('button', { name: 'Remove work@mail.example' }))
    expect(await screen.findByText(
      'Runs with no person then have no account for this tool. Its event subscriptions are removed.',
    )).toBeInTheDocument()
    await answerDialog('Cancel')
  })
})

// ─── GitHub: each account a row with its own reach; Add offers an owned
//     installation as well as the account itself ───

const installation = (id: number, name: string, extra: Partial<ServiceAccountInstallation> = {}): ServiceAccountInstallation => ({
  id, account: name, target_type: 'Organization', repository_selection: 'all', repositories_count: 0,
  suspended: false, admin: true, ...extra,
})

const github = (label: string, extra: Partial<AgentServiceSetAccount> = {}) => account(label, {
  display_email: label, kind: OAUTH_APP_KIND.GITHUB_APP, repositories: [], read_only: false, ...extra,
})

const githubOptions: AgentServiceAccountOptionsResponse = {
  reach_supported: true,
  my_accounts: [
    { label: 'alice', display_email: 'alice', is_default: true, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [installation(9, 'alice')] },
    { label: 'work', display_email: 'work', is_default: false, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [installation(10, 'acme')] },
    {
      label: 'spare', display_email: 'spare', is_default: false, kind: OAUTH_APP_KIND.GITHUB_APP, installations: [
        installation(12, 'spare-org'), installation(13, 'octo', { admin: false }), installation(14, 'paused-org', { suspended: true }),
      ],
    },
    { label: 'relay', display_email: 'relay', is_default: false, kind: OAUTH_APP_KIND.GITHUB_APP, hosted: true, installations: [installation(15, 'relay-org')] },
  ],
  accounts: [
    github('alice', { is_default: true }),
    github('work', { installation_id: '10', installation_account: 'acme', repositories: ['acme/api'], read_only: true }),
  ],
}

const listing = {
  pickable: true, from_meta: false,
  installations: [{ id: 9, account: 'alice', repositories: ['alice/site', 'alice/tools'], truncated: false }],
}

describe("an agent's own GitHub accounts", () => {
  it('shows each account as a row with its own reach, and adds an owned installation', async () => {
    const calls: Call[] = []
    mockApi(calls, { '/service-account-options': githubOptions, 'PUT /service-accounts': done })
    wrap(<AgentAccounts agentName="pool" mcpName="github-mcp" chatsUseThem={false} />)
    const [alice, work] = await screen.findAllByTestId('service-account-row')
    expect(screen.getAllByTestId('service-account-row')).toHaveLength(2)
    expect(within(alice).queryByText(/^Installation:/)).not.toBeInTheDocument()
    expect(within(work).getByText('Installation: acme')).toBeInTheDocument()

    const reachOf = (row: HTMLElement) => within(row).getByTestId('github-reach')
    expect(within(reachOf(alice)).getByText('GitHub reach')).toBeInTheDocument()
    expect(within(reachOf(alice)).getByText('Repositories: All repositories the account reaches.')).toBeInTheDocument()
    expect(within(reachOf(alice)).getByRole('button', { name: 'Change repositories for alice' })).toBeInTheDocument()
    expect(within(reachOf(alice)).getByRole('checkbox', { name: 'Read only for alice' })).not.toBeChecked()
    expect(within(reachOf(work)).getByText('Repositories: Only acme/api.')).toBeInTheDocument()
    expect(within(reachOf(work)).getByRole('button', { name: 'Change repositories for work' })).toBeInTheDocument()
    expect(within(reachOf(work)).getByRole('checkbox', { name: 'Read only for work' })).toBeChecked()

    // The accounts the set lacks, each owned and unsuspended installation of
    // one not signed in through the relay, never one held already.
    const add = await addSelect()
    expect(optionTexts(add)).toEqual([
      'Add an account', 'spare', 'Installation: spare-org via spare', 'relay', 'Connect another account',
    ])
    choose(add, 'Installation: spare-org via spare')
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.url).toBe('/v1/agents/pool/mcps/github-mcp/service-accounts')
    expect(put.body).toEqual({ account_label: 'spare', installation_id: '12' })
  })

  it("saves each row's reach with its own installation", async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/service-account-options': githubOptions,
      '/service-account-repositories': listing,
      'PUT /service-accounts/reach': done,
    })
    wrap(<AgentAccounts agentName="pool" mcpName="github-mcp" chatsUseThem={false} />)
    const [alice, work] = await screen.findAllByTestId('service-account-row')
    fireEvent.click(within(work).getByRole('checkbox', { name: 'Read only for work' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const readOnly = calls.find((c) => c.method === 'PUT')!
    expect(readOnly.url).toBe('/v1/agents/pool/mcps/github-mcp/service-accounts/reach')
    expect(readOnly.body).toEqual({ account_label: 'work', installation_id: '10', read_only: false })

    fireEvent.click(within(alice).getByRole('button', { name: 'Change repositories for alice' }))
    fireEvent.click(await within(alice).findByRole('checkbox', { name: 'alice/site' }))
    fireEvent.click(within(alice).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(2))
    const asked = calls.find((c) => c.url.includes('/service-account-repositories'))!
    expect(asked.url).toContain('account_label=alice&installation_id=')
    expect(calls.filter((c) => c.method === 'PUT')[1].body)
      .toEqual({ account_label: 'alice', installation_id: '', repositories: ['alice/site'] })
  })

  it('reads the list again after a refusal, and the card says why', async () => {
    const calls: Call[] = []
    const gone = "This account is no longer one of this agent's."
    mockApi(calls, {
      '/service-account-options': githubOptions,
      'PUT /service-accounts/reach': { ok: false, status: 404, text: async () => JSON.stringify({ detail: gone }) },
    })
    wrap(<AgentAccounts agentName="pool" mcpName="github-mcp" chatsUseThem={false} />)
    const [, work] = await screen.findAllByTestId('service-account-row')
    const reads = () => calls.filter((c) => c.method === 'GET' && c.url.endsWith('/service-account-options')).length
    const before = reads()
    fireEvent.click(within(work).getByRole('checkbox', { name: 'Read only for work' }))
    await waitFor(() => expect(reads()).toBe(before + 1))
    expect((await screen.findAllByText(gone)).length).toBeGreaterThan(0)
    const card = screen.getByTestId('agent-accounts')
    expect(card.querySelector(':scope > .text-status-danger-fg')?.textContent).toBe(gone)
  })

  it('clears an old refusal once a later reach save works', async () => {
    const calls: Call[] = []
    const gone = "This account is no longer one of this agent's."
    let refuse = true
    fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      if (method === 'PUT' && url.endsWith('/service-accounts/reach')) {
        if (refuse) return { ok: false, status: 404, text: async () => JSON.stringify({ detail: gone }) } as Response
        return { ok: true, json: async () => done } as Response
      }
      if (url.split('?')[0].endsWith('/service-account-options')) return { ok: true, json: async () => githubOptions } as Response
      return { ok: true, json: async () => ({}) } as Response
    })
    wrap(<AgentAccounts agentName="pool" mcpName="github-mcp" chatsUseThem={false} />)
    const card = await screen.findByTestId('agent-accounts')
    const work = () => screen.getAllByTestId('service-account-row')[1]
    fireEvent.click(within(work()).getByRole('checkbox', { name: 'Read only for work' }))
    await waitFor(() => expect(card.querySelector(':scope > .text-status-danger-fg')?.textContent).toBe(gone))
    refuse = false
    fireEvent.click(within(work()).getByRole('checkbox', { name: 'Read only for work' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(2))
    await waitFor(() => expect(card.querySelector(':scope > .text-status-danger-fg')).toBeNull())
  })
})

// ─── The page: AgentMcps shows the accounts in every mode, with the hint and
//     the subscriptions the mode calls for ───

describe("the agent's MCPs page", () => {
  const MCPS = {
    mcps: [{
      name: 'mail-x', label: 'Mail X', description: '', category: 'community', assignment_mode: 'auto',
      credential_type: 'per_user', has_service_account: true, enabled: true, authorized_by: 'auto',
    }],
  }

  it.each([
    ['Shared-only', { collaborative: false, default_scope: 'agent' }, SHARED_ONLY_HINT],
    ['Personal and Shared', { collaborative: true, default_scope: 'user' }, OTHER_HINT],
  ])('shows the accounts of a %s agent', async (_mode, info, hint) => {
    mockApi([], {
      '/v1/agents/pool/mcps': MCPS,
      '/v1/agents/pool/info': { name: 'pool', ...info },
      '/service-account-options': options,
      '/v1/subscriptions': { subscriptions: [sub('s-work', 'work', 'Work inbox')] },
      '/webhook-event-catalog': CATALOG,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/agents/pool/mcps']}>
          <Routes><Route path="/agents/:name/mcps" element={<AgentMcps />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    // The mode is read from the agent's info: wait for it before reading the hint.
    await waitFor(() => expect(qc.getQueryState(['agent-info', 'pool'])?.status).toBe('success'))
    expect(await screen.findByText(hint)).toBeInTheDocument()
    expect(screen.getAllByTestId('service-account-row')).toHaveLength(3)
    expect(await screen.findByText('Work inbox')).toBeInTheDocument()
  })

  it('shows no accounts section on a Personal-only agent, and never asks for one', async () => {
    const calls: Call[] = []
    mockApi(calls, {
      '/v1/agents/pool/mcps': MCPS,
      '/v1/agents/pool/info': { name: 'pool', collaborative: false, default_scope: 'user' },
      '/service-account-options': options,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/agents/pool/mcps']}>
          <Routes><Route path="/agents/:name/mcps" element={<AgentMcps />} /></Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(qc.getQueryState(['agent-info', 'pool'])?.status).toBe('success'))
    expect(await screen.findByText('Mail X')).toBeInTheDocument()
    expect(screen.queryByTestId('agent-accounts')).not.toBeInTheDocument()
    expect(calls.some((c) => c.url.includes('/service-account-options'))).toBe(false)
  })
})
