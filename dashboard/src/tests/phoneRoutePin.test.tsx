/**
 * Route PIN UI (inbound-only option in the RouteModal): the section renders
 * only for inbound routes, a stored PIN is never echoed (mask flag only),
 * and Save routes the value through the dedicated write-only endpoint —
 * never the route payload.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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
    pin_configured: false, created_at: '', updated_at: '', ...over,
  }
}

function mockApi(routes: unknown[]) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone/routes')) {
      if (init?.method === 'PUT' || init?.method === 'POST') return ok({ id: 'r1' })
      return ok({ routes })
    }
    if (path.startsWith('/v1/admin/phone-servers')) {
      return ok({ servers: [{ id: 1, name: 'twil', adapter_type: 'twilio', is_default: true }] })
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

describe('route PIN option', () => {
  beforeEach(() => { vi.stubGlobal('alert', vi.fn()) })
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

  it('saves the PIN through the dedicated endpoint, not the route payload', async () => {
    mockApi([makeRoute()])
    renderTab()
    await openEdit()
    const card = screen.getByText('Require a PIN').closest('div')!.parentElement!
    fireEvent.click(card.querySelector('[role="switch"]')!)
    fireEvent.change(screen.getByPlaceholderText('4–6 digits'), { target: { value: '4711' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => {
      const pinCall = fetchSpy.mock.calls.find(
        ([p, i]) => p === '/v1/admin/phone/routes/r1/pin' && i?.method === 'PUT')
      expect(pinCall).toBeTruthy()
      expect(pinCall![1]!.body).toBe(JSON.stringify({ value: '4711' }))
    })
    // The route PUT itself carries neither the value nor the mask flag.
    const routePut = fetchSpy.mock.calls.find(
      ([p, i]) => p === '/v1/admin/phone/routes/r1' && i?.method === 'PUT')
    expect(routePut).toBeTruthy()
    expect(String(routePut![1]!.body)).not.toContain('4711')
    expect(String(routePut![1]!.body)).not.toContain('pin_configured')
  })

  it('turning the toggle off on a configured route deletes the PIN on save', async () => {
    mockApi([makeRoute({ pin_configured: true })])
    renderTab()
    await openEdit()
    // The card's toggle is ON (stored PIN); the row's enable toggle is a
    // different switch — pick the one inside the PIN card via its container.
    const card = screen.getByText('Require a PIN').closest('div')!.parentElement!
    fireEvent.click(card.querySelector('[role="switch"]')!)
    expect(screen.getByText('The PIN will be removed when you save.')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      expect(fetchSpy.mock.calls.some(
        ([p, i]) => p === '/v1/admin/phone/routes/r1/pin' && i?.method === 'DELETE')).toBe(true)
    })
  })

  it('shows the lock marker on protected route rows', async () => {
    mockApi([makeRoute({ pin_configured: true })])
    renderTab()
    expect(await screen.findByLabelText('PIN protected')).toBeInTheDocument()
  })
})
