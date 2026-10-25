import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { answerDialog, renderWithDialogs } from './dialogsHarness'

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
    if (url.endsWith('/reset-password')) return new Response(JSON.stringify({ temp_password: 'tmp-pass-42' }))
    if (url.endsWith('/agents') && init?.method === 'PUT') {
      h.puts.push(JSON.parse(String(init.body)))
      return new Response('{}')
    }
    return new Response('{}')
  }),
  fetchAuthConfig: vi.fn(async () => ({})),
  signOutEverywhere: vi.fn(async (sub: string) => { h.signedOut.push(sub); return { sessions_closed: 1 } }),
}))
vi.mock('@/api/executionLayers', () => ({ useSetPlatformAuth: () => ({ mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()), isPending: false }) }))
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
  renderWithDialogs(<QueryClientProvider client={qc}><UsersPage /></QueryClientProvider>)
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
    await openEditor()
    fireEvent.click(screen.getByRole('button', { name: 'Sign Out Everywhere' }))
    expect(h.signedOut).toEqual([])
    await act(async () => { await answerDialog('Sign out everywhere', /out of every device\?/) })
    await waitFor(() => expect(h.signedOut).toEqual(['pm']))
  })
})

// ─── A reset password shows in the card of the user it was reset for, not
//     at the top of the page above a long list ───

describe('Users page: the one-time password after a reset', () => {
  it("shows inside that user's card, under the Account actions, Copy focused", async () => {
    h.users = [
      { ...person({ site: 'viewer' }), sub: 'a', name: 'Alice', email: 'a@x' },
      { ...person({ site: 'viewer' }), sub: 'b', name: 'Bob', email: 'b@x' },
      { ...person({ site: 'viewer' }), sub: 'c', name: 'Carol', email: 'c@x' },
    ]
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(<QueryClientProvider client={qc}><UsersPage /></QueryClientProvider>)
    const edits = await screen.findAllByRole('button', { name: 'Edit' })
    fireEvent.click(edits[1])
    fireEvent.click(await screen.findByRole('button', { name: 'Reset Password' }))
    await act(async () => { await answerDialog('Reset password', /Reset the password of b@x\?/) })
    const result = await screen.findByTestId('reset-password-result')
    expect(result).toHaveTextContent('tmp-pass-42')
    const follows = (a: Node, b: Node) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
    // Under the Account actions, where Reset Password was pressed.
    expect(follows(screen.getByRole('button', { name: 'Reset Password' }), result)).toBe(true)
    expect(follows(screen.getByText('Bob'), result)).toBe(true)
    expect(follows(result, screen.getByText('Carol'))).toBe(true)
    expect(within(result).getByRole('button', { name: 'Copy' })).toHaveFocus()
    // Dismiss takes its own button away: the focus goes back to Reset Password.
    fireEvent.click(within(result).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('reset-password-result')).toBeNull()
    expect(screen.getByRole('button', { name: 'Reset Password' })).toHaveFocus()
  })
})
