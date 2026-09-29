/**
 * Route identity UI (RouteModal + routes table): the two identity modes,
 * the tied-user picker with its PIN advisory, the remember-callers toggle
 * (callers are always viewers — no role control), the MCP preview, the
 * server-computed warnings surfaced on save and in the table, and the
 * acknowledgement a user-mode inbound route needs to save without a PIN.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

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
    pin_configured: false, warnings: [], created_at: '', updated_at: '', ...over,
  }
}

const PREVIEW = {
  attached: [{ name: 'memory-mcp', label: 'Memory' }],
  excluded: [{ name: 'schedules-mcp', label: 'Schedules', reason: 'Not available to external callers' }],
}

const NO_PIN_NOTE = 'This line has no PIN: anyone who dials it runs as Alice. Set a PIN on the route.'
const ADMIN_NOTE = 'Alice is a platform admin on an admin-only agent; calls run with manager rights on it (the cap for phone lines).'

function mockApi(routes: unknown[], opts: {
  engine?: string; saveResponse?: Record<string, unknown>; pinDeleteResponse?: Record<string, unknown>
} = {}) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone/routes/mcp-preview')) return ok(PREVIEW)
    if (path.startsWith('/v1/admin/phone/routes')) {
      if (init?.method === 'PUT' || init?.method === 'POST') return ok({ id: 'r1', ...(opts.saveResponse ?? {}) })
      if (init?.method === 'DELETE') return ok(opts.pinDeleteResponse ?? {})
      return ok({ routes })
    }
    if (path.startsWith('/v1/admin/phone-servers')) {
      return ok({ servers: [{ id: 1, name: 'twil', adapter_type: 'twilio', is_default: true }] })
    }
    if (path.startsWith('/v1/agents/assistant/users')) {
      return ok({ users: [{ sub: 'u-alice', name: 'Alice', email: 'alice@x', role: 'editor' }] })
    }
    if (path.startsWith('/v1/agents')) {
      return ok({ agents: [{ name: 'assistant', execution_path: opts.engine ?? 'claude-code-cli' }] })
    }
    if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
    return ok({})
  })
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
}

async function openEdit() {
  fireEvent.click(await screen.findByText('Edit'))
}

// The confirm names the picked user, so a test waits for the picker's list.
async function pickerWithAlice() {
  const picker = await screen.findByLabelText('Platform user')
  await within(picker).findByText(/Alice/)
  return picker
}

function routePut() {
  return fetchSpy.mock.calls.find(([p, i]) => p === '/v1/admin/phone/routes/r1' && i?.method === 'PUT')
}

function pinCard() {
  return screen.getByText('Require a PIN').closest('div')!.parentElement!
}

describe('route identity', () => {
  beforeEach(() => { vi.stubGlobal('alert', vi.fn()); vi.stubGlobal('confirm', vi.fn(() => true)) })
  afterEach(() => { fetchSpy.mockReset(); vi.unstubAllGlobals() })

  it('defaults to per-caller with the remember control and the tool preview', async () => {
    mockApi([makeRoute({ role: 'manager' })])   // a legacy column value: never shown
    renderTab()
    // The table names the identity without a role suffix.
    const cell = await screen.findByText('per caller')
    expect(cell).not.toHaveTextContent('manager')
    await openEdit()
    const group = screen.getByRole('radiogroup', { name: 'Caller identity' })
    expect((within(group).getByLabelText(/Per caller/) as HTMLInputElement).checked).toBe(true)
    // Callers are always viewers: no role control anywhere in the form.
    expect(screen.queryByLabelText('Role on the agent')).not.toBeInTheDocument()
    expect(screen.queryByText(/Editor|Manager —/)).not.toBeInTheDocument()
    expect(screen.getByText(/Callers read the agent's shared files as viewers/)).toBeInTheDocument()
    expect(screen.getByText('Remember callers')).toBeInTheDocument()
    expect(screen.queryByLabelText('Platform user')).not.toBeInTheDocument()
    // The preview names what reaches the line and what never does.
    const preview = await screen.findByTestId('route-mcp-preview')
    expect(preview).toHaveTextContent('Available on this line: Memory')
    expect(preview).toHaveTextContent('Not on calls: Schedules')
    expect(fetchSpy.mock.calls.some(([p]) =>
      String(p).startsWith('/v1/admin/phone/routes/mcp-preview?') && String(p).includes('identity_mode=caller'))).toBe(true)
  })

  it('a user-tied route shows the attached-user picker, the PIN advisory, and hides remember-callers', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    fireEvent.click(screen.getByLabelText(/A platform user/))
    const picker = await pickerWithAlice()
    expect(screen.getByText(/anyone who dials this number acts as that user/)).toBeInTheDocument()
    expect(screen.queryByText('Remember callers')).not.toBeInTheDocument()
    // No user picked yet → cannot save.
    expect(screen.getByText('Save').closest('button')).toBeDisabled()
    fireEvent.change(picker, { target: { value: 'u-alice' } })
    expect(screen.getByText('Save').closest('button')).not.toBeDisabled()
    // Saving without a PIN asks first and sends the acknowledgement.
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      const put = routePut()
      expect(put).toBeTruthy()
      const body = JSON.parse(String(put![1]!.body))
      expect(body.identity_mode).toBe('user')
      expect(body.identity_user_sub).toBe('u-alice')
      expect(body.acknowledge_no_pin).toBe(true)
      expect(body).not.toHaveProperty('warnings')
      expect(body).not.toHaveProperty('pin')
    })
    expect(vi.mocked(confirm)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(confirm)).toHaveBeenCalledWith(expect.stringMatching(/act as Alice, with all their tools and credentials\. Save without a PIN\?/))
  })

  it('offers exactly two identities — per caller and a platform user', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    const group = screen.getByRole('radiogroup', { name: 'Caller identity' })
    expect(within(group).getAllByRole('radio')).toHaveLength(2)
    expect(within(group).queryByLabelText(/^Shared/)).not.toBeInTheDocument()
  })

  it('shows no engine warning of its own for a Codex agent (the server decides)', async () => {
    // Codex takes per-caller calls in the local sandbox (2026-09-08); the
    // one remaining case (a remote execution target) arrives as a server
    // warning on the row, like every other advisory.
    mockApi([makeRoute()], { engine: 'codex-cli' })
    renderTab()
    await openEdit()
    await screen.findByTestId('route-mcp-preview')
    expect(screen.queryByText(/cannot take external calls/)).not.toBeInTheDocument()
    expect(screen.queryByText(/runs on Codex/)).not.toBeInTheDocument()
  })

  it('surfaces the server warnings on save and marks the row, minus the note just confirmed', async () => {
    mockApi([makeRoute({ identity_mode: 'user', identity_user_sub: 'u-alice', warnings: [NO_PIN_NOTE] })],
      { saveResponse: { warnings: [NO_PIN_NOTE, ADMIN_NOTE] } })
    renderTab()
    expect(await screen.findByLabelText('Route warnings')).toBeInTheDocument()
    await openEdit()
    await pickerWithAlice()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      expect(vi.mocked(alert)).toHaveBeenCalledWith(expect.stringContaining(ADMIN_NOTE))
    })
    expect(vi.mocked(confirm)).toHaveBeenCalledTimes(1)
    expect(vi.mocked(confirm)).toHaveBeenCalledWith(expect.stringContaining('act as Alice'))
    expect(vi.mocked(alert)).not.toHaveBeenCalledWith(expect.stringContaining('no PIN'))
  })

  it('Cancel on the confirm sends nothing and keeps the modal open', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    mockApi([makeRoute({ identity_mode: 'user', identity_user_sub: 'u-alice', warnings: [NO_PIN_NOTE] })])
    renderTab()
    await openEdit()
    await pickerWithAlice()
    fireEvent.click(screen.getByText('Save'))
    expect(vi.mocked(confirm)).toHaveBeenCalledTimes(1)
    expect(routePut()).toBeUndefined()
    expect(screen.getByText('Save')).toBeInTheDocument()
    expect(screen.getByLabelText('Platform user')).toBeInTheDocument()
  })

  it('setting a PIN in the same save sends it with the route and asks nothing', async () => {
    mockApi([makeRoute({ identity_mode: 'user', identity_user_sub: 'u-alice', warnings: [NO_PIN_NOTE] })],
      { saveResponse: { warnings: [] } })
    renderTab()
    await openEdit()
    await pickerWithAlice()
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    fireEvent.change(screen.getByPlaceholderText('4–6 digits'), { target: { value: '4711' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      const put = routePut()
      expect(put).toBeTruthy()
      const body = JSON.parse(String(put![1]!.body))
      expect(body.pin).toBe('4711')
      expect(body).not.toHaveProperty('acknowledge_no_pin')
    })
    expect(fetchSpy.mock.calls.some(([p]) => String(p).startsWith('/v1/admin/phone/routes/r1/pin'))).toBe(false)
    expect(vi.mocked(confirm)).not.toHaveBeenCalled()
    expect(vi.mocked(alert)).not.toHaveBeenCalledWith(expect.stringContaining('no PIN'))
  })

  it('turning the PIN off on a user-mode route asks, flags the removal, and does not repeat the note', async () => {
    mockApi([makeRoute({ identity_mode: 'user', identity_user_sub: 'u-alice', pin_configured: true })],
      { saveResponse: { warnings: [] }, pinDeleteResponse: { warnings: [NO_PIN_NOTE] } })
    renderTab()
    await openEdit()
    await pickerWithAlice()
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    expect(screen.getByText('The PIN will be removed when you save.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(
        ([p, i]) => p === '/v1/admin/phone/routes/r1/pin?acknowledge_no_pin=true' && i?.method === 'DELETE')).toBe(true)
    })
    expect(vi.mocked(confirm)).toHaveBeenCalledTimes(1)
    expect(JSON.parse(String(routePut()![1]!.body)).acknowledge_no_pin).toBe(true)
    expect(vi.mocked(alert)).not.toHaveBeenCalledWith(expect.stringContaining('no PIN'))
  })

  it('a caller-mode save never asks', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => { expect(routePut()).toBeTruthy() })
    expect(vi.mocked(confirm)).not.toHaveBeenCalled()
    expect(JSON.parse(String(routePut()![1]!.body))).not.toHaveProperty('acknowledge_no_pin')
  })
})
