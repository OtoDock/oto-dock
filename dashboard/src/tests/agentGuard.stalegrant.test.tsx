/**
 * Regression tests for AgentGuard vs the stale `user.agents` snapshot
 * (grant direction).
 *
 * The auth user is fetched ONCE at app mount; an admin granting a member a
 * NEW agent from ANOTHER session updates the DB (so the live `['agents']`
 * query — the grid — shows the card) but nothing refreshes the member's
 * snapshot, so the guard's `user.agents.includes(name)` bounced the click
 * back to /agents until a full reload. Fix: the guard consults the live
 * list (for non-admins it only ever contains accessible agents) before
 * bouncing, and heals the snapshot with one refreshUser() so the manage UI
 * (driven by the equally stale `user.agent_roles`) appears too.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const h = vi.hoisted(() => ({
  fetchCurrentUser: vi.fn(),
  apiFetch: vi.fn(),
}))

vi.mock('@/api/auth', async (importOriginal) => {
  const mod = await importOriginal<any>()
  return {
    ...mod,
    fetchAuthConfig: vi.fn(async () => ({})),
    fetchCurrentUser: h.fetchCurrentUser,
    apiFetch: h.apiFetch,
    startLogin: vi.fn(),
    logout: vi.fn(),
  }
})

import { AuthProvider, useAuth } from '@/contexts/AuthContext'
import AgentGuard from '@/components/AgentGuard'

/** The real routes sit behind RequireAuth, which holds rendering while the
 * auth snapshot loads — the guard never sees a null-because-loading user.
 * Mirror that gate, or every case bounces before /auth/me resolves. */
function AuthGate({ children }: { children: React.ReactNode }) {
  const { loading } = useAuth()
  return loading ? null : <>{children}</>
}

const member = {
  sub: 'u1', email: 'm@example.com', name: 'Mel', role: 'member',
  agents: ['alpha'], agent_roles: { alpha: 'viewer' },
} as any
const memberHealed = {
  ...member,
  agents: ['alpha', 'oto'], agent_roles: { alpha: 'viewer', oto: 'manager' },
}
const admin = { ...member, sub: 'u2', role: 'admin' }

/** apiFetch mock serving /v1/agents (and ?all=true) with the given slugs. */
function serveAgents(slugs: string[] | 'error') {
  h.apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/v1/agents')) {
      if (slugs === 'error') throw new Error('api down')
      return { ok: true, json: async () => ({ agents: slugs.map(name => ({ name })) }) }
    }
    return { ok: true, json: async () => ({}) }
  })
}

function renderGuardAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <AuthGate>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/agents" element={<div data-testid="agents-grid" />} />
              <Route path="chat/:name" element={<AgentGuard />}>
                <Route index element={<div data-testid="agent-chat" />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </AuthGate>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe('AgentGuard vs stale user.agents (grant without reload)', () => {
  beforeEach(() => {
    h.fetchCurrentUser.mockReset()
    h.apiFetch.mockReset()
  })

  it('member + freshly granted agent (live list only) → enters + one snapshot heal', async () => {
    h.fetchCurrentUser
      .mockResolvedValueOnce(member) // AuthProvider mount (stale snapshot)
      .mockResolvedValue(memberHealed) // the guard's refreshUser heal
    serveAgents(['alpha', 'oto'])

    renderGuardAt('/chat/oto')
    await waitFor(() => expect(screen.getByTestId('agent-chat')).toBeInTheDocument())
    await waitFor(() => expect(h.fetchCurrentUser).toHaveBeenCalledTimes(2))
  })

  it('member + agent in neither source → bounced to /agents (no heal)', async () => {
    h.fetchCurrentUser.mockResolvedValue(member)
    serveAgents(['alpha'])

    renderGuardAt('/chat/ghost')
    await waitFor(() => expect(screen.getByTestId('agents-grid')).toBeInTheDocument())
    expect(h.fetchCurrentUser).toHaveBeenCalledTimes(1) // mount only — no /auth/me spam
  })

  it('member + agent in the snapshot → enters without a heal (unchanged fast path)', async () => {
    h.fetchCurrentUser.mockResolvedValue(member)
    serveAgents(['alpha'])

    renderGuardAt('/chat/alpha')
    await waitFor(() => expect(screen.getByTestId('agent-chat')).toBeInTheDocument())
    expect(h.fetchCurrentUser).toHaveBeenCalledTimes(1)
  })

  it('admin → enters via the live list, snapshot never consulted', async () => {
    h.fetchCurrentUser.mockResolvedValue(admin)
    serveAgents(['alpha', 'oto'])

    renderGuardAt('/chat/oto')
    await waitFor(() => expect(screen.getByTestId('agent-chat')).toBeInTheDocument())
    expect(h.fetchCurrentUser).toHaveBeenCalledTimes(1)
    // Admin list is the all=true query.
    expect(h.apiFetch).toHaveBeenCalledWith('/v1/agents?all=true')
  })

  it('live query ERROR + slug missing from snapshot → instant bounce, no blank wait', async () => {
    h.fetchCurrentUser.mockResolvedValue(member)
    serveAgents('error')

    renderGuardAt('/chat/oto')
    await waitFor(() => expect(screen.getByTestId('agents-grid')).toBeInTheDocument())
  })

  it('heal resolving WITHOUT the slug does not refire (latch)', async () => {
    // /auth/me keeps returning the STALE snapshot (grant revoked mid-flight):
    // the guard must not hammer /auth/me on every re-render.
    h.fetchCurrentUser.mockResolvedValue(member)
    serveAgents(['alpha', 'oto'])

    renderGuardAt('/chat/oto')
    await waitFor(() => expect(screen.getByTestId('agent-chat')).toBeInTheDocument())
    await waitFor(() => expect(h.fetchCurrentUser).toHaveBeenCalledTimes(2))
    // Give any would-be loop a tick to fire, then confirm it didn't.
    await new Promise(r => setTimeout(r, 50))
    expect(h.fetchCurrentUser).toHaveBeenCalledTimes(2)
  })
})
