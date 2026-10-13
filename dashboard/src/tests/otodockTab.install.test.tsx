// The OtoDock tab's "This install" card: the running version (the socket's
// when the REST answer has none), the latest release, the switched-off
// reason, "Check now" posting the check, and the version alone on the cloud.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  notice: {} as Record<string, unknown>,
  requests: [] as { url: string; method: string }[],
  cloud: false,
}))

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'admin', agent_roles: {} }, authConfig: { cloud: h.cloud } }),
}))
vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (url: string, init?: { method?: string }) => {
    h.requests.push({ url, method: init?.method ?? 'GET' })
    if (init?.method === 'POST') return { ok: true, json: async () => ({ ...h.notice, latest: '1.9.0', newer: true, checked_at: '2026-10-21T03:00:00+00:00' }) }
    return { ok: true, json: async () => h.notice }
  }),
}))

import { ThisInstallCard } from '@/pages/admin/OtodockTab'
import { noteServerVersion } from '@/lib/session/serverInfo'

const BASE = {
  enabled: true, reason: '', running: '1.8.0', latest: '', url: '', published_at: '',
  checked_at: '', error: '', newer: false,
}

function show() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><ThisInstallCard /></QueryClientProvider>)
}

beforeEach(() => {
  h.notice = { ...BASE }
  h.requests = []
  h.cloud = false
  noteServerVersion('')
})

describe('ThisInstallCard', () => {
  it('shows the running version and runs a check on demand', async () => {
    show()
    await screen.findByText('No release check has run yet.')
    expect(screen.getByTestId('running-version').textContent).toBe('OtoDock 1.8.0')
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }))
    await waitFor(() => expect(h.requests.some((r) => r.method === 'POST' && r.url === '/v1/admin/release-check')).toBe(true))
    await screen.findByText('OtoDock 1.9.0 is available.')
    expect(screen.getByText(/checked 2026-10-21 03:00/)).toBeTruthy()
    expect(screen.queryByText(/upgrade.sh/)).toBeNull()
  })

  it('on the cloud shows the running version alone', async () => {
    h.cloud = true
    h.notice = { ...BASE, enabled: false, reason: 'the operator upgrades the OtoDock cloud', latest: '1.9.0', newer: true }
    show()
    await screen.findByTestId('running-version')
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.getByTestId('running-version').textContent).toBe('OtoDock 1.8.0')
    expect(screen.queryByText(/Release checks are off/)).toBeNull()
    expect(screen.queryByText(/is available/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull()
  })

  it('says why the check is off and falls back to the socket version', async () => {
    h.notice = { ...BASE, running: '', enabled: false, reason: 'OTODOCK_AIR_GAPPED is set' }
    noteServerVersion('1.8.0-dev')
    show()
    await screen.findByText('Release checks are off (OTODOCK_AIR_GAPPED is set).')
    expect(screen.getByTestId('running-version').textContent).toBe('OtoDock 1.8.0-dev')
    expect(screen.queryByRole('button', { name: 'Check now' })).toBeNull()
  })

  it('reports an up-to-date install and the last error', async () => {
    h.notice = { ...BASE, latest: '1.8.0', checked_at: '2026-10-21T03:00:00+00:00', error: 'HTTP 403 from x' }
    show()
    await screen.findByText('Up to date (the latest release is 1.8.0).')
    expect(screen.getByText(/Last check failed: HTTP 403 from x/)).toBeTruthy()
  })
})
