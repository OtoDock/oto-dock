/**
 * User Settings → My machines, the filesystem-access texts: the machine's
 * own OtoDock folder is refused to every
 * session, and home-only is a guardrail, not a sandbox.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { findAlertDialog, personClick, renderWithDialogs } from './dialogsHarness'

const machine = (id: string, name: string, allow_full_fs: boolean) => ({
  id, name, allow_full_fs, pairing_scope: 'user', status: 'online', last_seen: null,
  last_heartbeat_age_s: 2, capabilities: { os: 'linux' }, assigned_agents: [],
  browser_mode: 'dedicated',
})

const idle = { mutate: vi.fn(), mutateAsync: vi.fn(() => Promise.resolve()), isPending: false, error: null }
vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/remoteMachines')>()),
  useMyRemoteMachines: () => ({
    data: {
      machines: [machine('m-full-0001', 'Work laptop', true), machine('m-home-0002', 'Home laptop', false)],
      targets: [],
    },
    isLoading: false,
  }),
  usePairMyMachine: () => idle,
  useDeleteMyMachine: () => idle,
  useSetMyRemoteTarget: () => idle,
  useRemoveMyRemoteTarget: () => idle,
  useSetMyAllowFullFs: () => idle,
  useSetMyDeviceGrants: () => idle,
  useSetBrowserMode: () => idle,
  useSetBrowserToken: () => idle,
}))
vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({ user: { sub: 'u1', role: 'member', agent_roles: {}, feature_flags: {} } }),
}))
vi.mock('@/components/RemoteBadge', () => ({ default: () => null }))
vi.mock('@/components/PairInstallCommand', () => ({ default: () => null }))

import { MyMachinesSection } from '@/pages/UserSettings.machines'
import { DEVICE_CAPABILITY_INFO, deviceGrantBody } from '@/api/remoteMachines'

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

describe('My machines filesystem-access texts', () => {
  it('names the refused folder on a full-access card and the guardrail on a home-only one', () => {
    render(<MyMachinesSection />)
    fireEvent.click(screen.getByText('Work laptop'))
    expect(screen.getByText(
      'Full filesystem access. Agents can read and write any path your OS user can reach.')).toBeInTheDocument()
    expect(infoTexts('Filesystem access')).toContain("The machine's own OtoDock folder stays off limits.")
    fireEvent.click(screen.getByText('Home laptop'))
    expect(screen.getByText('Home folder only. This is a guardrail, not full isolation.')).toBeInTheDocument()
    expect(infoTexts('Filesystem access')).toContain('Agents stay in their agent folders and your OS home folder.')
  })

  it('says the same in the pairing dialog', () => {
    render(<MyMachinesSection />)
    fireEvent.click(screen.getByRole('button', { name: 'Pair Machine' }))
    expect(screen.getByText(
      'The home folder limit is a guardrail, not full isolation. Only pair machines you trust.')).toBeInTheDocument()
    expect(infoTexts('Allow full filesystem access')).toEqual([
      'By default, agents can only read and write files in your home folder. '
      + 'Turn this on if agents should manage system services or edit files outside your home. '
      + 'They can then reach any path your OS account can. '
      + "The machine's own OtoDock folder stays off limits. Either way, agents run as your OS user. "
      + 'You can change this later.'])
  })
})

describe('My machines device grants', () => {
  it('says what App connectors lets your agents do, and Cancel grants nothing', async () => {
    idle.mutate.mockClear()
    idle.mutateAsync.mockClear()
    renderWithDialogs(<MyMachinesSection />)
    fireEvent.click(screen.getByText('Work laptop'))
    expect(infoTexts('App connectors')).toContain('Work in the applications on this computer')
    fireEvent.click(screen.getAllByRole('checkbox', { name: 'App connectors' })[0])
    const dialog = await findAlertDialog('Grant "App connectors" on Work laptop?')
    expect(within(dialog).getByText(
      'Your agents can work in the applications on this computer that have a connector, like Blender. '
      + 'They can run code inside those applications. Turn it on only on a computer you trust them with.',
    )).toBeInTheDocument()
    personClick(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(idle.mutate).not.toHaveBeenCalled()
    expect(idle.mutateAsync).not.toHaveBeenCalled()
  })

  it('words every grant plainly on both pages', () => {
    for (const cap of DEVICE_CAPABILITY_INFO) {
      for (const assigned of [false, true]) {
        const body = deviceGrantBody(cap, assigned)
        expect(body.startsWith(assigned ? 'Assigned agents can ' : 'Your agents can ')).toBe(true)
        expect(body).not.toMatch(/[—–;/]/)
        for (const sentence of body.split(/(?<=\.)\s+/)) {
          expect(sentence.split(/\s+/).length).toBeLessThanOrEqual(20)
        }
      }
    }
  })
})
