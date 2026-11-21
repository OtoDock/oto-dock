/**
 * Admin → Remote machines, the Compute machines card: an admin adds a
 * compute machine on another server. The warning comes first, only a name is
 * asked, and one command comes back with where the server gets the image. An
 * added machine shows where it last signed in from, can be locked to that
 * address, is paired again from its card while it is offline, and its Remove
 * says that OtoDock removes itself from that server.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { personClick, renderWithDialogs } from './dialogsHarness'
import ComputeMachinesSection from '../pages/admin/RemoteMachinesPage.compute'
import type { ComputeCommand, RemoteMachine } from '../api/remoteMachines'

const COMMAND: ComputeCommand = {
  id: 'compute-0123456789abcdef', name: 'Rack two', expires_in_hours: 1,
  install_command: 'curl -fsSL -H "X-Pairing-Token: tok" "http://192.168.50.2:8400/v1/satellite/bootstrap?os=linux" | sudo bash',
  image: 'ghcr.io/otodock/otodock-proxy:2.0.0', image_public: true, plain: false,
  add_refusal: '', own: true, docker: true,
}

const h = vi.hoisted(() => ({
  add: vi.fn(),
  pair: vi.fn(),
  lock: vi.fn(),
  settings: { image: 'ghcr.io/otodock/otodock-proxy:2.0.0', image_public: true, plain: false, add_refusal: '', own: true, docker: true },
}))

vi.mock('@/api/remoteMachines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/remoteMachines')>()),
  useAddComputeMachine: () => ({ mutate: h.add, isPending: false }),
  usePairComputeAgain: () => ({ mutate: h.pair, isPending: false }),
  useSetComputeAddressLock: () => ({ mutate: h.lock, isPending: false }),
  useComputeSettings: () => ({ data: h.settings }),
}))

const OWN = {
  id: 'compute-own', name: 'Compute', status: 'online', last_seen: '2026-11-11T03:00:00Z', pairing_scope: 'compute',
  registered_by: '', capabilities: {},
  compute: { own: true, sessions: 0, holding: false, memory: null },
} as unknown as RemoteMachine
const RACK = {
  ...OWN, id: 'compute-0123456789abcdef', name: 'Rack two', compute_address: '192.168.50.7',
  compute_address_locked: false,
  compute: { own: false, sessions: 2, holding: false, memory: null, paired: true, waiting: false, refused_from: '' },
} as unknown as RemoteMachine

function mount(machines: RemoteMachine[]) {
  return renderWithDialogs(<ComputeMachinesSection machines={machines} onRemove={async () => {}} removing={false} />)
}

describe('Adding a compute machine on another server', () => {
  beforeEach(() => {
    h.add.mockReset()
    h.pair.mockReset()
    h.lock.mockReset()
    h.settings = { image: 'ghcr.io/otodock/otodock-proxy:2.0.0', image_public: true, plain: false, add_refusal: '', own: true, docker: true }
  })

  it('warns first, asks only a name, then shows one command and no secret', async () => {
    h.add.mockImplementation((_body, opts) => opts.onSuccess(COMMAND))
    mount([OWN])
    fireEvent.click(screen.getByTestId('compute-add'))
    const dialog = await screen.findByTestId('add-compute-machine')
    expect(within(dialog).getByTestId('compute-add-warning')).toHaveTextContent(
      'Chats, terminals and tasks will run on this server.They run there with the accounts of the people '
      + 'who use them. Add only a server you control.')
    expect(dialog).toHaveTextContent('If this server is lost, its chats go on from their history on another machine.')
    expect(within(dialog).queryByLabelText('Address')).toBeNull()
    const add = within(dialog).getByRole('button', { name: 'Add machine' })
    expect(add).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: ' Rack two ' } })
    personClick(add)
    await waitFor(() => expect(h.add).toHaveBeenCalled())
    expect(h.add.mock.calls[0][0]).toEqual({ name: 'Rack two' })
    const step = await within(dialog).findByTestId('compute-command')
    expect(step).toHaveTextContent(COMMAND.install_command)
    expect(step).toHaveTextContent('Paste it on the server as an account that can use sudo. It works once, within one hour.')
    expect(step).toHaveTextContent("The server downloads OtoDock's image, about 4 GB.")
    expect(step).toHaveTextContent('It shows as Online here once it signs in.')
    expect(step).not.toHaveTextContent('secret')
    expect(step).not.toHaveTextContent('https')
  })

  it('says to load the image first on an install built from source, and warns on plain http', async () => {
    h.add.mockImplementation((_body, opts) => opts.onSuccess(
      { ...COMMAND, image: 'otodock-compute-walk:a', image_public: false, plain: true }))
    mount([OWN])
    fireEvent.click(screen.getByTestId('compute-add'))
    const dialog = await screen.findByTestId('add-compute-machine')
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Rack two' } })
    personClick(within(dialog).getByRole('button', { name: 'Add machine' }))
    const step = await within(dialog).findByTestId('compute-command')
    expect(step).toHaveTextContent(
      'First load the image otodock-compute-walk:a on the server. This install was built from source, so the server cannot download it.')
    expect(step).toHaveTextContent('This install has no https, so run it only on a network you trust.')
  })

  it('says why it was not added', async () => {
    h.add.mockImplementation((_body, opts) => opts.onError(new Error('Give the machine a name of one line.')))
    mount([OWN])
    fireEvent.click(screen.getByTestId('compute-add'))
    const dialog = await screen.findByTestId('add-compute-machine')
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Rack two' } })
    personClick(within(dialog).getByRole('button', { name: 'Add machine' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Give the machine a name of one line.')
  })

  it('says what cannot run and how to see why on a Docker install', () => {
    mount([{ ...OWN, status: 'disconnected' }])
    expect(screen.getByTestId('compute-none-online')).toHaveTextContent(
      'No compute machine is online, so chats, tasks and calls cannot run. '
      + 'To see why, run docker compose logs otodock-compute.')
  })

  it('names the unit\'s log on an install from source', () => {
    h.settings = { ...h.settings, docker: false }
    mount([{ ...OWN, status: 'disconnected' }])
    expect(screen.getByTestId('compute-none-online')).toHaveTextContent(
      'No compute machine is online, so chats, tasks and calls cannot run. '
      + 'To see why, run journalctl -u otodock-compute.')
  })

  it('shows the card on an install with no compute machine of its own, and names the setup', () => {
    h.settings = { ...h.settings, docker: false, own: false }
    mount([])
    expect(screen.getByTestId('compute-machines')).toBeInTheDocument()
    expect(screen.getByTestId('compute-none-online')).toHaveTextContent(
      'This install has no compute machine, so chats, tasks and calls cannot run. '
      + 'On an install from source, run scripts/dev-setup.sh --service.')
  })

  it('says nothing about a machine that is away while the platform holds new work', () => {
    // An update or a backup stopped the compute service on purpose.
    mount([{ ...OWN, status: 'disconnected', compute: { own: true, sessions: 0, holding: true, memory: null } }])
    expect(screen.queryByTestId('compute-none-online')).toBeNull()
    expect(screen.getByTestId('compute-holding')).toHaveTextContent(
      'New chats wait while OtoDock updates, restarts or makes a backup.')
  })

  it('keeps the warning off while a compute machine is online', () => {
    mount([OWN])
    expect(screen.queryByTestId('compute-none-online')).toBeNull()
  })

  it('says why an install outside Docker adds none, in place of the Add button', () => {
    h.settings = { ...h.settings, add_refusal: 'This install runs outside Docker, so a compute machine on another server cannot run its tools. Only an install in Docker can add one.' }
    mount([OWN])
    expect(screen.queryByTestId('compute-add')).toBeNull()
    expect(screen.getByTestId('compute-add-refusal')).toHaveTextContent('Only an install in Docker can add one.')
  })

  it('shows where an added machine last signed in from, and locks it to that address', () => {
    mount([OWN, RACK])
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('On another server, last signed in from 192.168.50.7, 2 sessions')).toBeInTheDocument()
    // The install's own machine has no lock, no pairing and no Remove.
    expect(within(section).getAllByRole('switch')).toHaveLength(1)
    expect(within(section).queryByRole('button', { name: 'Remove Compute' })).toBeNull()
    fireEvent.click(within(section).getByRole('switch', { name: 'Lock to this address' }))
    expect(h.lock).toHaveBeenCalledWith(
      { machineId: RACK.id, locked: true, address: '192.168.50.7' }, expect.anything())
  })

  it('says a locked machine refused a sign-in, and why a lock did not change', () => {
    h.lock.mockImplementation((_body, opts) => opts.onError(new Error('Its address changed to 10.0.0.9. Look again before you lock it.')))
    const locked = {
      ...RACK, compute_address_locked: true,
      compute: { ...RACK.compute, refused_from: '203.0.113.50' },
    } as unknown as RemoteMachine
    mount([OWN, locked])
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('On another server, locked to 192.168.50.7, 2 sessions')).toBeInTheDocument()
    expect(within(section).getByTestId('compute-refused')).toHaveTextContent('Refused a sign-in from 203.0.113.50.')
    fireEvent.click(within(section).getByRole('switch', { name: 'Lock to this address' }))
    expect(within(section).getByRole('alert')).toHaveTextContent('Its address changed to 10.0.0.9.')
  })

  it('pairs an offline machine again with a new command', async () => {
    h.pair.mockImplementation((_id, opts) => opts.onSuccess(COMMAND))
    const offline = { ...RACK, status: 'disconnected' } as unknown as RemoteMachine
    const waiting = {
      ...RACK, id: 'compute-new', name: 'Rack three', status: 'never_connected', compute_address: '', last_seen: null,
      compute: { ...RACK.compute, paired: false, waiting: true },
    } as unknown as RemoteMachine
    mount([OWN, offline, waiting])
    const section = screen.getByTestId('compute-machines')
    expect(within(section).getByText('Waiting for its first sign-in')).toBeInTheDocument()
    expect(within(section).getAllByRole('button', { name: 'Pair again' })).toHaveLength(2)
    // A machine with no first sign-in has no lock to offer.
    expect(within(section).getAllByRole('switch')).toHaveLength(1)
    fireEvent.click(within(section).getAllByRole('button', { name: 'Pair again' })[0])
    expect(h.pair).toHaveBeenCalledWith(RACK.id, expect.anything())
    expect(await screen.findByTestId('compute-command')).toHaveTextContent(COMMAND.install_command)
    expect(screen.getByText('Pair Rack two again')).toBeInTheDocument()
  })

  it('offers Pair again to an online machine whose server runs an older setup', () => {
    const behind = { ...RACK, compute: { ...RACK.compute, host_behind: true } } as unknown as RemoteMachine
    mount([OWN, behind])
    const section = screen.getByTestId('compute-machines')
    expect(within(screen.getByTestId(`compute-row-${RACK.id}`)).getByText('Online')).toBeInTheDocument()
    expect(within(section).getByRole('button', { name: 'Pair again' })).toBeInTheDocument()
    expect(within(section).getByTestId('compute-host-behind')).toHaveTextContent(
      'Its server runs an older OtoDock setup. Pair it again to update it.')
  })

  it('offers no lock for an address other clients share, and says why', () => {
    const shared = { ...RACK, compute: { ...RACK.compute, shared: true } } as unknown as RemoteMachine
    mount([OWN, shared])
    const section = screen.getByTestId('compute-machines')
    expect(within(section).queryByRole('switch')).toBeNull()
    expect(within(section).getByTestId('compute-shared')).toHaveTextContent(
      'Other clients reach OtoDock from this address too, so it cannot be locked.')
  })

  it('still lets an admin turn off a lock that meets a shared address', () => {
    const locked = { ...RACK, compute_address_locked: true, compute: { ...RACK.compute, shared: true } } as unknown as RemoteMachine
    mount([OWN, locked])
    const section = screen.getByTestId('compute-machines')
    fireEvent.click(within(section).getByRole('switch', { name: 'Lock to this address' }))
    expect(h.lock).toHaveBeenCalledWith({ machineId: RACK.id, locked: false, address: '192.168.50.7' }, expect.anything())
  })

  it('offers no new pairing while it is online', () => {
    mount([OWN, RACK])
    expect(screen.queryByRole('button', { name: 'Pair again' })).toBeNull()
  })

  it('removes a machine on another server with words that keep the conversation', () => {
    mount([OWN, RACK])
    fireEvent.click(screen.getByRole('button', { name: 'Remove Rack two' }))
    expect(screen.getByRole('alertdialog')).toHaveTextContent(
      'A chat running on it stops. Its next message runs on another compute machine and keeps the conversation. '
      + 'If the server connects within 30 days, OtoDock removes itself from it.')
  })
})
