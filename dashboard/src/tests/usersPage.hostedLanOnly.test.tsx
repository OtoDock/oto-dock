import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderWithDialogs } from './dialogsHarness'

// ─── A hosted install has no local network: the Users page offers no
//     "LAN only" switch there, except on an account that still has it on,
//     so an admin can turn it off. ───────────────────────────────────────

const h = vi.hoisted(() => ({ users: [] as any[], cloud: true }))

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url === '/v1/admin/users') return new Response(JSON.stringify({ users: h.users }))
    if (url === '/v1/agents?all=true') return new Response(JSON.stringify({ agents: [] }))
    return new Response('{}')
  }),
  fetchAuthConfig: vi.fn(async () => ({ cloud: h.cloud })),
  signOutEverywhere: vi.fn(),
}))
vi.mock('@/api/executionLayers', () => ({ useSetPlatformAuth: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()), isPending: false }) }))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'admin', agent_roles: {} }, refreshUser: vi.fn() }),
}))

import UsersPage from '@/pages/admin/UsersPage'

function account(localOnly: number) {
  return { sub: 'p', email: 'p@x', name: 'Pat', role: 'member', agents: [], agent_roles: {},
    default_agent: '', allow_platform_auth: 0, created_at: '', last_login: '', auth_provider: 'local',
    local_only: localOnly }
}

async function openEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  renderWithDialogs(<QueryClientProvider client={qc}><UsersPage /></QueryClientProvider>)
  fireEvent.click((await screen.findAllByRole('button', { name: 'Edit' }))[0])
  await screen.findByText('Platform API', { selector: 'span' })
}

describe('Users page on a hosted install', () => {
  it('offers no LAN only switch', async () => {
    h.users = [account(0)]
    h.cloud = true
    await openEditor()
    expect(screen.queryByText('LAN only', { selector: 'span' })).toBeNull()
  })

  it('keeps it on an account that still has it, to turn it off', async () => {
    h.users = [account(1)]
    h.cloud = true
    await openEditor()
    expect(screen.getAllByText('LAN only').length).toBeGreaterThan(0)
    expect(screen.getByRole('switch', { name: 'LAN only' })).toBeInTheDocument()
  })

  it('offers it on a self-hosted install', async () => {
    h.users = [account(0)]
    h.cloud = false
    await openEditor()
    expect(screen.getByRole('switch', { name: 'LAN only' })).toBeInTheDocument()
  })
})
