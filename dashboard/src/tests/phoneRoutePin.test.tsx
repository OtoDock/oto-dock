/**
 * Route PIN UI (inbound-only option in the RouteModal): the section renders
 * only for inbound routes, a stored PIN is never echoed (mask flag only), a
 * PIN being set rides the route save as `pin` (never the mask flag, never a
 * stale value on an outbound save), and a removal goes to the write-only
 * DELETE endpoint after the save.
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

function mockApi(routes: unknown[]) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone/routes/mcp-preview')) return ok({ attached: [], excluded: [] })
    if (path.startsWith('/v1/admin/phone/routes')) {
      if (init?.method === 'PUT' || init?.method === 'POST') return ok({ id: 'r1' })
      if (init?.method === 'DELETE') return ok({})
      return ok({ routes })
    }
    if (path.startsWith('/v1/admin/phone-servers')) {
      return ok({ servers: [{ id: 1, name: 'twil', adapter_type: 'twilio', is_default: true }] })
    }
    if (path.startsWith('/v1/agents/assistant/users')) {
      return ok({ users: [{ sub: 'u-alice', name: 'Alice', email: 'alice@x', role: 'editor' }] })
    }
    if (path.startsWith('/v1/agents')) return ok({ agents: [{ name: 'assistant' }] })
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

function pinCard() {
  return screen.getByText('Require a PIN').closest('div')!.parentElement!
}

function routeSave(method: 'PUT' | 'POST') {
  const path = method === 'PUT' ? '/v1/admin/phone/routes/r1' : '/v1/admin/phone/routes'
  return fetchSpy.mock.calls.find(([p, i]) => p === path && i?.method === method)
}

function pinRequests() {
  return fetchSpy.mock.calls.filter(([p]) => String(p).startsWith('/v1/admin/phone/routes/r1/pin'))
}

describe('route PIN option', () => {
  beforeEach(() => { vi.stubGlobal('alert', vi.fn()); vi.stubGlobal('confirm', vi.fn(() => true)) })
  afterEach(() => { fetchSpy.mockReset(); vi.unstubAllGlobals() })

  it('renders only for inbound routes', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    expect(screen.getByText('Require a PIN')).toBeInTheDocument()
    // Flip the modal to outbound — the PIN section must disappear live.
    fireEvent.click(screen.getByRole('button', { name: 'outbound' }))
    expect(screen.queryByText('Require a PIN')).not.toBeInTheDocument()
  })

  it('never echoes a stored PIN — mask flag + placeholder only', async () => {
    mockApi([makeRoute({ pin_configured: true })])
    renderTab()
    await openEdit()
    expect(screen.getByText('(set)')).toBeInTheDocument()
    const input = screen.getByPlaceholderText('••••••') as HTMLInputElement
    expect(input.value).toBe('')
    expect(input.type).toBe('password')
  })

  it('sends the PIN in the route request, never the mask flag', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    fireEvent.change(screen.getByPlaceholderText('4–6 digits'), { target: { value: '4711' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => {
      const put = routeSave('PUT')
      expect(put).toBeTruthy()
      const body = JSON.parse(String(put![1]!.body))
      expect(body.pin).toBe('4711')
      expect(body).not.toHaveProperty('pin_configured')
      expect(body).not.toHaveProperty('acknowledge_no_pin')
    })
    expect(pinRequests()).toHaveLength(0)
    expect(vi.mocked(confirm)).not.toHaveBeenCalled()
  })

  it('a new user-mode route with a PIN sends it in the POST and asks nothing', async () => {
    mockApi([])
    renderTab()
    fireEvent.click(await screen.findByText('Add Route'))
    fireEvent.click(screen.getByLabelText(/A platform user/))
    const picker = await screen.findByLabelText('Platform user')
    await within(picker).findByText(/Alice/)
    fireEvent.change(picker, { target: { value: 'u-alice' } })
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    fireEvent.change(screen.getByPlaceholderText('4–6 digits'), { target: { value: '4711' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => {
      const post = routeSave('POST')
      expect(post).toBeTruthy()
      const body = JSON.parse(String(post![1]!.body))
      expect(body.pin).toBe('4711')
      expect(body.identity_user_sub).toBe('u-alice')
      expect(body).not.toHaveProperty('acknowledge_no_pin')
    })
    expect(pinRequests()).toHaveLength(0)
    expect(vi.mocked(confirm)).not.toHaveBeenCalled()
  })

  it('a PIN typed before a flip to outbound never rides the save', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    fireEvent.change(screen.getByPlaceholderText('4–6 digits'), { target: { value: '4711' } })
    fireEvent.click(screen.getByRole('button', { name: 'outbound' }))
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      const put = routeSave('PUT')
      expect(put).toBeTruthy()
      const body = JSON.parse(String(put![1]!.body))
      expect(body.direction).toBe('outbound')
      expect(body).not.toHaveProperty('pin')
    })
    expect(String(routeSave('PUT')![1]!.body)).not.toContain('4711')
  })

  it('turning the toggle off on a configured route deletes the PIN on save', async () => {
    mockApi([makeRoute({ pin_configured: true })])
    renderTab()
    await openEdit()
    // The card's toggle is ON (stored PIN); the row's enable toggle is a
    // different switch — pick the one inside the PIN card via its container.
    fireEvent.click(pinCard().querySelector('[role="switch"]')!)
    expect(screen.getByText('The PIN will be removed when you save.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(
        ([p, i]) => p === '/v1/admin/phone/routes/r1/pin' && i?.method === 'DELETE')).toBe(true)
    })
    // A caller-mode route needs no acknowledgement: no query string.
    expect(vi.mocked(confirm)).not.toHaveBeenCalled()
  })

  it('shows the lock marker on protected route rows', async () => {
    mockApi([makeRoute({ pin_configured: true })])
    renderTab()
    expect(await screen.findByLabelText('PIN protected')).toBeInTheDocument()
  })
})
