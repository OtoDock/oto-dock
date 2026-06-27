import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// The REAL useRemoteMachines runs against a spied transport — the regression
// being pinned is "a non-admin agents-page render performs NO /v1/admin/*
// request". AgentCard renders for every role, and an unconditional 15s poll
// of the admin machines endpoint produced a steady 403 stream that a network
// IDS/IPS matched as scanning and answered by blocking the client's flow.
const h = vi.hoisted(() => ({
  role: 'member',
  apiFetch: vi.fn(async (_path: string) => ({
    json: async () => ({ machines: [] }),
  })),
}))

vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: h.role, sub: 'u1' } }),
}))

import AgentCard from '@/components/AgentCard'
import type { AgentSummary } from '@/api/agents'

// execution_target set → AgentCard would look the machine up if it could.
const agent = {
  name: 'demo',
  display_name: 'Demo',
  description: '',
  color: '#336699',
  collaborative: true,
  default_scope: 'user',
  execution_target: 'machine-1',
  mcp_count: 0,
  schedule_count: 0,
  trigger_count: 0,
} as unknown as AgentSummary

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AgentCard agent={agent} isDefault={false} onSetDefault={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('useRemoteMachines role gate (agents page)', () => {
  beforeEach(() => {
    h.apiFetch.mockClear()
  })

  it('non-admin render performs no /v1/admin/* requests', async () => {
    h.role = 'creator'
    renderCard()
    // Give react-query a tick to fire anything it was going to fire.
    await new Promise((r) => setTimeout(r, 50))
    const adminCalls = h.apiFetch.mock.calls
      .filter(([path]) => String(path).startsWith('/v1/admin/'))
    expect(adminCalls).toEqual([])
  })

  it('admin render does query the machines endpoint (positive control)', async () => {
    h.role = 'admin'
    renderCard()
    await waitFor(() =>
      expect(h.apiFetch).toHaveBeenCalledWith('/v1/admin/remote-machines'))
  })
})
