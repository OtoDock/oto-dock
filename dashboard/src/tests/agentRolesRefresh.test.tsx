/**
 * Regression tests for the stale `user.agent_roles` snapshot.
 *
 * The auth user is fetched ONCE at app mount (AuthContext), and
 * `user.agent_roles` drives the User Settings → Remote Machines agent list
 * and the per-agent role gates. Creating an agent assigns the creator as
 * its manager server-side — but nothing refreshed the snapshot, so the new
 * agent was missing from the Remote Machines list until a full page reload
 * (the agents grid looked fine because it reads the separate `['agents']`
 * query). Fix: `useCreateAgent` / `useDeleteAgent` /
 * `useInstallCommunityAgent` call `refreshUser()` on success.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
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
import { useCreateAgent } from '@/api/agents'

/** Renders the auth snapshot's agent slugs — the same data source the
 * Remote Machines settings tab maps over — plus a create trigger. */
function Harness() {
  const { user } = useAuth()
  const createAgent = useCreateAgent()
  return (
    <div>
      <div data-testid="roles">
        {Object.keys(user?.agent_roles ?? {}).sort().join(',')}
      </div>
      <button onClick={() => createAgent.mutate({ display_name: 'Beta' })}>
        create
      </button>
    </div>
  )
}

const userBefore = {
  sub: 'u1', email: 'a@example.com', name: 'Alice', role: 'admin',
  agents: ['alpha'], agent_roles: { alpha: 'manager' },
} as any
const userAfter = {
  ...userBefore,
  agents: ['alpha', 'beta'],
  agent_roles: { alpha: 'manager', beta: 'manager' },
}

function renderHarness() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <Harness />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

describe('agent_roles auth-snapshot refresh (Remote Machines stale-list fix)', () => {
  beforeEach(() => {
    h.fetchCurrentUser.mockReset()
    h.apiFetch.mockReset()
  })

  it('useCreateAgent refetches /auth/me so the new agent appears without a reload', async () => {
    h.fetchCurrentUser
      .mockResolvedValueOnce(userBefore) // AuthProvider mount
      .mockResolvedValueOnce(userAfter) // refreshUser after the create
    h.apiFetch.mockResolvedValue({ ok: true, json: async () => ({ name: 'beta' }) })

    renderHarness()
    await waitFor(() =>
      expect(screen.getByTestId('roles')).toHaveTextContent('alpha'))

    fireEvent.click(screen.getByText('create'))
    await waitFor(() =>
      expect(screen.getByTestId('roles')).toHaveTextContent('alpha,beta'))
    expect(h.fetchCurrentUser).toHaveBeenCalledTimes(2)
  })

  it('keeps the existing snapshot when the refetch fails (never logs the user out)', async () => {
    h.fetchCurrentUser
      .mockResolvedValueOnce(userBefore)
      .mockResolvedValueOnce(null) // transient /auth/me failure → null
    h.apiFetch.mockResolvedValue({ ok: true, json: async () => ({ name: 'beta' }) })

    renderHarness()
    await waitFor(() =>
      expect(screen.getByTestId('roles')).toHaveTextContent('alpha'))

    fireEvent.click(screen.getByText('create'))
    await waitFor(() => expect(h.fetchCurrentUser).toHaveBeenCalledTimes(2))
    // Stale-but-present beats logged-out: the snapshot is unchanged.
    expect(screen.getByTestId('roles')).toHaveTextContent('alpha')
  })

  it('does not refetch when the create fails', async () => {
    h.fetchCurrentUser.mockResolvedValueOnce(userBefore)
    h.apiFetch.mockResolvedValue({
      ok: false, json: async () => ({ detail: 'nope' }),
    })

    renderHarness()
    await waitFor(() =>
      expect(screen.getByTestId('roles')).toHaveTextContent('alpha'))

    fireEvent.click(screen.getByText('create'))
    // Give the mutation a tick to settle, then confirm no /auth/me refetch.
    await waitFor(() => expect(h.apiFetch).toHaveBeenCalled())
    expect(h.fetchCurrentUser).toHaveBeenCalledTimes(1)
  })
})
