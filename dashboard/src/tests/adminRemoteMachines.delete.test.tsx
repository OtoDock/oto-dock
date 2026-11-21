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
vi.mock('@/components/RemoteBadge', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/RemoteBadge')>()),
  default: () => null,
}))

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

// A machine whose local link is down (its sessions cannot reach OtoDock):
// the row says so, and the expanded card names the satellite's reason.
describe('Admin machine whose local link is down', () => {
  it('says the satellite is restoring it, then to restart it, with the reason inside', () => {
    h.machines = [
      { ...USER_MACHINE, id: 'm-adm-0001', name: 'Office PC', pairing_scope: 'admin',
        registered_by: 'admin-sub', link_ok: false, link_down_s: 12,
        link_error: 'connection refused at 127.0.0.1:57353' },
      { ...USER_MACHINE, id: 'm-adm-0002', name: 'Plotter PC', pairing_scope: 'admin',
        registered_by: 'admin-sub', link_ok: false, link_down_s: 300, link_error: '' },
      { ...USER_MACHINE, link_ok: false, link_down_s: 5 },
    ]
    mount()
    expect(screen.getByText(
      'Its sessions cannot reach OtoDock. The satellite is restoring the link.')).toBeInTheDocument()
    expect(screen.getByText(
      'Its sessions cannot reach OtoDock. Restart the satellite on that machine.')).toBeInTheDocument()
    expect(screen.queryByText(/Last problem/)).toBeNull()
    fireEvent.click(screen.getByText('Office PC'))
    expect(screen.getByText('Last problem: connection refused at 127.0.0.1:57353')).toBeInTheDocument()
    // The user-paired row says it too.
    fireEvent.click(screen.getByRole('button', { name: /User-Paired Remote Machines/ }))
    expect(screen.getAllByTestId('machine-link-notice')).toHaveLength(3)
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

// "Every role of its agents runs here": off by default, turned on behind a
// confirm that says what the other roles' chats reach, turned off at once.
describe('Admin machine: every role of its agents runs here', () => {
  const studio = (on: boolean) => ({
    ...USER_MACHINE, id: 'm-studio', name: 'Studio PC', pairing_scope: 'admin', allow_full_fs: false,
    device_grants: [], all_roles_run_here: on, registered_by: 'admin-sub',
    owner_display_name: null, owner_email: null,
  })
  // The file's fetch stub is typed with the path alone: read the init too.
  const puts = () => (h.apiFetch.mock.calls as unknown as [string, RequestInit | undefined][])
    .filter(([path]) => path.endsWith('/all-roles'))
    .map(([path, init]) => [path, init?.body])

  function mountWithDialogs() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderWithDialogs(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <RemoteMachinesPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  beforeEach(() => { h.apiFetch.mockClear() })

  it('asks before it lets every role run there, and Cancel changes nothing', async () => {
    h.machines = [studio(false)]
    mountWithDialogs()
    fireEvent.click(screen.getByText('Studio PC'))
    expect(screen.getByText('Who runs here')).toBeInTheDocument()
    expect(screen.getByText(
      "On, the chats of editors, contributors and viewers run on this computer, with the machine's own access.",
    )).toBeInTheDocument()
    expect(infoTexts('Every role of its agents runs here')).toEqual([
      'Off, their chats run on the OtoDock platform. Managers and admins run here either way. '
      + 'An outside caller always runs on the platform.'])
    const box = screen.getByRole('checkbox', { name: 'Every role of its agents runs here' })
    expect(box).not.toBeChecked()

    fireEvent.click(box)
    let dialog = await findAlertDialog('Let every role run on Studio PC?')
    expect(within(dialog).getByText(
      "Chats of editors, contributors and viewers will run on this computer. They can reach every file this "
      + "computer's user can. They also get its browser and computer control, and the accounts their chats use.",
    )).toBeInTheDocument()
    personClick(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(puts()).toEqual([])

    fireEvent.click(screen.getByRole('checkbox', { name: 'Every role of its agents runs here' }))
    dialog = await findAlertDialog('Let every role run on Studio PC?')
    personClick(within(dialog).getByRole('button', { name: 'Allow' }))
    await waitFor(() => expect(puts()).toEqual([
      ['/v1/admin/remote-machines/m-studio/all-roles', JSON.stringify({ enabled: true })]]))
  })

  it('turns it off with no question', async () => {
    h.machines = [studio(true)]
    mountWithDialogs()
    fireEvent.click(screen.getByText('Studio PC'))
    const box = screen.getByRole('checkbox', { name: 'Every role of its agents runs here' })
    expect(box).toBeChecked()
    fireEvent.click(box)
    await waitFor(() => expect(puts()).toEqual([
      ['/v1/admin/remote-machines/m-studio/all-roles', JSON.stringify({ enabled: false })]]))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})

// A compute machine: the platform's own, owned by nobody. It has its own
// section, no settings, no agents and no owner, and its own remove confirm.
describe('Admin compute machines', () => {
  const COMPUTE = {
    ...USER_MACHINE, id: 'c-compute-1', name: 'Compute one', status: 'never_connected',
    pairing_scope: 'compute', registered_by: '', owner_display_name: '', owner_email: '',
  }
  const ADMIN = { ...USER_MACHINE, id: 'm-adm-0009', name: 'Office PC', pairing_scope: 'admin', registered_by: 'admin-sub' }
  beforeEach(() => {
    h.machines = [ADMIN, COMPUTE, USER_MACHINE]
    h.del.mockClear()
  })

  it('lists a compute machine apart, plainly, with nothing to set', () => {
    mount()
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('Compute one')).toBeInTheDocument()
    expect(within(section).getByText('Machines the platform runs itself.')).toBeInTheDocument()
    fireEvent.click(within(section).getByRole('button', { name: 'About Compute machines' }), { detail: 0 })
    expect(screen.getByRole('tooltip')).toHaveTextContent(
      'No person owns them. Chats, terminals, tasks, calls and meetings on the platform run on them. '
      + 'Apps run on the one beside the platform, never on an added server. '
      + 'While none is online, these cannot run. Chats on a remote machine still can.')
    expect(within(section).getByText('Never connected')).toBeInTheDocument()
    expect(within(section).queryByText('Not in use')).toBeNull()
    // With none online the card says what that means.
    expect(within(section).getByTestId('compute-none-online')).toHaveTextContent(
      'No compute machine is online, so chats, tasks and calls cannot run.')
    // An added machine that never signed in says so in place of its id.
    expect(within(section).getByText('Waiting for its first sign-in')).toBeInTheDocument()
    expect(within(section).queryByText(/Owner/)).toBeNull()
    expect(within(section).queryByText('never_connected')).toBeNull()
    // Not among the admin's machines, whose cards hold the settings.
    expect(screen.getAllByText('Compute one')).toHaveLength(1)
    expect(screen.getByText('Office PC')).toBeInTheDocument()
  })

  it('removes one after its own confirm, which names no uninstall', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Remove Compute one' }))
    const dialog = screen.getByRole('alertdialog')
    expect(within(dialog).getByText(/A chat running on it stops\./)).toBeInTheDocument()
    expect(within(dialog).queryByText(/uninstalls itself/)).toBeNull()
    personClick(within(dialog).getByRole('button', { name: 'Remove machine' }))
    await waitFor(() => expect(h.del).toHaveBeenCalledWith('c-compute-1'))
  })

  it('shows each machine\'s state word, what runs there and the stop on remove', () => {
    h.machines = [ADMIN, { ...COMPUTE, status: 'stale' },
                  { ...COMPUTE, id: 'c-compute-2', name: 'Compute two', status: 'never_connected' }]
    mount()
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('Slow connection')).toBeInTheDocument()
    expect(within(section).getByText('Never connected')).toBeInTheDocument()
    expect(within(section).queryByText('Not in use')).toBeNull()
    fireEvent.click(within(section).getByRole('button', { name: 'About Compute machines' }), { detail: 0 })
    expect(screen.getByRole('tooltip')).toHaveTextContent(
      'No person owns them. Chats, terminals, tasks, calls and meetings on the platform run on them. '
      + 'Apps run on the one beside the platform, never on an added server. '
      + 'While none is online, these cannot run. Chats on a remote machine still can.')
    fireEvent.click(screen.getByRole('button', { name: 'Remove Compute one' }))
    expect(within(screen.getByRole('alertdialog')).getByText(
      'A chat running on it stops. Its next message runs on another compute machine and keeps the conversation. '
      + 'If the server connects within 30 days, OtoDock removes itself from it.')).toBeInTheDocument()
  })

  it('shows what the compute machine runs, its memory, and work held for an update', () => {
    h.machines = [ADMIN, {
      ...COMPUTE, status: 'online',
      compute: { own: true, sessions: 3, holding: true,
                 memory: { total_mb: 16384, available_mb: 9830, reserved_mb: 2550, capped: false } },
    }]
    mount()
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('3 sessions, 9.6 GB free of 16 GB')).toBeInTheDocument()
    expect(within(section).getByText('Online')).toBeInTheDocument()
    expect(within(section).queryByTestId('compute-none-online')).toBeNull()
    expect(within(section).getByTestId('compute-holding')).toHaveTextContent(
      'New chats wait while OtoDock updates, restarts or makes a backup.')
    // The install keeps its own compute machine: no Remove.
    expect(within(section).queryByRole('button', { name: 'Remove Compute one' })).toBeNull()
  })

  it('says one session and leaves memory out before the first report', () => {
    h.machines = [ADMIN, {
      ...COMPUTE, status: 'online', compute_address: '192.168.50.7',
      last_seen: '2026-11-11T03:00:00Z',
      compute: { own: false, sessions: 1, holding: false, memory: null },
    }]
    mount()
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('On another server, last signed in from 192.168.50.7, 1 session')).toBeInTheDocument()
    expect(within(section).queryByTestId('compute-holding')).toBeNull()
    expect(within(section).getByRole('button', { name: 'Remove Compute one' })).toBeInTheDocument()
  })

  it('shows no section when there is no compute machine', () => {
    h.machines = [ADMIN, USER_MACHINE]
    mount()
    expect(screen.queryByTestId('compute-machines')).toBeNull()
  })
})
