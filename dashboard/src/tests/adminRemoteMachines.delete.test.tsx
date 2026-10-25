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
import { findAlertDialog, personClick, renderWithDialogs } from './dialogsHarness'

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

/** The text behind every info button named `About <label>`, each opened
 *  and closed again by a keyboard click. */
function infoTexts(label: string): string[] {
  return screen.getAllByRole('button', { name: `About ${label}` }).map((button) => {
    fireEvent.click(button, { detail: 0 })
    const text = screen.getByRole('tooltip').textContent ?? ''
    fireEvent.click(button, { detail: 0 })
    return text
  })
}

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
    const dialog = screen.getByRole('alertdialog')
    expect(within(dialog).getByText(/Remove Alice laptop\?/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Alice <alice@example.com>/)).toBeInTheDocument()
    expect(within(dialog).getByText(/uninstalls itself/)).toBeInTheDocument()

    // Cancel sends nothing.
    personClick(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(h.del).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Remove Alice laptop' }))
    personClick(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Remove machine' }))
    await waitFor(() => expect(h.del).toHaveBeenCalledWith('m-user'))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  })

  it('a failed delete keeps the dialog open with the error', async () => {
    h.del.mockRejectedValueOnce(new Error('Failed to delete machine'))
    mount()
    fireEvent.click(screen.getByRole('button', { name: /User-Paired Remote Machines/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove Alice laptop' }))
    personClick(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Remove machine' }))
    expect(await screen.findByText('Failed to delete machine')).toBeInTheDocument()
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
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
      'Full filesystem access. Agents can read and write any path the OS user can reach.')).toBeInTheDocument()
    expect(infoTexts('Filesystem access')).toEqual(["The machine's own OtoDock folder stays off limits."])
    fireEvent.click(screen.getByText('Plotter PC'))
    expect(screen.getByText('Home folder only. This is a guardrail, not full isolation.')).toBeInTheDocument()
    expect(infoTexts('Filesystem access')).toEqual(["Agents stay in their agent folders and the OS user's home folder."])
  })

  it('says the same in the pairing dialog', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Pair New Machine' }))
    expect(screen.getByText(
      'The home folder limit is a guardrail, not full isolation. Only pair '
      + 'machines you trust with the agent.')).toBeInTheDocument()
    expect(infoTexts('Allow full filesystem access')).toEqual([
      'When on, agents can read and write any path the OS user can reach. '
      + "That includes system files and services. The machine's own OtoDock folder stays off "
      + "limits. When off, agents stay inside the OS user's home folder. Either way, agents run "
      + 'as the OS user.'])
  })
})

// The device grant confirm says what the grant lets the assigned agents do.
describe('Admin machine device grant confirm', () => {
  beforeEach(() => {
    h.machines = [{
      ...USER_MACHINE, id: 'm-studio', name: 'Studio PC', pairing_scope: 'admin', allow_full_fs: false,
      device_grants: [], registered_by: 'admin-sub', owner_display_name: null, owner_email: null,
    }]
    h.apiFetch.mockClear()
  })

  it('names App connectors for the assigned agents, and Cancel grants nothing', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <RemoteMachinesPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByText('Studio PC'))
    expect(infoTexts('App connectors')).toContain('Work in the applications on this computer')
    fireEvent.click(screen.getByRole('checkbox', { name: 'App connectors' }))
    const dialog = await findAlertDialog('Grant "App connectors" on Studio PC?')
    expect(within(dialog).getByText(
      'Assigned agents can work in the applications on this computer that have a connector, like Blender. '
      + 'They can run code inside those applications. Turn it on only on a computer you trust them with.',
    )).toBeInTheDocument()
    personClick(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(h.apiFetch.mock.calls.some(([path]) => String(path).includes('device-grants'))).toBe(false)
  })
})
