/**
 * Admin → Setup → Phone: a settings field that is focused and left untouched
 * saves nothing, whenever its card was opened. Before, a Languages card opened
 * while the phone settings still loaded saved an empty backchannel or thinking
 * list over every language's list on a blur, and saved the same list again
 * after the load; an Infrastructure number left before the load saved its
 * built-in default (a custom port among them); and an Advanced tuning number
 * the person had changed kept that change after Restore Defaults, so leaving it
 * wrote it back over the restored default.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { answerDialog, renderWithDialogs } from './dialogsHarness'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const PHONE = {
  phrases: JSON.stringify({ en: { hold_message: 'Please hold for Acme.', greeting_fallback: 'Acme, hello.' } }),
  backchannel_phrases: JSON.stringify({ en: ['Mhm', 'Right'], el: ['Ναι'] }),
  thinking_phrases: JSON.stringify({ en: ['Let me check'] }),
  http_api_port: '9100', idle_timeout_s: '45', call_max_duration_s: '900', outbound_call_timeout_s: '240',
  bargein_threshold: '0.7', log_level: 'INFO',
}
const AUDIO = { vad_threshold: '0.55', smart_turn_threshold: '0.6', language_candidates: 'en,el' }

/** The settings reads wait until the test lets them answer; a write changes
 *  what the next read answers, as the server does; every write is kept. */
function mockApi() {
  const stores: Record<string, Record<string, string>> = {
    '/v1/admin/phone/settings': { ...PHONE }, '/v1/admin/audio/settings': { ...AUDIO },
  }
  const puts: Array<{ path: string; body: Record<string, string> }> = []
  let answer: () => void = () => undefined
  const loaded = new Promise<void>(resolve => { answer = resolve })
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (init?.method === 'PUT') {
      const body = JSON.parse(String(init.body))
      puts.push({ path, body })
      Object.assign(stores[path] ?? {}, body)
      return ok({ status: 'updated' })
    }
    if (path in stores) { await loaded; return ok({ ...stores[path] }) }
    if (path === '/v1/admin/audio/policy') return ok({ show_experimental: true })
    if (path.startsWith('/v1/admin/phone/routes')) return ok({ routes: [] })
    if (path.startsWith('/v1/admin/phone-servers')) return ok({ servers: [] })
    if (path.startsWith('/v1/agents')) return ok({ agents: [] })
    if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
    return ok({})
  })
  return { puts, answer: () => act(async () => { answer(); await loaded }) }
}

async function openCard(title: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  renderWithDialogs(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
  fireEvent.click(await screen.findByText(title))
}

/** Focus and leave every field shown, as a click in and out does. */
function touchAll(selector: string) {
  for (const field of document.querySelectorAll<HTMLInputElement>(selector)) {
    fireEvent.focus(field)
    fireEvent.blur(field)
  }
}

const settle = () => act(async () => { await new Promise(r => setTimeout(r, 50)) })

afterEach(() => { fetchSpy.mockReset() })

describe('a phone settings card opened before the settings load', () => {
  it('Languages waits, then shows the saved phrases, and leaving them saves nothing', async () => {
    const api = mockApi()
    await openCard('Languages')
    touchAll('input[type=text], input:not([type])')
    await api.answer()
    expect(await screen.findByDisplayValue('Please hold for Acme.')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Acme, hello.')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Mhm, Right')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Let me check')).toBeInTheDocument()
    touchAll('input[type=text], input:not([type])')
    await settle()
    expect(api.puts).toEqual([])
  })

  it('Infrastructure waits, then shows the saved numbers, and leaving them saves nothing', async () => {
    const api = mockApi()
    await openCard('Infrastructure')
    touchAll('input[type=number]')
    await api.answer()
    expect(await screen.findByDisplayValue('9100')).toBeInTheDocument()
    expect(screen.getByDisplayValue('45')).toBeInTheDocument()
    // No saved row for the Q&A timeout: the field shows the default in force.
    expect(screen.getByDisplayValue('40')).toBeInTheDocument()
    touchAll('input[type=number]')
    await settle()
    expect(api.puts).toEqual([])
  })

  it('Advanced tuning shows the saved numbers, and leaving them saves nothing', async () => {
    const api = mockApi()
    await openCard('Advanced')
    // The section is drawn (the policy answered) while the settings still load.
    await screen.findByText('VAD Threshold')
    touchAll('input[type=number]')
    await api.answer()
    expect(await screen.findByDisplayValue('0.55')).toBeInTheDocument()
    expect(screen.getByDisplayValue('0.7')).toBeInTheDocument()
    touchAll('input[type=number]')
    await settle()
    expect(api.puts).toEqual([])
  })
})

describe('a phone settings field after a change', () => {
  it('a changed phrase list still saves, the other languages kept', async () => {
    const api = mockApi()
    await openCard('Languages')
    await api.answer()
    const field = await screen.findByDisplayValue('Mhm, Right')
    fireEvent.change(field, { target: { value: 'Mhm, Right, Okay' } })
    fireEvent.blur(field)
    await waitFor(() => expect(api.puts).toEqual([{
      path: '/v1/admin/phone/settings',
      body: { backchannel_phrases: JSON.stringify({ en: ['Mhm', 'Right', 'Okay'], el: ['Ναι'] }) },
    }]))
  })

  it('Advanced tuning follows Restore Defaults over a number the person changed', async () => {
    const api = mockApi()
    await openCard('Advanced')
    await api.answer()
    const vad = await screen.findByDisplayValue('0.55')
    fireEvent.change(vad, { target: { value: '0.6' } })
    fireEvent.blur(vad)
    await waitFor(() => expect(api.puts).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: 'Restore Defaults' }))
    await answerDialog('Restore')
    await waitFor(() => expect(api.puts).toHaveLength(3))
    const restored = await screen.findByDisplayValue('0.40')
    fireEvent.focus(restored)
    fireEvent.blur(restored)
    await settle()
    expect(api.puts).toHaveLength(3)
  })
})
