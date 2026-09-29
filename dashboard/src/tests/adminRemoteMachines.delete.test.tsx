/**
 * Admin → Remote machines, the user-paired section: an admin can remove a
 * user's machine. The row carries a Remove control (icon-only on phones),
 * the confirm names the owner, Cancel sends nothing, and the confirm calls
 * the admin delete (the same self-uninstall cascade as the owner's own
 * Remove).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const USER_MACHINE = {
  id: 'm-user', name: 'Alice laptop', status: 'online', last_seen: null,
  last_heartbeat_age_s: 3, pairing_scope: 'user', registered_by: 'owner-sub',
  owner_display_name: 'Alice', owner_email: 'alice@example.com',
  capabilities: { os: 'linux' }, assigned_agents: [],
}

const h = vi.hoisted(() => ({
  machines: [] as unknown[],
  del: vi.fn(async (_id: string) => {}),
  apiFetch: vi.fn(async (_path: string) => ({
    ok: true,
    json: async () => ({ machines: [], satellites: [], agents: [] }),
  })),
}))

vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))
// The page reads the engine catalog (eligibility, engine chips); the
// apiFetch stub above answers every path with the machines shape, so the
// catalog hook is mocked to an empty catalog here.
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
  useDeleteMachine: () => ({ mutateAsync: h.del, isPending: false }),
}))
vi.mock('@/components/RemoteBadge', () => ({ default: () => null }))

import RemoteMachinesPage from '@/pages/admin/RemoteMachinesPage'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RemoteMachinesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Admin removes a user-paired machine', () => {
  beforeEach(() => {
    h.machines = [USER_MACHINE]
    h.del.mockClear()
  })

  it('shows a Remove control on the user row, confirms with the owner named, and calls the admin delete', async () => {
    mount()
    // Collapsed by default.
    fireEvent.click(screen.getByRole('button', { name: /User-Paired Remote Machines/ }))
    const remove = await screen.findByRole('button', { name: 'Remove Alice laptop' })
    expect(remove.className).toContain('shrink-0')

    fireEvent.click(remove)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/Remove Alice laptop\?/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Alice <alice@example.com>/)).toBeInTheDocument()
    expect(within(dialog).getByText(/uninstall itself/)).toBeInTheDocument()

    // Cancel sends nothing.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(h.del).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Remove Alice laptop' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove machine' }))
    await waitFor(() => expect(h.del).toHaveBeenCalledWith('m-user'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('a failed delete keeps the dialog open with the error', async () => {
    h.del.mockRejectedValueOnce(new Error('Failed to delete machine'))
    mount()
    fireEvent.click(screen.getByRole('button', { name: /User-Paired Remote Machines/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Alice laptop' }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove machine' }))
    expect(await screen.findByText('Failed to delete machine')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})

// The filesystem-access texts: a paired
// machine's own OtoDock folder is refused to every session, and home-only is
// a guardrail, not a sandbox.
describe('Admin machine filesystem-access texts', () => {
  const admin = (id: string, name: string, allow_full_fs: boolean) => ({
    ...USER_MACHINE, id, name, pairing_scope: 'admin', allow_full_fs,
    registered_by: 'admin-sub', owner_display_name: null, owner_email: null,
  })
  beforeEach(() => {
    h.machines = [admin('m-full-0001', 'Office PC', true), admin('m-home-0002', 'Plotter PC', false)]
  })

  it('names the refused folder on a full-access card and the guardrail on a home-only one', () => {
    mount()
    fireEvent.click(screen.getByText('Office PC'))
    expect(screen.getByText(
      "Full filesystem access. Agents can read and write any path the OS user can reach, "
      + "except the machine's own OtoDock folder.")).toBeInTheDocument()
    fireEvent.click(screen.getByText('Plotter PC'))
    expect(screen.getByText(
      "Home folder only. Agents are limited to the agent tree and the OS user's home folder, "
      + 'a guardrail, not a sandbox.')).toBeInTheDocument()
  })

  it('says the same in the pairing dialog', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Pair New Machine' }))
    expect(screen.getByText(
      'When enabled, agents on this machine can read and write any path the OS user can reach '
      + "(system files, services), except the machine's own OtoDock folder, which no session may "
      + "touch. When disabled, agents stay inside the OS user's home folder. Agents run natively "
      + 'as the OS user either way: the home folder setting is a guardrail, not a kernel sandbox, '
      + 'so only pair machines you trust with the agent.')).toBeInTheDocument()
  })
})
