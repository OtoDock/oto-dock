/**
 * Admin → Remote machines, an admin-paired machine's Capacity block: a
 * machine whose satellite watches its memory shows no session limit unless
 * an admin set one, says memory decides, and says when new sessions wait; a
 * Mac or Windows computer says it does not watch its chats' memory; an older
 * satellite keeps its recommended count.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const ADMIN_MACHINE = {
  id: 'm-admin', name: 'Build box', status: 'online', last_seen: null,
  last_heartbeat_age_s: 3, pairing_scope: 'admin', registered_by: 'admin-sub',
  capabilities: { os: 'linux' }, assigned_agents: [], max_sessions: null,
}

const h = vi.hoisted(() => ({
  machines: [] as unknown[],
  satellites: [] as unknown[],
}))

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (_path: string) => ({
    ok: true,
    json: async () => ({ machines: [], satellites: h.satellites, agents: [] }),
  })),
}))
vi.mock('@/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/agents')>()),
  useExecutionLayers: () => ({ data: {} }),
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'admin-sub', agents: [], agent_roles: {} } }),
}))
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/remoteMachines')>()),
  useRemoteMachines: () => ({ data: h.machines, isLoading: false }),
}))
vi.mock('@/components/RemoteBadge', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/RemoteBadge')>()),
  default: () => null,
}))

import RemoteMachinesPage from '@/pages/admin/RemoteMachinesPage'

/** The page with the machine's card opened. */
async function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RemoteMachinesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  fireEvent.click(await screen.findByText('Build box'))
}

function stat(extra: Record<string, unknown>) {
  return { machine_id: 'm-admin', name: 'Build box', online: true, active_sessions: 3,
           max_sessions: null, cpu_pct: 10, mem_pct: 40, ...extra }
}

describe('A machine that watches its memory', () => {
  beforeEach(() => {
    h.machines = [ADMIN_MACHINE]
  })

  it('shows no limit and says memory decides', async () => {
    h.satellites = [stat({ memory_watched: true, mem_total_mb: 16384, mem_free_mb: 9000, low_memory: false })]
    await mount()
    expect(await screen.findByText('Memory decides how many sessions this computer runs. Set a number to cap them.'))
      .toBeTruthy()
    expect(screen.getByPlaceholderText('No limit')).toBeTruthy()
    expect(screen.queryByText('This computer is low on memory. New chats cannot start, and tasks wait.')).toBeNull()
  })

  it('says when new sessions wait', async () => {
    h.satellites = [stat({ memory_watched: true, mem_total_mb: 16384, mem_free_mb: 900, low_memory: true })]
    await mount()
    expect(await screen.findByText('This computer is low on memory. New chats cannot start, and tasks wait.')).toBeTruthy()
  })

  it('keeps an older satellite on its recommended count', async () => {
    h.satellites = [stat({ max_sessions: 6, memory_watched: false })]
    await mount()
    expect(await screen.findByPlaceholderText('Auto (6)')).toBeTruthy()
    expect(screen.queryByText('Memory is not watched on this computer. A command that uses too much is not stopped here.')).toBeNull()
  })

  it('says a Mac does not watch its chats memory', async () => {
    h.satellites = [stat({ max_sessions: 6, memory_watched: false, memory_unwatched: true })]
    await mount()
    expect(await screen.findByText('Memory is not watched on this computer. A command that uses too much is not stopped here.')).toBeTruthy()
  })
})
