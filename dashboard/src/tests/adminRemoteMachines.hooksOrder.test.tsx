/**
 * Admin → Remote machines renders every hook before its "not in this build"
 * early return: a user snapshot that flips the feature flag between two
 * renders (a refresh of /auth/me) must not change how many hooks ran, which
 * React answers with "Rendered fewer hooks than expected" and a blank page.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const h = vi.hoisted(() => ({
  flags: {} as Record<string, boolean>,
  apiFetch: vi.fn(async (_path: string) => ({
    ok: true,
    json: async () => ({ machines: [], satellites: [], agents: [] }),
  })),
}))

vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
// A real hook inside the stub: the catalog query is one hook call, and the
// order check only sees calls that reach React.
vi.mock('@/api/agents', async (importOriginal) => {
  const { useState } = await import('react')
  return {
    ...(await importOriginal<typeof import('@/api/agents')>()),
    useExecutionLayers: () => { useState(0); return { data: {} } },
  }
})
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'admin-sub', agents: [], agent_roles: {}, feature_flags: h.flags } }),
}))
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/remoteMachines')>()),
  useRemoteMachines: () => ({ data: [], isLoading: false }),
}))
vi.mock('@/components/RemoteBadge', () => ({ default: () => null }))

import RemoteMachinesPage from '@/pages/admin/RemoteMachinesPage'

function page() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RemoteMachinesPage />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('Admin → Remote machines hook order', () => {
  it('survives the feature flag flipping between renders, both ways', () => {
    h.flags = {}
    const { rerender } = render(page())
    expect(screen.queryByText('Remote-machine support is not included in this build.')).toBeNull()
    h.flags = { remote_machines_available: false }
    rerender(page())
    expect(screen.getByText('Remote-machine support is not included in this build.')).toBeTruthy()
    h.flags = {}
    rerender(page())
    expect(screen.queryByText('Remote-machine support is not included in this build.')).toBeNull()
  })
})
