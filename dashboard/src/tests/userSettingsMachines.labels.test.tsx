/**
 * User Settings → My machines, the filesystem-access texts: the machine's
 * own OtoDock folder is refused to every
 * session, and home-only is a guardrail, not a sandbox.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

const machine = (id: string, name: string, allow_full_fs: boolean) => ({
  id, name, allow_full_fs, pairing_scope: 'user', status: 'online', last_seen: null,
  last_heartbeat_age_s: 2, capabilities: { os: 'linux' }, assigned_agents: [],
  browser_mode: 'dedicated',
})

const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false, error: null }
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

describe('My machines filesystem-access texts', () => {
  it('names the refused folder on a full-access card and the guardrail on a home-only one', () => {
    render(<MyMachinesSection />)
    fireEvent.click(screen.getByText('Work laptop'))
    expect(screen.getByText(
      'Full filesystem access. Agents can read and write any path your OS user can reach, '
      + "except the machine's own OtoDock folder.")).toBeInTheDocument()
    fireEvent.click(screen.getByText('Home laptop'))
    expect(screen.getByText(
      'Home folder only. Agents are limited to the agent tree and your OS home folder, '
      + 'a guardrail, not a sandbox.')).toBeInTheDocument()
  })

  it('says the same in the pairing dialog', () => {
    render(<MyMachinesSection />)
    fireEvent.click(screen.getByRole('button', { name: 'Pair Machine' }))
    expect(screen.getByText(
      'By default agents on your machine can only read and write files under your home folder. '
      + 'Enable this if you want agents to manage system services or edit files outside your '
      + 'home: they will be able to touch any path your OS account can reach, except the '
      + "machine's own OtoDock folder. Agents run natively as your OS user either way (the home "
      + 'folder setting is a guardrail, not a kernel sandbox), so only pair machines you trust. '
      + 'You can change this later.')).toBeInTheDocument()
  })
})
