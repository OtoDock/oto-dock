import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// ─── Subscription scope on create: the modal's "Subscribe as", the panel's
//     scope pills, the agent tab's section, the trigger modal's picker and
//     the delete that walks the linked-triggers refusal ───

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'me', role: 'creator', agent_roles: { dev: 'manager' } } }),
}))

import * as authApi from '@/api/auth'
import { SubscribeToEventsModal } from '@/components/accounts/SubscribeToEventsModal'
import { SubscriptionsPanel } from '@/components/accounts/SubscriptionsPanel'
import { ServiceAccountBindingDropdown } from '@/components/ServiceAccountBindingDropdown'
import { CreateTriggerModal, EditTriggerModal } from '@/pages/agent/AgentTriggers.modals'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const CATALOG = {
  provider_id: 'github',
  event_catalog: [
    { key: 'push', label: 'Push', default_selected: true },
    { key: 'pull_request', label: 'Pull request' },
  ],
  vendor_target_spec: { kind: 'free_text', label: 'Repository', placeholder: 'owner/repo' },
  webhook_base: '',
  registration: { mode: 'auto' },
  per_subscription_secret: false,
  vendor_target_prefill: '',
}

const PAIRED_CATALOG = {
  ...CATALOG,
  provider_id: 'microsoft',
  event_catalog: [
    { key: 'mail', label: 'Mail', default_selected: true, resource_contains: 'messages' },
    { key: 'calendar', label: 'Calendar', default_selected: true, resource_contains: 'events' },
  ],
  vendor_target_spec: {
    kind: 'static_list', label: 'Resource',
    static_options: [
      { value: 'me/mailFolders/inbox/messages', label: 'Inbox' },
      { value: 'me/events', label: 'Calendar' },
    ],
  },
}

const row = (over: Record<string, unknown>) => ({
  id: 'sub-1', scope: 'user', owner: 'me', agent: null, mcp_name: 'github-mcp',
  provider_id: 'github', account_label: 'acct', vendor_target: 'o/r',
  vendor_subscription_id: null, selected_events: ['push'], selected_subevents: {},
  status: 'active', last_error: null, last_event_at: null, event_count: 0,
  expires_at: null, created_by: 'me', created_by_name: 'Me', created_at: '', updated_at: '',
  delivery_mode: 'vendor', ...over,
})

const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }) as Response
const fail = (status: number, detail: unknown) => ({
  ok: false, status, statusText: 'x',
  json: async () => ({ detail }), text: async () => JSON.stringify({ detail }),
}) as Response

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

const BINDINGS = [
  { agent_name: 'dev', display_name: 'Developer', can_manage: true, agent_scope_available: true },
  { agent_name: 'ops', display_name: 'Ops', can_manage: false, agent_scope_available: true },
  { agent_name: 'solo', display_name: 'Solo', can_manage: true, agent_scope_available: false },
]

const modalProps = {
  mcpName: 'github-mcp', accountLabel: 'acct', providerId: 'github',
  eventCatalog: CATALOG.event_catalog, vendorTargetSpec: CATALOG.vendor_target_spec as any,
  registrationMode: 'auto' as const, onClose: () => {},
}

describe('SubscribeToEventsModal — subscribe as', () => {
  beforeEach(() => { fetchSpy.mockReset() })
  afterEach(() => { fetchSpy.mockReset() })

  it('renders no selector without service options and posts a personal subscription', async () => {
    const posts: any[] = []
    fetchSpy.mockImplementation(async (_p: string, o?: RequestInit) => {
      posts.push(JSON.parse(String(o?.body)))
      return ok(row({}))
    })
    wrap(<SubscribeToEventsModal {...modalProps} />)
    expect(screen.queryByLabelText('Subscribe as')).toBeNull()
    fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'https://github.com/o/r.git' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toEqual({
      scope: 'user', agent: undefined, mcp_name: 'github-mcp', account_label: 'acct',
      vendor_target: 'o/r', selected_events: ['push'],
    })
  })

  it('offers the manageable agents, disables the rest, hides personal-only ones', () => {
    wrap(<SubscribeToEventsModal {...modalProps} serviceOptions={BINDINGS} />)
    const select = screen.getByLabelText('Subscribe as') as HTMLSelectElement
    const options = within(select).getAllByRole('option') as HTMLOptionElement[]
    expect(options.map((o) => o.textContent)).toEqual([
      'Me (personal)', 'Agent Developer', 'Agent Ops (managers only)',
    ])
    expect(options[2].disabled).toBe(true)
    expect(select.value).toBe('__me__')
  })

  it('sends scope=service and the agent when an agent is chosen', async () => {
    const posts: any[] = []
    fetchSpy.mockImplementation(async (_p: string, o?: RequestInit) => {
      posts.push(JSON.parse(String(o?.body)))
      return ok(row({ scope: 'service', agent: 'dev' }))
    })
    wrap(<SubscribeToEventsModal {...modalProps} serviceOptions={BINDINGS} />)
    fireEvent.change(screen.getByLabelText('Subscribe as'), { target: { value: 'dev' } })
    fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'o/r' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({ scope: 'service', agent: 'dev', account_label: 'acct' })
  })

  it('applies the chosen scope to every paired request and skips rows that exist', async () => {
    const posts: any[] = []
    const onClose = vi.fn()
    fetchSpy.mockImplementation(async (_p: string, o?: RequestInit) => {
      const body = JSON.parse(String(o?.body))
      posts.push(body)
      if (body.vendor_target === 'me/mailFolders/inbox/messages') {
        return fail(409, { error: 'exists', message: 'exists' })
      }
      return ok(row({ scope: 'service', agent: 'dev' }))
    })
    wrap(
      <SubscribeToEventsModal
        {...modalProps}
        providerId="microsoft"
        eventCatalog={PAIRED_CATALOG.event_catalog}
        vendorTargetSpec={PAIRED_CATALOG.vendor_target_spec as any}
        serviceOptions={BINDINGS}
        onClose={onClose}
      />,
    )
    fireEvent.change(screen.getByLabelText('Subscribe as'), { target: { value: 'dev' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(posts).toHaveLength(2)
    for (const p of posts) expect(p).toMatchObject({ scope: 'service', agent: 'dev' })
    expect(posts.map((p) => p.vendor_target)).toEqual(['me/mailFolders/inbox/messages', 'me/events'])
  })

  it('a fixed agent shows no selector and names the agent', () => {
    wrap(<SubscribeToEventsModal {...modalProps} scope="service" agent="dev" serviceOptions={BINDINGS} />)
    expect(screen.queryByLabelText('Subscribe as')).toBeNull()
    expect(screen.getByText('dev')).toBeInTheDocument()
  })

  it('a missing-scopes refusal in service scope says the bound owner must reconnect', async () => {
    fetchSpy.mockImplementation(async () =>
      fail(400, { error: 'missing_scopes', message: 'm', required: ['repo'], action: 'reconnect' }))
    wrap(<SubscribeToEventsModal {...modalProps} scope="service" agent="dev" />)
    fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'o/r' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    expect(await screen.findByText(/The bound account acct is missing the scopes repo/)).toBeInTheDocument()
  })
})

describe('SubscriptionsPanel — both scopes with a pill', () => {
  beforeEach(() => { fetchSpy.mockReset() })

  it('lists my personal rows and the rows of agents this account serves', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.includes('/webhook-event-catalog')) return ok(CATALOG)
      if (path.startsWith('/v1/subscriptions')) {
        return ok({ subscriptions: [
          row({ id: 's-me', vendor_target: 'mine/repo' }),
          row({ id: 's-other', owner: 'someone-else', vendor_target: 'theirs/repo' }),
          row({ id: 's-dev', scope: 'service', owner: '', agent: 'dev', vendor_target: 'dev/repo', created_by: 'co', created_by_name: 'Co Manager' }),
          row({ id: 's-unbound', scope: 'service', owner: '', agent: 'ops', vendor_target: 'ops/repo' }),
          row({ id: 's-label', account_label: 'other', vendor_target: 'label/repo' }),
        ] })
      }
      return ok({})
    })
    wrap(<SubscriptionsPanel mcpName="github-mcp" accountLabel="acct" serviceBindings={[BINDINGS[0]]} />)
    await screen.findByText('mine/repo')
    const rows = screen.getAllByTestId('subscription-row')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('Personal')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Agent Developer')).toBeInTheDocument()
    expect(within(rows[1]).getByText(/by Co Manager/)).toBeInTheDocument()
    expect(screen.queryByText('theirs/repo')).toBeNull()
    expect(screen.queryByText('ops/repo')).toBeNull()
    expect(screen.queryByText('label/repo')).toBeNull()
  })

  it('a delete refused for linked triggers asks again and then forces', async () => {
    const deletes: string[] = []
    fetchSpy.mockImplementation(async (path: string, o?: RequestInit) => {
      if (path.includes('/webhook-event-catalog')) return ok(CATALOG)
      if (o?.method === 'DELETE') {
        deletes.push(path)
        if (!path.includes('force=true')) {
          return fail(409, { error: 'linked_triggers', message: '1 trigger', triggers: [{ id: 't1', name: 'On push', scope: 'user' }] })
        }
        return ok({ deleted: true })
      }
      if (path.startsWith('/v1/subscriptions')) return ok({ subscriptions: [row({})] })
      return ok({})
    })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    wrap(<SubscriptionsPanel mcpName="github-mcp" accountLabel="acct" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(deletes).toHaveLength(2))
    expect(deletes[1]).toBe('/v1/subscriptions/sub-1?force=true')
    expect(confirmSpy).toHaveBeenCalledTimes(2)
    expect(String(confirmSpy.mock.calls[1][0])).toContain('On push')
    confirmSpy.mockRestore()
  })
})

describe('Agent MCPs tab — subscribe for this agent', () => {
  beforeEach(() => { fetchSpy.mockReset() })

  const routes = (binding: unknown) => async (path: string) => {
    if (path.includes('/service-account-options')) {
      return ok({ my_accounts: [{ label: 'acct', display_email: 'me@x', is_default: true }], current_binding: binding })
    }
    if (path.includes('/webhook-event-catalog')) {
      expect(path).toContain('scope=service')
      expect(path).toContain('agent=dev')
      return ok(CATALOG)
    }
    if (path.startsWith('/v1/subscriptions')) {
      expect(path).toContain('scope=service')
      return ok({ subscriptions: [row({ id: 's-dev', scope: 'service', owner: '', agent: 'dev', vendor_target: 'dev/repo' })] })
    }
    return ok({})
  }
  const bound = { label: 'acct', owner_sub: 'me', owner_name: 'Me', owner_email: '', set_by: '', set_at: '' }

  it('renders the button and the agent rows when a binding exists', async () => {
    fetchSpy.mockImplementation(routes(bound))
    wrap(<ServiceAccountBindingDropdown agentName="dev" mcpName="github-mcp" callerSub="me" agentScopeAvailable />)
    expect(await screen.findByRole('button', { name: '+ Subscribe to events for this agent' })).toBeInTheDocument()
    expect(await screen.findByText('dev/repo')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '+ Subscribe to events for this agent' }))
    expect(await screen.findByText('Subscribe to github events')).toBeInTheDocument()
    expect(screen.queryByLabelText('Subscribe as')).toBeNull()
  })

  it('shows nothing without a binding or without agent scope', async () => {
    fetchSpy.mockImplementation(routes(null))
    wrap(<ServiceAccountBindingDropdown agentName="dev" mcpName="github-mcp" callerSub="me" agentScopeAvailable />)
    await screen.findByText('(no service account)')
    expect(screen.queryByTestId('agent-service-subscriptions')).toBeNull()

    fetchSpy.mockImplementation(routes(bound))
    wrap(<ServiceAccountBindingDropdown agentName="dev" mcpName="github-mcp" callerSub="me" agentScopeAvailable={false} />)
    await waitFor(() => expect(screen.getAllByText('(no service account)').length).toBe(2))
    expect(screen.queryByTestId('agent-service-subscriptions')).toBeNull()
  })
})

describe('Create trigger — subscription picker follows the scope', () => {
  beforeEach(() => { fetchSpy.mockReset() })

  it('offers the agent rows for an agent trigger and personal rows for a user trigger', async () => {
    const asked: string[] = []
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/subscriptions')) {
        asked.push(path)
        if (path.includes('scope=service')) {
          return ok({ subscriptions: [row({ id: 's-dev', scope: 'service', owner: '', agent: 'dev', vendor_target: 'dev/repo' })] })
        }
        return ok({ subscriptions: [row({ id: 's-me', vendor_target: 'mine/repo' }), row({ id: 's-x', owner: 'x', vendor_target: 'x/repo' })] })
      }
      if (path.includes('/info')) return ok({ collaborative: true, default_scope: 'user' })
      if (path.startsWith('/v1/tasks')) return ok({ tasks: [] })
      if (path.startsWith('/v1/apps')) return ok({ apps: [] })
      return ok({})
    })
    wrap(<CreateTriggerModal agent="dev" onClose={() => {}} canCreateAgentScope />)
    const source = await screen.findByDisplayValue('Generic webhook URL')
    fireEvent.change(source, { target: { value: 'vendor' } })
    const picker = await screen.findByTestId('trigger-subscription')
    await waitFor(() => expect(within(picker).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['— Choose —', 'github/mine/repo · push', 'Paste a subscription id…']))

    fireEvent.change(screen.getByDisplayValue('User (personal automation)'), { target: { value: 'agent' } })
    await waitFor(() => expect(within(picker).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['— Choose —', 'github/dev/repo · push', 'Paste a subscription id…']))
    expect(asked.some((p) => p.includes('scope=service') && p.includes('agent=dev'))).toBe(true)
  })

  it('a scope change drops the subscription picked for the other scope', async () => {
    const posts: Array<Record<string, unknown>> = []
    fetchSpy.mockImplementation(async (path: string, o?: RequestInit) => {
      if (o?.method === 'POST') { posts.push(JSON.parse(String(o.body))); return ok({ trigger: {} }) }
      if (path.startsWith('/v1/subscriptions')) {
        if (path.includes('scope=service')) {
          return ok({ subscriptions: [row({ id: 's-dev', scope: 'service', owner: '', agent: 'dev', vendor_target: 'dev/repo' })] })
        }
        return ok({ subscriptions: [row({ id: 's-me', vendor_target: 'mine/repo' })] })
      }
      if (path.includes('/info')) return ok({ collaborative: true, default_scope: 'user' })
      if (path.startsWith('/v1/tasks')) return ok({ tasks: [] })
      if (path.startsWith('/v1/apps')) return ok({ apps: [] })
      return ok({})
    })
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    wrap(<CreateTriggerModal agent="dev" onClose={() => {}} canCreateAgentScope />)
    fireEvent.change(await screen.findByDisplayValue('Generic webhook URL'), { target: { value: 'vendor' } })
    const picker = await screen.findByTestId('trigger-subscription')
    await waitFor(() => expect(within(picker).getAllByRole('option')).toHaveLength(3))
    fireEvent.change(picker, { target: { value: 's-me' } })

    fireEvent.change(screen.getByDisplayValue('User (personal automation)'), { target: { value: 'agent' } })
    fireEvent.change(screen.getByPlaceholderText('e.g. GitHub PR opened'), { target: { value: 'On push' } })
    fireEvent.change(screen.getByPlaceholderText('e.g. PR merged: {{title}}'), { target: { value: 't' } })
    fireEvent.change(screen.getByPlaceholderText('e.g. {{author}} merged #{{number}}'), { target: { value: 'b' } })
    fireEvent.submit(screen.getByPlaceholderText('e.g. GitHub PR opened').closest('form')!)

    await waitFor(() => expect(alertSpy).toHaveBeenCalled())
    expect(String(alertSpy.mock.calls[0][0])).toContain("this agent's subscriptions")
    expect(posts).toEqual([])
    alertSpy.mockRestore()
  })

  it('says where to create one when the scope has none', async () => {
    fetchSpy.mockImplementation(async (path: string) => {
      if (path.startsWith('/v1/subscriptions')) return ok({ subscriptions: [] })
      if (path.includes('/info')) return ok({ collaborative: true, default_scope: 'agent' })
      if (path.startsWith('/v1/tasks')) return ok({ tasks: [] })
      if (path.startsWith('/v1/apps')) return ok({ apps: [] })
      return ok({})
    })
    wrap(<CreateTriggerModal agent="dev" onClose={() => {}} canCreateAgentScope />)
    const source = await screen.findByDisplayValue('Generic webhook URL')
    fireEvent.change(source, { target: { value: 'vendor' } })
    expect(await screen.findByText(/No agent subscriptions yet: Agent Settings → MCPs/)).toBeInTheDocument()
  })
})

// An app's trigger is usually created before the agent's subscription
// exists; the edit modal binds it later, offering the scope's rows, and
// sends subscription_id only when it changed.
describe('Edit trigger — the subscription can be bound later', () => {
  beforeEach(() => { fetchSpy.mockReset() })

  const trigger = {
    id: 't-1', slug: 'github-events', name: 'GitHub events', scope: 'agent', agent: 'dev',
    created_by: 'me', task_id: null, notify_enabled: false, notify_severity: 'info',
    notify_title: null, notify_body: null, notify_target_scope: null, notify_target: null,
    debounce_seconds: 0, enabled: true, fired_count: 0, last_fired_at: null, last_error: null,
    subscription_id: null, event_filter: {}, app_id: 'a-1', handler: 'github',
    can_edit: true, created_at: '', updated_at: '', webhook_url: '',
  } as any

  it('offers the agent rows and posts the chosen id', async () => {
    const posted: any[] = []
    fetchSpy.mockImplementation(async (path: string, init?: any) => {
      if (path.startsWith('/v1/subscriptions')) {
        return ok({ subscriptions: [row({ id: 's-dev', scope: 'service', owner: '', agent: 'dev', vendor_target: 'dev/repo' })] })
      }
      if (path === '/v1/triggers/t-1/edit') { posted.push(JSON.parse(init.body)); return ok({ status: 'updated' }) }
      return ok({})
    })
    wrap(<EditTriggerModal trigger={trigger} onClose={() => {}} />)
    const picker = await screen.findByTestId('edit-trigger-subscription')
    await waitFor(() => expect(within(picker).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['— None —', 'github/dev/repo · push']))
    fireEvent.change(picker, { target: { value: 's-dev' } })
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect(posted[0].subscription_id).toBe('s-dev')
    expect(posted[0].name).toBe('GitHub events')
  })

  it('leaves the field out when it did not change', async () => {
    const posted: any[] = []
    fetchSpy.mockImplementation(async (path: string, init?: any) => {
      if (path.startsWith('/v1/subscriptions')) return ok({ subscriptions: [] })
      if (path === '/v1/triggers/t-1/edit') { posted.push(JSON.parse(init.body)); return ok({ status: 'updated' }) }
      return ok({})
    })
    wrap(<EditTriggerModal trigger={{ ...trigger, subscription_id: 's-old' }} onClose={() => {}} />)
    const picker = await screen.findByTestId('edit-trigger-subscription')
    expect(within(picker).getAllByRole('option').map((o) => o.textContent)).toEqual(['— None —', 's-old (current)'])
    fireEvent.click(screen.getByRole('button', { name: /Save/i }))
    await waitFor(() => expect(posted).toHaveLength(1))
    expect('subscription_id' in posted[0]).toBe(false)
  })
})

// ─── Target kinds: the manifest offers one repository or a whole organization ───

const KINDS_SPEC = {
  kind: 'free_text', label: 'Repository (owner/name)', placeholder: 'owner/repo',
  validation_regex: '^[^/]+/[^/]+$',
  target_kinds: [
    { key: 'repository', label: 'One repository', placeholder: 'owner/repo',
      validation_regex: '^[^/]+/[^/]+$' },
    { key: 'organization', label: 'Every repository in an organization', placeholder: 'octocat-org',
      validation_regex: '^[^/]+$', required_scopes: ['admin:org_hook'],
      help_text: 'Needs the Organization webhooks permission.' },
  ],
}

describe('SubscribeToEventsModal — target kinds', () => {
  beforeEach(() => { fetchSpy.mockReset() })
  afterEach(() => { fetchSpy.mockReset() })

  it('shows no chooser and sends no kind when the manifest declares none', async () => {
    const posts: any[] = []
    fetchSpy.mockImplementation(async (_p: string, o?: RequestInit) => {
      posts.push(JSON.parse(String(o?.body)))
      return ok(row({}))
    })
    wrap(<SubscribeToEventsModal {...modalProps} />)
    expect(screen.queryByRole('radiogroup', { name: 'Subscribe to' })).toBeNull()
    fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'o/r' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect('vendor_target_kind' in posts[0]).toBe(false)
  })

  it('offers the kinds, swaps the hints and the check, and sends the chosen key', async () => {
    const posts: any[] = []
    fetchSpy.mockImplementation(async (_p: string, o?: RequestInit) => {
      posts.push(JSON.parse(String(o?.body)))
      return ok(row({ target_kind: 'organization' }))
    })
    wrap(<SubscribeToEventsModal {...modalProps} vendorTargetSpec={KINDS_SPEC as any} />)
    const group = screen.getByRole('radiogroup', { name: 'Subscribe to' })
    const radios = within(group).getAllByRole('radio')
    expect(radios.map((r) => r.textContent)).toEqual(['One repository', 'Every repository in an organization'])
    expect(radios[0].getAttribute('aria-checked')).toBe('true')

    fireEvent.click(radios[1])
    expect(screen.getByText('Needs the Organization webhooks permission.')).toBeTruthy()
    // A repository string is refused for the organization kind before any request.
    fireEvent.change(screen.getByPlaceholderText('octocat-org'), { target: { value: 'o/r' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(screen.getByText(/doesn't look right/)).toBeTruthy())
    expect(posts).toHaveLength(0)

    fireEvent.change(screen.getByPlaceholderText('octocat-org'), { target: { value: 'OtoDock' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({ vendor_target: 'OtoDock', vendor_target_kind: 'organization' })
  })

  it('sends the default kind as the empty key', async () => {
    const posts: any[] = []
    fetchSpy.mockImplementation(async (_p: string, o?: RequestInit) => {
      posts.push(JSON.parse(String(o?.body)))
      return ok(row({}))
    })
    wrap(<SubscribeToEventsModal {...modalProps} vendorTargetSpec={KINDS_SPEC as any} />)
    fireEvent.change(screen.getByPlaceholderText('owner/repo'), { target: { value: 'o/r' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create subscription' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({ vendor_target: 'o/r', vendor_target_kind: '' })
  })
})
