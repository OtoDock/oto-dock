/**
 * Transfer targets on a route (RouteModal): the editor lists the configured
 * targets by name and number, a row is added and removed, and the save sends
 * the list as `transfer_targets`. A route row without the field (an older
 * server) renders an empty editor.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'
import { renderWithDialogs } from './dialogsHarness'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

function makeRoute(over: Record<string, unknown> = {}) {
  return {
    id: 'r1', direction: 'inbound', name: 'Main Line', agent: 'assistant',
    language: 'en', language_detect: false, llm_mode: 'proxy', phone_server_id: 1,
    stt_provider_id: null, tts_provider_id: null, greeting: '',
    phone_context_override: '', backchannel_mode: 'on',
    thinking_filler_mode: 'on', background_sound: 'off', enabled: true,
    audiosocket_uuid: null, did: '+16083191947', ami_caller_id: '',
    ami_outbound_context: '', dial_prefix: '', trigger_slug: null,
    identity_mode: 'caller', identity_user_sub: null, role: 'viewer', remember_callers: true,
    transfer_targets: [{ label: 'Reception', number: '200' }],
    pin_configured: false, warnings: [], created_at: '', updated_at: '', ...over,
  }
}

function mockApi(routes: unknown[]) {
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (path.startsWith('/v1/admin/phone/routes/mcp-preview')) return ok({ attached: [], excluded: [] })
    if (path.startsWith('/v1/admin/phone/routes')) {
      if (init?.method === 'PUT' || init?.method === 'POST') return ok({ id: 'r1' })
      return ok({ routes })
    }
    if (path.startsWith('/v1/admin/phone-servers')) {
      return ok({ servers: [{ id: 1, name: 'pbx', adapter_type: 'asterisk_manual', is_default: true }] })
    }
    if (path.startsWith('/v1/agents')) return ok({ agents: [{ name: 'assistant', execution_path: 'claude-code-cli' }] })
    if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
    return ok({})
  })
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderWithDialogs(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
}

function routePut() {
  return fetchSpy.mock.calls.find(([p, i]) => p === '/v1/admin/phone/routes/r1' && i?.method === 'PUT')
}

describe('route transfer targets', () => {
  afterEach(() => { fetchSpy.mockReset() })

  it('lists the targets, adds one and saves the list', async () => {
    mockApi([makeRoute()])
    renderTab()
    fireEvent.click(await screen.findByText('Edit'))
    const editor = await screen.findByTestId('transfer-targets')
    expect((within(editor).getByLabelText('Transfer target 1 name') as HTMLInputElement).value).toBe('Reception')
    expect((within(editor).getByLabelText('Transfer target 1 number') as HTMLInputElement).value).toBe('200')

    fireEvent.click(within(editor).getByText('+ Add target'))
    fireEvent.change(within(editor).getByLabelText('Transfer target 2 name'), { target: { value: 'Maria' } })
    fireEvent.change(within(editor).getByLabelText('Transfer target 2 number'), { target: { value: '+306912345678' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      const put = routePut()
      expect(put).toBeTruthy()
      const body = JSON.parse(String(put![1]!.body))
      expect(body.transfer_targets).toEqual([
        { label: 'Reception', number: '200' },
        { label: 'Maria', number: '+306912345678' },
      ])
    })
  })

  it('wraps the target rows and keeps the overrides, the voice and the Asterisk fields under Advanced', async () => {
    mockApi([makeRoute()])
    renderTab()
    fireEvent.click(await screen.findByText('Edit'))
    const editor = await screen.findByTestId('transfer-targets')
    expect(editor.querySelector('.flex-wrap')).not.toBeNull()
    const advanced = screen.getByTestId('route-advanced')
    expect(within(advanced).getByText('STT Provider')).toBeInTheDocument()
    expect(within(advanced).getByText('TTS Provider')).toBeInTheDocument()
    expect(within(advanced).getByText('AudioSocket UUID')).toBeInTheDocument()   // an Asterisk server, inbound
    expect(screen.getByText('Set up your inbound and outbound call routes.')).toBeInTheDocument()
    fireEvent.change(within(advanced).getByLabelText('Voice id'), { target: { value: 'a7be ff01' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => {
      const put = routePut()
      expect(put).toBeTruthy()
      expect(JSON.parse(String(put![1]!.body)).voice_id).toBe('a7beff01')   // no whitespace ever
    })
  })

  it('a Twilio line shows none of the Asterisk fields', async () => {
    mockApi([makeRoute()])
    fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
      const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
      if (path.startsWith('/v1/admin/phone/routes/mcp-preview')) return ok({ attached: [], excluded: [] })
      if (path.startsWith('/v1/admin/phone/routes')) return init?.method ? ok({ id: 'r1' }) : ok({ routes: [makeRoute()] })
      if (path.startsWith('/v1/admin/phone-servers')) return ok({ servers: [{ id: 1, name: 'twil', adapter_type: 'twilio', is_default: true }] })
      if (path.startsWith('/v1/agents')) return ok({ agents: [{ name: 'assistant' }] })
      if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
      return ok({})
    })
    renderTab()
    fireEvent.click(await screen.findByText('Edit'))
    const advanced = await screen.findByTestId('route-advanced')
    expect(within(advanced).getByLabelText('Voice id')).toBeInTheDocument()
    expect(within(advanced).queryByText('AudioSocket UUID')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'outbound' }))
    expect(within(advanced).queryByText('Outbound Dial Prefix')).not.toBeInTheDocument()
    expect(within(advanced).queryByText('Dialplan Context')).not.toBeInTheDocument()
  })

  it('removes a target and tolerates a row without the field', async () => {
    mockApi([makeRoute({ transfer_targets: undefined })])
    renderTab()
    fireEvent.click(await screen.findByText('Edit'))
    const editor = await screen.findByTestId('transfer-targets')
    expect(within(editor).queryByLabelText('Transfer target 1 name')).not.toBeInTheDocument()
    fireEvent.click(within(editor).getByText('+ Add target'))
    expect(within(editor).getByLabelText('Transfer target 1 name')).toBeInTheDocument()
    fireEvent.click(within(editor).getByLabelText('Remove transfer target 1'))
    expect(within(editor).queryByLabelText('Transfer target 1 name')).not.toBeInTheDocument()
  })
})
