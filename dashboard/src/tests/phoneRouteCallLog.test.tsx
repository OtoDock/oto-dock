/**
 * Per-route call-log modal: rows with caller numbers + outcome chips
 * (incl. failed-PIN attempts), and the empty state.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import * as authApi from '@/api/auth'
import CallLogModal from '@/components/phone/CallLogModal'
import type { PhoneRoute } from '@/api/phone'

const fetchSpy = vi.spyOn(authApi, 'apiFetch')

const ROUTE = { id: 'r1', name: 'Main Line', direction: 'inbound' } as PhoneRoute

function entry(over: Record<string, unknown> = {}) {
  return {
    id: 1, route_id: 'r1', route_name: 'Main Line', phone_server_id: 1,
    agent: 'assistant', direction: 'inbound', from_number: '+15550001111',
    to_number: '+16083191947', transport: 'twilio', call_uuid: 'CA1',
    outcome: 'completed', pin_attempts: 0,
    started_at: new Date().toISOString(), ended_at: null, duration_s: 65,
    created_at: '', ...over,
  }
}

function mockLog(calls: unknown[], total = calls.length) {
  fetchSpy.mockImplementation(async () =>
    ({ ok: true, json: async () => ({ calls, total }) }) as Response)
}

function renderModal() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <CallLogModal route={ROUTE} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

describe('CallLogModal', () => {
  afterEach(() => { fetchSpy.mockReset() })

  it('renders calls with numbers, outcomes, and failed-PIN attempts', async () => {
    mockLog([
      entry(),
      entry({ id: 2, outcome: 'pin_failed', pin_attempts: 3,
              from_number: '+15550002222', duration_s: 31 }),
    ])
    renderModal()
    expect(await screen.findByText('+15550001111')).toBeInTheDocument()
    expect(screen.getByText('Completed')).toBeInTheDocument()
    expect(screen.getByText('1m 5s')).toBeInTheDocument()
    // The failed-PIN caller is exactly what the admin came to see.
    expect(screen.getByText('+15550002222')).toBeInTheDocument()
    expect(screen.getByText('Wrong PIN')).toBeInTheDocument()
    expect(screen.getByText('3 attempts')).toBeInTheDocument()
  })

  it('shows outbound callee numbers and the empty state', async () => {
    mockLog([entry({ direction: 'outbound', to_number: '+15559998888' })])
    renderModal()
    expect(await screen.findByText('+15559998888')).toBeInTheDocument()
  })

  it('empty log explains that PIN failures will show up here', async () => {
    mockLog([])
    renderModal()
    expect(await screen.findByText(/No calls recorded yet/)).toBeInTheDocument()
  })
})
