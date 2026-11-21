import { useState } from 'react'

import {
  useComputeSettings, usePairComputeAgain, useSetComputeAddressLock,
  type ComputeBox, type ComputeCommand, type ComputeSettings, type RemoteMachine,
} from '../../api/remoteMachines'
import AlertDialog from '../../components/ui/AlertDialog'
import Button from '../../components/ui/Button'
import Dialog from '../../components/ui/Dialog'
import InfoTip from '../../components/ui/InfoTip'
import Switch from '../../components/ui/Switch'
import { BADGE_BASE, BADGE_TONE, CARD } from '../../components/ui/classes'
import { errorText } from '../../lib/http/apiJson'
import { isReachableMachine, machineStateWord } from '../../lib/status/machine'
import AddComputeMachineDialog, { ComputeCommandStep } from './RemoteMachinesPage.computeAdd'

/** The machine's state in the platform's words, capitalised for a badge. */
function stateLabel(state: string | null | undefined): string {
  const word = machineStateWord(state)
  return word.charAt(0).toUpperCase() + word.slice(1)
}

/** Megabytes as gigabytes with one decimal, the way a person reads memory. */
function gigabytes(mb: number): string {
  return `${(Math.round((mb / 1024) * 10) / 10).toLocaleString()} GB`
}

/** Whether a compute machine was added for another server (every one but the install's own). */
function isAdded(m: RemoteMachine): boolean {
  return !m.compute?.own
}

/** Where an added machine signs in from, in a few words. */
function placeLine(m: RemoteMachine): string {
  if (!isAdded(m)) return ''
  if (!m.compute_address || !m.last_seen) return 'Waiting for its first sign-in'
  return m.compute_address_locked
    ? `On another server, locked to ${m.compute_address}`
    : `On another server, last signed in from ${m.compute_address}`
}

/** What a compute machine holds now, in one short line. */
function computeLine(m: RemoteMachine, box: ComputeBox | undefined): string {
  const said: string[] = []
  const place = placeLine(m)
  if (place) said.push(place)
  if (!box || (isAdded(m) && !m.last_seen)) return said.join(', ')
  said.push(box.sessions === 1 ? '1 session' : `${box.sessions} sessions`)
  if (box.memory) said.push(`${gigabytes(box.memory.available_mb)} free of ${gigabytes(box.memory.total_mb)}`)
  return said.join(', ')
}

/**
 * What the card says while no compute machine is online, with the way out:
 * the command that shows why on an install that runs one of its own (its
 * log is Docker's or the unit's), the setup command on one that runs none.
 * The command is set in the code font: the text font draws two hyphens as
 * one dash, and a person types what they read.
 */
function NoneOnlineLine({ settings, count }: { settings: ComputeSettings | undefined; count: number }) {
  const cmd = (text: string) => <code className="font-mono select-all">{text}</code>
  if (settings && !settings.own && count === 0) {
    return <>This install has no compute machine, so chats, tasks and calls cannot run. On an install from source, run {cmd('scripts/dev-setup.sh --service')}.</>
  }
  const said = 'No compute machine is online, so chats, tasks and calls cannot run.'
  if (!settings) return <>{said}</>
  return <>{said} To see why, run {cmd(settings.docker ? 'docker compose logs otodock-compute' : 'journalctl -u otodock-compute')}.</>
}

/**
 * The compute machines: machines the platform runs itself, which no person
 * owns (`remote_machines.pairing_scope` is `compute`). The install's own
 * (the compose service or the bare-metal unit) has no Remove, no pairing and
 * no lock: the install keeps it. An added one runs on another server: one
 * pasted command pairs it, "Pair again" makes a new command while it is not
 * online or while its server runs an older setup (`compute.host_behind`), and
 * "Lock to this address" pins the address it signs in from, unless other
 * clients share that address (`compute.shared`). The card shows on every
 * install, with or without a machine: with none online it says what cannot
 * run and how to see why. While the platform holds new work (an update, a
 * backup, a start) the holding line stands alone: the stop was asked for.
 */
export default function ComputeMachinesSection({
  machines,
  onRemove,
  removing,
}: {
  machines: RemoteMachine[]
  onRemove: (id: string) => Promise<unknown>
  removing: boolean
}) {
  const settings = useComputeSettings()
  const [confirm, setConfirm] = useState<RemoteMachine | null>(null)
  const [error, setError] = useState('')
  const [rowError, setRowError] = useState<{ id: string; text: string } | null>(null)
  const [adding, setAdding] = useState(false)
  const [command, setCommand] = useState<ComputeCommand | null>(null)
  const pairAgain = usePairComputeAgain()
  const lock = useSetComputeAddressLock()

  if (machines.length === 0 && !settings.data) return null
  const holding = machines.some(m => m.compute?.holding)
  const noneOnline = !machines.some(m => isReachableMachine(m.status))

  const setLock = (m: RemoteMachine, locked: boolean) => {
    setRowError(null)
    lock.mutate({ machineId: m.id, locked, address: m.compute_address ?? '' }, {
      onError: e => setRowError({ id: m.id, text: errorText(e) || 'The lock was not changed.' }),
    })
  }
  const pair = (m: RemoteMachine) => {
    setRowError(null)
    pairAgain.mutate(m.id, {
      onSuccess: setCommand,
      onError: e => setRowError({ id: m.id, text: errorText(e) || 'No new command was made.' }),
    })
  }

  return (
    <div className={CARD} data-testid="compute-machines">
      <div className="p-4">
        <h3 className="text-base font-semibold text-primary">
          Compute machines{' '}
          <span className="text-tertiary font-normal">({machines.length})</span>
          <InfoTip label="Compute machines">
            No person owns them. Chats, terminals, tasks, calls and meetings on the platform run on them. Apps run on the one beside the platform, never on an added server. While none is online, these cannot run. Chats on a remote machine still can.
          </InfoTip>
        </h3>
        <div className="mt-0.5 flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-tertiary">Machines the platform runs itself.</p>
          {settings.data && !settings.data.add_refusal && (
            <Button size="xs" onClick={() => setAdding(true)} data-testid="compute-add">Add a compute machine</Button>
          )}
        </div>
        {settings.data?.add_refusal && (
          <p className="text-xs text-tertiary mt-1" data-testid="compute-add-refusal">{settings.data.add_refusal}</p>
        )}
        {noneOnline && !holding && (
          <p className="text-xs text-status-warning-fg mt-1" data-testid="compute-none-online">
            <NoneOnlineLine settings={settings.data} count={machines.length} />
          </p>
        )}
        {holding && (
          <p className="text-xs text-status-warning-fg mt-1" data-testid="compute-holding">
            New chats wait while OtoDock updates, restarts or makes a backup.
          </p>
        )}
      </div>
      {machines.length > 0 && (
        <div className="border-t border-subtle divide-y divide-subtle">
          {machines.map(m => {
            const added = isAdded(m)
            const online = isReachableMachine(m.status)
            const refused = m.compute_address_locked ? (m.compute?.refused_from ?? '') : ''
            return (
              <div key={m.id} className="p-3" data-testid={`compute-row-${m.id}`}>
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-primary truncate">{m.name}</p>
                    {/* Wraps on a phone: the address is the line's point. */}
                    <p className="text-xs text-tertiary break-words sm:truncate">
                      {computeLine(m, m.compute) || m.id}
                    </p>
                  </div>
                  <span className={`${BADGE_BASE} ${online ? BADGE_TONE.success : BADGE_TONE.neutral} shrink-0`}>
                    {stateLabel(m.status)}
                  </span>
                  {added && (!online || m.compute?.host_behind) && (
                    <Button size="xs" className="shrink-0" disabled={pairAgain.isPending} onClick={() => pair(m)}>
                      Pair again
                    </Button>
                  )}
                  {/* Icon-only on phones, "Remove" from sm: the row never pushes
                      the page wider than the screen. The install's own machine
                      has none. */}
                  {added && (
                    <button
                      type="button"
                      onClick={() => { setError(''); setConfirm(m) }}
                      title="Remove this machine"
                      aria-label={`Remove ${m.name}`}
                      className="shrink-0 inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                      <span className="hidden sm:inline">Remove</span>
                    </button>
                  )}
                </div>
                {added && m.compute?.host_behind && (
                  <p className="mt-1 text-xs text-status-warning-fg" data-testid="compute-host-behind">
                    Its server runs an older OtoDock setup. Pair it again to update it.
                  </p>
                )}
                {added && m.compute_address && m.last_seen && m.compute?.shared && !m.compute_address_locked && (
                  <p className="mt-2 text-xs text-tertiary" data-testid="compute-shared">
                    Other clients reach OtoDock from this address too, so it cannot be locked.
                  </p>
                )}
                {added && m.compute_address && m.last_seen && (!m.compute?.shared || m.compute_address_locked) && (
                  <div className="mt-2 flex items-center gap-2">
                    <Switch
                      checked={!!m.compute_address_locked}
                      onChange={next => setLock(m, next)}
                      disabled={lock.isPending}
                      aria-labelledby={`compute-lock-${m.id}`}
                    />
                    <span id={`compute-lock-${m.id}`} className="text-xs text-secondary">Lock to this address</span>
                    <InfoTip label="Lock to this address">
                      A sign-in from another address is refused, and every admin is told.
                    </InfoTip>
                  </div>
                )}
                {refused && (
                  <p className="mt-1 text-xs text-status-warning-fg" data-testid="compute-refused">
                    Refused a sign-in from {refused}.
                  </p>
                )}
                {rowError?.id === m.id && (
                  <p className="mt-1 text-xs text-status-danger-fg" role="alert">{rowError.text}</p>
                )}
              </div>
            )
          })}
        </div>
      )}
      {confirm && (
        <AlertDialog
          title={`Remove ${confirm.name}?`}
          confirmLabel="Remove machine"
          tone="danger"
          busy={removing}
          busyLabel="Removing…"
          error={error || null}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            void onRemove(confirm.id)
              .then(() => setConfirm(null))
              .catch((e: unknown) => setError(errorText(e) || 'The machine was not removed.'))
          }}
        >
          A chat running on it stops. Its next message runs on another compute machine and keeps the conversation. If the server connects within 30 days, OtoDock removes itself from it.
        </AlertDialog>
      )}
      {adding && <AddComputeMachineDialog onClose={() => setAdding(false)} />}
      {command && (
        <Dialog onClose={() => setCommand(null)} dismissible="escape" title={`Pair ${command.name} again`} placement="top">
          <div className="space-y-4 p-4">
            <ComputeCommandStep command={command} />
            <div className="flex justify-end">
              <Button variant="primary" onClick={() => setCommand(null)}>Done</Button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  )
}
