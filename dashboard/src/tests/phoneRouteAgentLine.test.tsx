/**
 * A route that runs as the agent itself (RouteModal): the third identity,
 * its hint only while it is chosen, the tools the line may tick (the
 * preview's `line_tools_offered`, sent as `line_tools`), and the no-PIN
 * confirm in this mode's own words with the acknowledgement it carries.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'
import { answerDialog, findAlertDialog, renderWithDialogs } from './dialogsHarness'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const AGENT_NO_PIN = 'Anyone who calls this route acts as the agent itself. They can change its shared files and use its tools, its accounts and the tools you ticked.'
const HINT = 'Callers act as the agent. They can change its shared files and use its tools and accounts.'

function makeRoute(over: Record<string, unknown> = {}) {
  return {
    id: 'r1', direction: 'inbound', name: 'Main Line', agent: 'assistant',
    language: 'en', llm_mode: 'proxy', phone_server_id: 1,
    stt_provider_id: null, tts_provider_id: null, greeting: '',
    phone_context_override: '', backchannel_mode: 'on',
    thinking_filler_mode: 'on', background_sound: 'off', enabled: true,
    audiosocket_uuid: null, did: '+16083191947', ami_caller_id: '',
    ami_outbound_context: '', dial_prefix: '', trigger_slug: null,
    identity_mode: 'caller', identity_user_sub: null, role: 'viewer', remember_callers: true,
    line_tools: [], pin_configured: false, warnings: [], created_at: '', updated_at: '', ...over,
  }
}

const PREVIEW = {
  attached: [{ name: 'memory-mcp', label: 'Memory' }],
  excluded: [],
  line_tools_offered: [
    { name: 'schedules-mcp', label: 'Schedules' },
    { name: 'notifications-mcp', label: 'Notifications' },
  ],
}

function mockApi(routes: unknown[], preview: unknown = PREVIEW) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone/routes/mcp-preview')) return ok(preview)
    if (path.startsWith('/v1/admin/phone/routes')) {
      if (init?.method === 'PUT' || init?.method === 'POST') return ok({ id: 'r1' })
      if (init?.method === 'DELETE') return ok({})
      return ok({ routes })
    }
    if (path.startsWith('/v1/admin/phone-servers')) {
      return ok({ servers: [{ id: 1, name: 'twil', adapter_type: 'twilio', is_default: true }] })
    }
    if (path.startsWith('/v1/agents/assistant/users')) return ok({ users: [] })
    if (path.startsWith('/v1/agents')) return ok({ agents: [{ name: 'assistant' }] })
    if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
    return ok({})
  })
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
}

async function openEdit() {
  fireEvent.click(await screen.findByText('Edit'))
}

function chooseAgent() {
  fireEvent.click(screen.getByRole('radio', { name: /The agent itself/ }))
}

function routePut() {
  return fetchSpy.mock.calls.find(([p, i]) => p === '/v1/admin/phone/routes/r1' && i?.method === 'PUT')
}

function previewCalls() {
  return fetchSpy.mock.calls.map(([p]) => String(p)).filter((p) => p.startsWith('/v1/admin/phone/routes/mcp-preview?'))
}

function pinCard() {
  return screen.getByText('Require a PIN').closest('div')!.parentElement!
}

describe('a route run as the agent itself', () => {
  afterEach(() => { fetchSpy.mockReset() })

  it('shows its hint and the tools only while it is chosen, and no remember control', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    expect(screen.queryByText(HINT)).toBeNull()
    expect(screen.queryByText('Tools this line may use')).toBeNull()
    expect(screen.getByText('Remember callers')).toBeInTheDocument()
    chooseAgent()
    const radio = screen.getByRole('radio', { name: /The agent itself/ })
    expect(screen.getByText(HINT)).toBeInTheDocument()
    expect(radio).toHaveAttribute('aria-describedby', 'identity-agent-hint')
    expect(screen.queryByText('Remember callers')).toBeNull()
    expect(screen.queryByLabelText('Platform user')).toBeNull()
    const list = await screen.findByTestId('route-line-tools')
    expect(within(list).getByText('Tools this line may use')).toBeInTheDocument()
    expect(within(list).getByText('Anyone who calls this line can use what you tick.')).toBeInTheDocument()
    expect((within(list).getByLabelText('Schedules') as HTMLInputElement).checked).toBe(false)
    expect((within(list).getByLabelText('Notifications') as HTMLInputElement).checked).toBe(false)
    fireEvent.click(within(list).getByRole('button', { name: 'About Tools this line may use' }), { detail: 0 })
    expect(screen.getByRole('tooltip')).toHaveTextContent(
      'These tools manage the platform. They stay off on this line unless you tick them.')
    // Another mode hides them again.
    fireEvent.click(screen.getByRole('radio', { name: /Per caller/ }))
    expect(screen.queryByText(HINT)).toBeNull()
    expect(screen.queryByText('Tools this line may use')).toBeNull()
  })

  it('reads the preview for this mode with the ticks, and keeps the list while it reads', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    chooseAgent()
    const list = await screen.findByTestId('route-line-tools')
    expect(previewCalls().some((p) => p.includes('identity_mode=agent') && !p.includes('line_tools='))).toBe(true)
    fireEvent.click(within(list).getByLabelText('Notifications'))
    expect(screen.getByTestId('route-line-tools')).toBeInTheDocument()
    await waitFor(() => {
      expect(previewCalls().some((p) => p.includes('identity_mode=agent') && p.includes('line_tools=notifications-mcp'))).toBe(true)
    })
  })

  it('sends the ticks as line_tools and asks in its own words before a save with no PIN', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    chooseAgent()
    const list = await screen.findByTestId('route-line-tools')
    fireEvent.click(within(list).getByLabelText('Schedules'))
    fireEvent.click(within(list).getByLabelText('Notifications'))
    fireEvent.click(within(list).getByLabelText('Schedules'))
    fireEvent.click(screen.getByText('Save'))
    const ask = await findAlertDialog('Save without a PIN?')
    expect(ask).toHaveTextContent(AGENT_NO_PIN)
    expect(routePut()).toBeUndefined()
    await act(async () => { await answerDialog('Save without a PIN', 'Save without a PIN?') })
    await waitFor(() => expect(routePut()).toBeTruthy())
    const body = JSON.parse(String(routePut()![1]!.body))
    expect(body.identity_mode).toBe('agent')
    expect(body.line_tools).toEqual(['notifications-mcp'])
    expect(body.acknowledge_no_pin).toBe(true)
  })

  it('Cancel on the confirm sends nothing', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    chooseAgent()
    fireEvent.click(screen.getByText('Save'))
    await act(async () => { await answerDialog('Cancel', 'Save without a PIN?') })
    expect(routePut()).toBeUndefined()
    expect(screen.getByRole('radio', { name: /The agent itself/ })).toBeChecked()
  })

  it('a PIN set in the same save asks nothing and sends no acknowledgement', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    chooseAgent()
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    fireEvent.change(screen.getByPlaceholderText('6 digits'), { target: { value: '471125' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(routePut()).toBeTruthy())
    const body = JSON.parse(String(routePut()![1]!.body))
    expect(body.identity_mode).toBe('agent')
    expect(body.pin).toBe('471125')
    expect(body).not.toHaveProperty('acknowledge_no_pin')
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('an outbound route in this mode may carry a PIN and needs no acknowledgement', async () => {
    mockApi([makeRoute({ direction: 'outbound', identity_mode: 'agent' })])
    renderTab()
    await openEdit()
    expect(screen.getByText('Require a PIN')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(routePut()).toBeTruthy())
    expect(JSON.parse(String(routePut()![1]!.body))).not.toHaveProperty('acknowledge_no_pin')
  })

  it('opens a saved route with its ticks, and the table names the mode', async () => {
    mockApi([makeRoute({ identity_mode: 'agent', line_tools: ['schedules-mcp'], pin_configured: true })])
    renderTab()
    expect(await screen.findByText('agent itself')).toBeInTheDocument()
    await openEdit()
    const list = await screen.findByTestId('route-line-tools')
    expect((within(list).getByLabelText('Schedules') as HTMLInputElement).checked).toBe(true)
    expect((within(list).getByLabelText('Notifications') as HTMLInputElement).checked).toBe(false)
  })

  it('leaving the mode drops the ticks from the save', async () => {
    mockApi([makeRoute({ identity_mode: 'agent', line_tools: ['schedules-mcp'], pin_configured: true })])
    renderTab()
    await openEdit()
    await screen.findByTestId('route-line-tools')
    fireEvent.click(screen.getByRole('radio', { name: /Per caller/ }))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(routePut()).toBeTruthy())
    const body = JSON.parse(String(routePut()![1]!.body))
    expect(body.identity_mode).toBe('caller')
    expect(body.line_tools).toEqual([])
  })

  it('draws no list when the preview offers no tool', async () => {
    mockApi([makeRoute()], { attached: [], excluded: [], line_tools_offered: [] })
    renderTab()
    await openEdit()
    chooseAgent()
    await screen.findByTestId('route-mcp-preview')
    expect(screen.queryByTestId('route-line-tools')).toBeNull()
  })
})
