import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// ─── A Shared-only agent takes editor or manager assignments only: the
//     admin Users page offers no viewer or contributor role there, starts a
//     newly ticked Shared-only agent at editor, and shows a lower row an
//     older install holds as a disabled, marked option. ───────────────────

const h = vi.hoisted(() => ({
  users: [] as any[],
  puts: [] as any[],
  signedOut: [] as string[],
}))

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/v1/admin/users') return new Response(JSON.stringify({ users: h.users }))
    if (url === '/v1/agents?all=true') {
      return new Response(JSON.stringify({ agents: [
        { name: 'office', collaborative: false, default_scope: 'agent' },
        { name: 'site', collaborative: true, default_scope: 'user' },
      ] }))
    }
    if (url.endsWith('/agents') && init?.method === 'PUT') {
      h.puts.push(JSON.parse(String(init.body)))
      return new Response('{}')
    }
    return new Response('{}')
  }),
  fetchAuthConfig: vi.fn(async () => ({})),
  signOutEverywhere: vi.fn(async (sub: string) => { h.signedOut.push(sub); return { sessions_closed: 1 } }),
}))
vi.mock('@/api/executionLayers', () => ({ useSetPlatformAuth: () => ({ mutate: vi.fn(), isPending: false }) }))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { role: 'admin', sub: 'admin', agent_roles: {} }, refreshUser: vi.fn() }),
}))

import UsersPage from '@/pages/admin/UsersPage'

function person(agentRoles: Record<string, string>) {
  return {
    sub: 'pm', email: 'pm@x', name: 'Site PM', role: 'member', agents: Object.keys(agentRoles),
    agent_roles: agentRoles, default_agent: '', allow_platform_auth: 0, created_at: '', last_login: '',
  }
}

async function openEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><UsersPage /></QueryClientProvider>)
  const edit = await screen.findAllByRole('button', { name: 'Edit' })
  fireEvent.click(edit[0])
  await screen.findAllByText('office')
}

function roleSelectOf(agent: string): HTMLSelectElement {
  const label = screen.getAllByText(agent).map((el) => el.closest('label')).find(Boolean)!
  return within(label as HTMLElement).getByRole('combobox') as HTMLSelectElement
}

describe('Users page on a Shared-only agent', () => {
  beforeEach(() => { h.puts = [] })

  it('offers editor and manager only, and starts a ticked Shared-only agent at editor', async () => {
    h.users = [person({ site: 'viewer' })]
    await openEditor()
    const officeBox = screen.getAllByText('office').map((el) => el.closest('label')).find(Boolean)!
    fireEvent.click(within(officeBox as HTMLElement).getByRole('checkbox'))
    const select = roleSelectOf('office')
    expect(select.value).toBe('editor')
    const values = Array.from(select.options).map((o) => o.value)
    expect(values).toEqual(['editor', 'manager'])
    // The collaborative agent still offers every role.
    expect(Array.from(roleSelectOf('site').options).map((o) => o.value))
      .toEqual(['viewer', 'contributor', 'editor', 'manager'])
    fireEvent.click(screen.getByRole('button', { name: /Save/ }))
    await waitFor(() => expect(h.puts).toHaveLength(1))
    expect(h.puts[0].agent_roles).toEqual({ site: 'viewer', office: 'editor' })
  })

  it('shows a lower row an older install holds as a disabled option', async () => {
    h.users = [person({ office: 'contributor' })]
    await openEditor()
    const select = roleSelectOf('office')
    expect(select.value).toBe('contributor')
    const legacy = Array.from(select.options).find((o) => o.value === 'contributor')!
    expect(legacy.disabled).toBe(true)
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['contributor', 'editor', 'manager'])
  })

  it('signs a person out of every device after a confirm', async () => {
    h.users = [person({ site: 'viewer' })]
    h.signedOut = []
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    await openEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Sign Out Everywhere' }))
    await waitFor(() => expect(h.signedOut).toEqual(['pm']))
    expect(confirm).toHaveBeenCalledTimes(1)
    confirm.mockRestore()
  })
})
