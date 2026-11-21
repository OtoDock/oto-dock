/**
 * Admin, Setup, Phone, Advanced: the experimental tuning's groups. Six of
 * them, in this order, and no switch for the speech detector's feed: every
 * detector has the one feed, and no setting chooses it. Restore Defaults
 * sends no key of the two switches earlier builds had.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { answerDialog, renderWithDialogs } from './dialogsHarness'

import * as authApi from '@/api/auth'
import PhoneServersTab from '@/pages/admin/PhoneServersTab'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

/** The settings as the server holds them, with the rows two earlier builds
 *  stored for the detector's feed. Every write is kept. */
function mockApi() {
  const stores: Record<string, Record<string, string>> = {
    '/v1/admin/phone/settings': { vad_context: 'on', voice_vad_context: 'on', bargein_threshold: '0.35' },
    '/v1/admin/audio/settings': { vad_threshold: '0.40' },
  }
  const puts: Array<{ path: string; body: Record<string, string> }> = []
  fetchSpy.mockImplementation(async (path: string, init?: RequestInit) => {
    const ok = (body: unknown) => ({ ok: true, json: async () => body }) as Response
    if (init?.method === 'PUT') {
      const body = JSON.parse(String(init.body))
      puts.push({ path, body })
      Object.assign(stores[path] ?? {}, body)
      return ok({ status: 'updated' })
    }
    if (path in stores) return ok({ ...stores[path] })
    if (path === '/v1/admin/audio/policy') return ok({ show_experimental: true })
    if (path.startsWith('/v1/admin/phone/routes')) return ok({ routes: [] })
    if (path.startsWith('/v1/admin/phone-servers')) return ok({ servers: [] })
    if (path.startsWith('/v1/agents')) return ok({ agents: [] })
    if (path.startsWith('/v1/triggers')) return ok({ triggers: [] })
    return ok({})
  })
  return { puts }
}

async function openAdvanced() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  renderWithDialogs(<QueryClientProvider client={qc}><PhoneServersTab /></QueryClientProvider>)
  fireEvent.click(await screen.findByText('Advanced'))
  await screen.findByText('Voice Activity Detection')
}

afterEach(() => { fetchSpy.mockReset() })

describe('the experimental tuning of the phone tab', () => {
  it('has its six groups in order and no switch for the detector', async () => {
    mockApi()
    await openAdvanced()
    const groups = screen.getAllByRole('heading', { level: 4 }).map(h => h.textContent)
    expect(groups).toEqual([
      'Voice Activity Detection', 'Smart Turn', 'Barge-In on Calls', 'Turn Timing on Calls',
      'TTS Buffering on Calls', 'Fillers on Calls',
    ])
    // One switch in the card, the one that shows the tuning.
    expect(screen.getAllByRole('switch').map(s => s.getAttribute('aria-label'))).toEqual(['Show experimental tuning'])
    expect(screen.queryByText(/hear quiet/i)).not.toBeInTheDocument()
  })

  it('restores the defaults without a key for the detector\'s feed', async () => {
    const api = mockApi()
    await openAdvanced()
    fireEvent.click(screen.getByRole('button', { name: 'Restore Defaults' }))
    await answerDialog('Restore')
    await waitFor(() => expect(api.puts).toHaveLength(2))
    const phone = api.puts.find(p => p.path === '/v1/admin/phone/settings')
    expect(Object.keys(phone?.body ?? {}).filter(k => k.includes('vad_context'))).toEqual([])
    expect(phone?.body.bargein_threshold).toBe('0.35')
  })
})
