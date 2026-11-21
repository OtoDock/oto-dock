/**
 * Admin → Remote machines, an admin-paired machine's Updates block: a
 * machine on the newest version says so and offers no button; a running or
 * queued update disables it with one sentence; a press that stops sessions
 * asks first, with the card's own count.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const MACHINE = {
  id: 'm-admin', name: 'Build box', status: 'online', last_seen: null,
  last_heartbeat_age_s: 3, pairing_scope: 'admin', registered_by: 'admin-sub',
  capabilities: { os: 'linux' }, assigned_agents: [], max_sessions: null,
  satellite_version: '0.5.185', auto_update_enabled: true,
}

const h = vi.hoisted(() => ({
  machines: [] as unknown[],
  satellites: [] as unknown[],
  presses: [] as string[],
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
  useTriggerUpdateNow: () => ({
    isPending: false,
    mutateAsync: async (id: string) => { h.presses.push(id) },
  }),
}))
vi.mock('@/components/RemoteBadge', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/RemoteBadge')>()),
  default: () => null,
}))

import RemoteMachinesPage from '@/pages/admin/RemoteMachinesPage'

/** `sessions`: the machine's reported count, `'unknown'` for a machine
 *  that has not reported one since it signed in, null for no stat at all. */
async function mount(machine: Record<string, unknown>, sessions: number | 'unknown' | null = 0) {
  h.machines = [{ ...MACHINE, ...machine }]
  h.satellites = sessions == null ? [] : [{
    machine_id: 'm-admin', name: 'Build box', online: true,
    active_sessions: sessions === 'unknown' ? null : sessions,
    max_sessions: 6, cpu_pct: 10, mem_pct: 40,
  }]
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = () => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <RemoteMachinesPage />
      </MemoryRouter>
    </QueryClientProvider>
  )
  const view = render(tree())
  fireEvent.click(await screen.findByText('Build box'))
  await screen.findByText(/Satellite version/)
  if (sessions != null) await screen.findByText(`${sessions === 'unknown' ? '—' : sessions} / 6`)
  /** The page again after the rows changed (the next poll). */
  return (machine: Record<string, unknown>) => {
    h.machines = [{ ...MACHINE, ...machine }]
    view.rerender(tree())
  }
}

const button = () => screen.queryByRole('button', { name: 'Update now' })

describe('The Updates block of a machine card', () => {
  beforeEach(() => { h.presses = [] })

  it('says a machine on the newest version is up to date and offers no button', async () => {
    await mount({ update_state: 'current', pending_update: true })
    expect(screen.getByTestId('update-state').textContent).toBe('Up to date')
    expect(button()).toBeNull()
    expect(screen.queryByText('Update queued')).toBeNull()
  })

  it('offers the button when an update is available', async () => {
    await mount({ update_state: 'available' })
    expect(button()).toBeTruthy()
    expect((button() as HTMLButtonElement).disabled).toBe(false)
    expect(screen.queryByTestId('update-state')).toBeNull()
  })

  it('disables the button while the update runs and says so', async () => {
    await mount({ update_state: 'running' })
    expect(screen.getByTestId('update-state').textContent).toBe('Updating')
    expect((button() as HTMLButtonElement).disabled).toBe(true)
    expect((button() as HTMLButtonElement).title).toBe('')
    expect(screen.getByText('This machine is updating. Chats that were working continue by themselves when it is back.')).toBeTruthy()
  })

  it('disables the button while an update is queued', async () => {
    await mount({ update_state: 'queued', status: 'disconnected', pending_update: true }, null)
    expect(screen.getByTestId('update-state').textContent).toBe('Update queued')
    expect((button() as HTMLButtonElement).disabled).toBe(true)
    expect((button() as HTMLButtonElement).title).toBe('')
    expect(screen.getByText('The update runs when this machine connects.')).toBeTruthy()
  })

  it('says an update waits for an idle moment and keeps the button', async () => {
    await mount({ update_state: 'waiting' })
    expect(screen.getByText('The update starts when nothing runs on this machine.')).toBeTruthy()
    expect((button() as HTMLButtonElement).disabled).toBe(false)
  })

  it('shows a machine its version keeps out as Update needed, with Update now', async () => {
    await mount({ update_state: 'refused', status: 'disconnected' }, null)
    expect(screen.getByTestId('update-state').textContent).toContain('Update needed')
    expect((button() as HTMLButtonElement).disabled).toBe(false)
  })

  it('sends a plaintext link to the installer with no button', async () => {
    await mount({ update_state: 'manual' })
    expect(button()).toBeNull()
    expect(screen.getByText('Run the installer on this machine again to update it.')).toBeTruthy()
  })

  it('keeps Update now in the paused case after rollbacks', async () => {
    await mount({ update_state: 'available', update_rollback_count: 2, update_rollback_target: '0.5.186' })
    expect(screen.getByTestId('auto-update-paused')).toBeTruthy()
    expect((button() as HTMLButtonElement).disabled).toBe(false)
  })

  it('drops the button sentence from the auto-update note when it cannot be pressed', async () => {
    await mount({ update_state: 'current', auto_update_enabled: false })
    expect(screen.getByText(/Auto-update is off/).textContent).not.toContain('Update now')
  })

  it('asks before a press that stops sessions, with their count', async () => {
    await mount({ update_state: 'available' }, 3)
    fireEvent.click(button() as HTMLButtonElement)
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText('Update Build box now?')).toBeTruthy()
    expect(within(dialog).getByText(
      'This machine runs 3 sessions now. The update stops them. Chats that are working continue by themselves after it. Idle chats start again at their next message.')).toBeTruthy()
    expect(h.presses).toEqual([])
    // A real press: the confirm refuses a click no pointer pressed.
    const confirm = within(dialog).getByRole('button', { name: 'Update now' })
    fireEvent.pointerDown(confirm)
    fireEvent.click(confirm, { detail: 1 })
    expect(h.presses).toEqual(['m-admin'])
  })

  it('says one session in the singular', async () => {
    await mount({ update_state: 'available' }, 1)
    fireEvent.click(button() as HTMLButtonElement)
    expect(await screen.findByText(
      'This machine runs 1 session now. The update stops it. If its chat is working, it continues by itself after the update. An idle chat starts again at its next message.')).toBeTruthy()
  })

  it('asks without a count when none is known', async () => {
    await mount({ update_state: 'available' }, null)
    fireEvent.click(button() as HTMLButtonElement)
    expect(await screen.findByText(
      'The update stops the sessions on this machine. Chats that are working continue by themselves after it. Idle chats start again at their next message.')).toBeTruthy()
    expect(h.presses).toEqual([])
  })

  it('closes the confirm when a press is no longer possible', async () => {
    const poll = await mount({ update_state: 'available' }, 3)
    fireEvent.click(button() as HTMLButtonElement)
    expect(await screen.findByRole('alertdialog')).toBeTruthy()
    poll({ update_state: 'running' })
    expect(screen.queryByRole('alertdialog')).toBeNull()
    // The request ended with its condition: a press possible again (the
    // update rolled back) does not bring the confirm back by itself.
    poll({ update_state: 'available' })
    await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(h.presses).toEqual([])
  })

  it('asks without a number while the machine has not reported its count since it signed in', async () => {
    await mount({ update_state: 'available' }, 'unknown')
    fireEvent.click(button() as HTMLButtonElement)
    expect(await screen.findByText(
      'The update stops the sessions on this machine. Chats that are working continue by themselves after it. Idle chats start again at their next message.')).toBeTruthy()
    expect(h.presses).toEqual([])
  })

  it('does not ask for a press while the retry after two rollbacks runs', async () => {
    await mount({ update_state: 'running', update_rollback_count: 2, update_rollback_target: '0.5.186' })
    expect(screen.queryByTestId('auto-update-paused')).toBeNull()
  })

  it('updates at once when nothing runs on the machine', async () => {
    await mount({ update_state: 'available' }, 0)
    fireEvent.click(button() as HTMLButtonElement)
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(h.presses).toEqual(['m-admin'])
  })

  it('queues for a machine that is away without asking', async () => {
    await mount({ update_state: 'available', status: 'disconnected' }, null)
    fireEvent.click(button() as HTMLButtonElement)
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(h.presses).toEqual(['m-admin'])
  })
})
