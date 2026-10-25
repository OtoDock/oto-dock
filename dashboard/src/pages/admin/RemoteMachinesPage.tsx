import { useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  useRemoteMachines, usePairMachine, useDeleteMachine, useAssignAgent, useUnassignAgent,
  useSetAutoUpdate, useTriggerUpdateNow, useSetAllowFullFs, useSetDeviceGrants,
  useSetMaxSessions,
  DEVICE_CAPABILITY_INFO,
  deviceGrantBody,
  type DeviceCapabilityInfo,
  type RemoteMachine, type PairResult,
} from '../../api/remoteMachines'
import { PAIRING_SCOPE } from '../../lib/placement'
import { MACHINE_STATE, type MachineState } from '../../lib/status/machine'
import { useAgents, useExecutionLayers } from '../../api/agents'
import { useFlag } from '../../hooks/useFlag'
import { hasAgentScope, modeOfAgent } from '../../lib/visibility'
import { engineLabels, installedCliBinaries, runsRemote } from '../../lib/engines'
import { cliChipInfo } from '../../lib/cliChip'
import RemoteBadge from '../../components/RemoteBadge'
import PairInstallCommand from '../../components/PairInstallCommand'
import { BrowserModeSelect, BrowserTokenField } from '../../components/BrowserModeControls'
import { browserModeDesc, browserModeOf } from '../../lib/remote/browserMode'
import { errorText, apiJson, toastError, withMessage } from '../../lib/http/apiJson'
import { useDialogs } from '../../components/ui/dialogs'
import AlertDialog from '../../components/ui/AlertDialog'
import Dialog from '../../components/ui/Dialog'
import Button from '../../components/ui/Button'
import { Input, Select } from '../../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD, DIALOG_PANEL } from '../../components/ui/classes'
import InfoTip from '../../components/ui/InfoTip'
import { CheckboxRow, SaveState } from '../../components/ui/SettingsControls'
import { useSaveState } from '../../hooks/useSaveState'

// Per-satellite live stats from /v1/admin/concurrency-stats (5s poll), the same
// shape PlatformPage consumes. Only the per-machine `satellites[]` slice is used
// here, joined to each machine row by machine_id.
interface SatelliteStat {
  machine_id: string
  name: string
  online: boolean
  active_sessions: number
  max_sessions: number | null
  cpu_pct: number
  mem_pct: number
}

function useSatelliteStats() {
  // Shares the ['concurrency-stats'] cache with PlatformPage's useConcurrencyStats
  // to dedupe the 5s poll. CRITICAL: both observers must cache the SAME (full)
  // shape — return the whole payload and derive the satellites via `select`.
  // Returning just `data.satellites` here would clobber the shared cache with a
  // bare array, making PlatformPage's `stats.sessions.active` read crash.
  return useQuery({
    queryKey: ['concurrency-stats'],
    queryFn: async (): Promise<{ satellites?: SatelliteStat[] }> => {
      return apiJson<{ satellites?: SatelliteStat[] }>('/v1/admin/concurrency-stats')
        .catch(withMessage('Failed to fetch'))
    },
    select: (data) => (data.satellites ?? []) as SatelliteStat[],
    refetchInterval: 5_000,
  })
}

// Per-machine live capacity (active/max sessions, cpu%/mem%) + an
// editable proxy-side max_sessions override. Empty input = NULL (use the
// satellite's own recommendation). The satellite still hard-caps at its
// physical max on its own — this is just the proxy soft pre-check value.
function MachineCapacityControls({
  machine, stat,
}: {
  machine: RemoteMachine
  stat?: SatelliteStat
}) {
  const setMaxSessions = useSetMaxSessions()
  const { stateOf, run } = useSaveState()
  // Local edit buffer seeded from the persisted override (null → empty).
  const [value, setValue] = useState(
    machine.max_sessions != null ? String(machine.max_sessions) : '',
  )
  const persisted = machine.max_sessions != null ? String(machine.max_sessions) : ''
  // A saved override replaces the buffer (in render, on the change).
  const [seeded, setSeeded] = useState(persisted)
  if (seeded !== persisted) {
    setSeeded(persisted)
    setValue(persisted)
  }
  // The recommendation the satellite reports (the effective cap when no
  // override is set), shown as the input placeholder.
  const recommended = stat && stat.max_sessions != null ? stat.max_sessions : null

  const save = () => {
    if (value === persisted) return
    const trimmed = value.trim()
    const parsed = trimmed === '' ? null : parseInt(trimmed, 10)
    if (parsed != null && (!Number.isFinite(parsed) || parsed < 1)) {
      setValue(persisted)  // reject junk, revert to persisted
      return
    }
    void run('max_sessions', () => setMaxSessions.mutateAsync({ machineId: machine.id, value: parsed }),
      { failTitle: 'The session limit was not saved' })
  }

  const sessionsLabel = stat
    ? `${stat.active_sessions} / ${stat.max_sessions ?? '∞'}`
    : '—'

  return (
    <div className="pt-2 border-t border-subtle space-y-2">
      <p className="text-xs font-medium text-tertiary">Capacity</p>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5">
          <p className="text-xs text-primary">
            Active sessions: <span className="font-mono">{sessionsLabel}</span>
            {stat && (
              <span className="ml-2 text-tertiary">
                CPU <span className="font-mono">{Math.round(stat.cpu_pct)}%</span>
                {' · '}
                Mem <span className="font-mono">{Math.round(stat.mem_pct)}%</span>
              </span>
            )}
          </p>
          <p className="text-3xs text-tertiary max-w-md">Blank uses the machine's own recommendation{recommended != null ? `, ${recommended}` : ''}.</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span>
            <label className="text-xs text-tertiary">Max sessions</label>
            <InfoTip label="Max sessions">The most sessions OtoDock sends to this machine at once. The machine also stops at its own hard limit.</InfoTip>
            <SaveState state={stateOf('max_sessions')} />
          </span>
          <Input
            type="number"
            min={1}
            value={value}
            placeholder={recommended != null ? `Auto (${recommended})` : 'Auto'}
            onChange={e => setValue(e.target.value)}
            onBlur={save}
            disabled={setMaxSessions.isPending}
            className="max-w-24 text-right"
          />
        </div>
      </div>
    </div>
  )
}

// One class per live state (the mirror's words; the stored column's
// `offline` never arrives). `paused` reads amber like `stale`: offline by
// intent, not a fault.
const STATUS_COLORS: Record<MachineState, string> = {
  online: BADGE_TONE.success,
  stale: BADGE_TONE.warning,
  paused: BADGE_TONE.warning,
  disconnected: BADGE_TONE.warning,
  never_connected: BADGE_TONE.neutral,
}

// Per-machine update controls — version + auto-update toggle +
// manual "Update now" button. Rendered inside the expanded machine row.
function MachineUpdateControls({ machine }: { machine: RemoteMachine }) {
  const setAutoUpdate = useSetAutoUpdate()
  const { stateOf, run } = useSaveState()
  const triggerUpdate = useTriggerUpdateNow()
  const autoEnabled = machine.auto_update_enabled ?? true
  const version = machine.satellite_version || 'unknown'
  const updateError = machine.last_update_error
  const pending = machine.pending_update ?? false
  // Proxy-side budget (ws/satellite.py MAX_AUTO_UPDATE_ROLLBACKS = 2): once
  // the same target rolled back twice, automatic pushes of it stop and only
  // "Update now" retries — say so, or the admin keeps waiting for an update
  // that will never come by itself.
  const rollbackCount = machine.update_rollback_count ?? 0
  const rollbackTarget = machine.update_rollback_target || ''
  const autoPaused = autoEnabled && !pending && !!rollbackTarget && rollbackCount >= 2

  return (
    <div className="pt-2 border-t border-subtle space-y-2">
      <p className="text-xs font-medium text-tertiary">Updates<SaveState state={stateOf('auto_update')} /></p>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5 min-w-0">
          <p className="text-xs text-primary">
            Satellite version: <span className="font-mono">{version}</span>
            {pending && (
              <span className={`${BADGE_BASE} ${BADGE_TONE.warning} ml-2`}>
                Update queued for next reconnect
              </span>
            )}
          </p>
          {updateError && (
            <p className="text-xs text-status-danger-fg">
              Last update failed: {updateError}
            </p>
          )}
          {autoPaused && (
            <p className="text-xs text-status-warning-fg" data-testid="auto-update-paused">
              Automatic updates are paused. The update to{' '}
              <span className="font-mono">{rollbackTarget}</span> rolled back{' '}
              {rollbackCount} times. Click "Update now" to try again.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="inline-flex items-center gap-1.5 text-xs text-primary cursor-pointer">
            <input
              type="checkbox"
              checked={autoEnabled}
              disabled={setAutoUpdate.isPending}
              onChange={e => {
                const enabled = e.target.checked
                void run('auto_update', () => setAutoUpdate.mutateAsync({ machine_id: machine.id, enabled }),
                  { failTitle: 'The auto-update setting was not saved' })
              }}
              className="accent-action"
            />
            Auto-update
          </label>
          <Button
            size="xs"
            type="button"
            onClick={() => { void triggerUpdate.mutateAsync(machine.id).catch(toastError('The update was not started')) }}
            disabled={triggerUpdate.isPending}
            title={
              machine.status === MACHINE_STATE.ONLINE
                ? 'Send the latest version to this machine now'
                : 'Update this machine the next time it connects'
            }
          >
            {triggerUpdate.isPending ? 'Triggering…' : 'Update now'}
          </Button>
        </div>
      </div>
      {!autoEnabled && (
        <p className="text-3xs text-tertiary">
          Auto-update is off. OtoDock refuses this machine when it needs a
          newer version. Click "Update now" to update it.
        </p>
      )}
    </div>
  )
}

// Per-machine filesystem-access policy toggle. When enabled,
// the path framework admits any path the satellite-user's OS account
// can reach; when disabled, only the agent tree + the OS user's home
// directory are admitted. Admin can flip this on any machine (admin-
// or user-paired) from this page.
function MachineFsPolicyControls({ machine }: { machine: RemoteMachine }) {
  const setAllowFullFs = useSetAllowFullFs()
  const { stateOf, run } = useSaveState()
  const allowFullFs = machine.allow_full_fs ?? false
  return (
    <div className="pt-2 border-t border-subtle space-y-2">
      <p className="text-xs font-medium text-tertiary">
        Filesystem access
        <InfoTip label="Filesystem access">
          {allowFullFs ? "The machine's own OtoDock folder stays off limits." : "Agents stay in their agent folders and the OS user's home folder."}
        </InfoTip>
        <SaveState state={stateOf('fs')} />
      </p>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5 max-w-md">
          <p className="text-xs text-primary">
            {allowFullFs
              ? 'Full filesystem access. Agents can read and write any path the OS user can reach.'
              : 'Home folder only. This is a guardrail, not full isolation.'}
          </p>
        </div>
        <label className="inline-flex items-center gap-1.5 text-xs text-primary cursor-pointer">
          <input
            type="checkbox"
            checked={allowFullFs}
            disabled={setAllowFullFs.isPending}
            onChange={e => {
              const enabled = e.target.checked
              void run('fs', () => setAllowFullFs.mutateAsync({ machineId: machine.id, enabled }),
                { failTitle: 'The filesystem access was not changed' })
            }}
            className="accent-action"
          />
          Allow full FS
        </label>
      </div>
    </div>
  )
}


// Per-machine device-control consent. Each
// capability is OFF by default and granted only here (admin scope) or by the
// owner for user-paired machines. Strictly more powerful than full-FS (it can
// click system/sudo prompts and drive a browser with saved logins), so
// enabling asks for confirmation.
function MachineDeviceGrantsControls({ machine }: { machine: RemoteMachine }) {
  const setDeviceGrants = useSetDeviceGrants()
  const dialogs = useDialogs()
  const { stateOf, run } = useSaveState()
  const granted = new Set(machine.device_grants ?? [])
  // The grants as they are now: a refresh may land while the confirm is open.
  const grantsRef = useRef(machine.device_grants)
  useLayoutEffect(() => { grantsRef.current = machine.device_grants })
  const noDisplay = machine.capabilities?.display?.has_display === false
  const toggle = async (cap: DeviceCapabilityInfo, on: boolean) => {
    if (on && !(await dialogs.confirm({
      title: `Grant "${cap.label}" on ${machine.name}?`,
      body: deviceGrantBody(cap, true),
      confirmLabel: 'Grant',
    }))) return
    const next = new Set(grantsRef.current ?? [])
    if (on) { next.add(cap.key) } else { next.delete(cap.key) }
    void run('grants', () => setDeviceGrants.mutateAsync({ machineId: machine.id, grants: [...next] }),
      { failTitle: `The ${cap.label} permission was not changed` })
  }
  return (
    <div className="pt-2 border-t border-subtle space-y-2">
      <p className="text-xs font-medium text-tertiary">Device control<SaveState state={stateOf('grants')} /></p>
      <p className="text-3xs text-tertiary max-w-md">
        Let assigned agents control this computer’s screen, browser or apps. Off
        by default.
      </p>
      {noDisplay && (
        <p className="text-3xs text-status-warning-fg max-w-md">
          This computer reported no screen. Computer and browser control work
          once someone is signed in to its desktop.
        </p>
      )}
      <div className="flex flex-col gap-1">
        {DEVICE_CAPABILITY_INFO.map(cap => {
          const on = granted.has(cap.key)
          const isBrowser = cap.key === 'browser'
          // The Browser-control row carries its mode selector while granted,
          // and its description follows the selected mode.
          const desc = isBrowser && on ? browserModeDesc(browserModeOf(machine)) : cap.desc
          return (
            <div key={cap.key} className="flex flex-col">
              <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-primary">
                <label className="inline-flex items-center gap-1.5 cursor-pointer whitespace-nowrap">
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={setDeviceGrants.isPending}
                    onChange={e => toggle(cap, e.target.checked)}
                    className="accent-action"
                  />
                  <span className="font-medium">{cap.label}</span>
                </label>
                <InfoTip label={cap.label}>{desc}</InfoTip>
                {isBrowser && on && <BrowserModeSelect machine={machine} scope="admin" />}
              </div>
              {/* Own mode's token block belongs to this row, not to the list. */}
              {isBrowser && on && <BrowserTokenField machine={machine} scope="admin" />}
            </div>
          )
        })}
      </div>
    </div>
  )
}


export default function RemoteMachinesPage() {
  const { data: machines, isLoading } = useRemoteMachines()
  const { data: satelliteStats } = useSatelliteStats()
  const { data: agents } = useAgents({ all: true })
  const { data: layers } = useExecutionLayers()

  // This build ships without the remote-machines feature (the nav entry is
  // already hidden — this covers a direct URL). Every hook runs above the
  // early return below: a user snapshot that flips the flag between two
  // renders must not change how many hooks ran.
  const featureAvailable = useFlag('remote_machines_available')

  // machine_id → live per-satellite stats (active/max sessions, cpu%/mem%).
  const statByMachine = new Map((satelliteStats ?? []).map(s => [s.machine_id, s]))
  const pairMachine = usePairMachine()
  const deleteMachine = useDeleteMachine()
  const assignAgent = useAssignAgent()
  const unassignAgent = useUnassignAgent()

  const [showPairModal, setShowPairModal] = useState(false)
  const [pairResult, setPairResult] = useState<PairResult | null>(null)
  const [pairName, setPairName] = useState('')
  const [pairAllowFullFs, setPairAllowFullFs] = useState(false)  // admin default: home-only (opt-in full-FS)
  const [pairError, setPairError] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)
  // User-paired section is collapsed by default (per-laptop overview is
  // observability-only — admin doesn't need it open every time).
  const [showUserPaired, setShowUserPaired] = useState(false)
  // Admin removal of a USER-paired machine: a modal (the row is cramped and
  // this is someone else's machine) names the owner and what happens on
  // their side before anything is sent.
  const [removeUserMachine, setRemoveUserMachine] = useState<RemoteMachine | null>(null)
  const [removeError, setRemoveError] = useState('')

  if (!featureAvailable) {
    return (
      <div className="p-6">
        <div className="rounded-card border border-default bg-surface-muted p-4 text-sm text-secondary">
          Remote-machine support is not included in this build.
        </div>
      </div>
    )
  }

  const handlePair = async () => {
    setPairError('')
    try {
      const result = await pairMachine.mutateAsync({
        name: pairName.trim(),
        allow_full_fs: pairAllowFullFs,
      })
      setPairResult(result)
    } catch (e) {
      setPairError(errorText(e))
    }
  }

  // The caller shows a refusal (the Confirm button's toast).
  const handleDelete = (id: string) => deleteMachine.mutateAsync(id).then(() => {
    setDeleteConfirm(null)
    setExpandedId(null)
  })

  const handleAssign = async (machineId: string, agentSlug: string) => {
    await assignAgent.mutateAsync({ machineId, agentSlug }).catch(toastError('The agent was not assigned'))
  }

  const handleUnassign = async (machineId: string, agentSlug: string) => {
    await unassignAgent.mutateAsync({ machineId, agentSlug }).catch(toastError('The agent was not unassigned'))
  }

  // Agents eligible for remote assignment: their engine must run on a
  // satellite (the descriptor's `runtime.supports_remote_execution` — an
  // in-process engine has nothing to relocate) and they must have a shared
  // (agent) workspace to run in — a remote machine runs the agent scope, so
  // Personal-only agents (no shared space) are excluded. A machine that
  // advertises the engines it can run narrows the list further (below).
  const engineNames = engineLabels(layers)
  const cliBinaries = installedCliBinaries(layers)
  const eligibleAgents = (agents ?? []).filter(
    a => runsRemote(layers?.[a.execution_path]) && hasAgentScope(modeOfAgent(a))
  )

  if (isLoading) return <div className="p-6 text-tertiary">Loading...</div>

  // Split admin-paired (this section is fully managed here) from
  // user-paired (read-only observability section below).
  const adminMachines = (machines ?? []).filter(m => m.pairing_scope !== PAIRING_SCOPE.USER)
  const userMachines = (machines ?? []).filter(m => m.pairing_scope === PAIRING_SCOPE.USER)

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-display text-xl font-semibold text-primary">Platform Remote Machines</h2>
          <p className="text-sm text-tertiary mt-0.5">
            Remote Machines paired by an admin for platform-wide agent execution.
          </p>
        </div>
        <Button
          variant="primary"
          className="self-start sm:self-auto shrink-0"
          onClick={() => { setShowPairModal(true); setPairResult(null); setPairName(''); setPairAllowFullFs(false); setPairError('') }}
        >
          Pair New Machine
        </Button>
      </div>

      {adminMachines.length === 0 && (
        <div className={`${CARD} p-8 text-center`}>
          <p className="text-tertiary">No platform remote machines paired yet.</p>
          <p className="text-sm text-tertiary mt-1">
            Click "Pair New Machine" to connect a remote machine.
          </p>
        </div>
      )}

      {adminMachines.map((m: RemoteMachine) => (
        <div key={m.id} className={CARD}>
          {/* Header row */}
          <div
            className="flex items-center justify-between gap-2 p-4 cursor-pointer hover:bg-surface-hover rounded-card"
            onClick={() => setExpandedId(expandedId === m.id ? null : m.id)}
          >
            <div className="flex items-center gap-3 min-w-0">
              <RemoteBadge
                state={m.status ?? null}
                machineName={m.name}
                lastSeenIso={m.last_seen}
                heartbeatAgeS={m.last_heartbeat_age_s ?? null}
              />
              <div className="min-w-0">
                <p className="font-medium text-primary truncate">{m.name}</p>
                <p className="text-xs text-tertiary truncate">{m.id.slice(0, 8)} · {m.capabilities?.os || 'unknown'}</p>
              </div>
            </div>
            <div className="flex items-center gap-2 sm:gap-3 shrink-0">
              <span className={`${BADGE_BASE} ${STATUS_COLORS[m.status] ?? STATUS_COLORS.never_connected}`}>
                {m.status}
              </span>
              {m.assigned_agents.length > 0 && (
                <span className="hidden sm:inline text-xs text-tertiary whitespace-nowrap">
                  {m.assigned_agents.length} agent{m.assigned_agents.length !== 1 ? 's' : ''}
                </span>
              )}
              <svg className={`w-4 h-4 text-tertiary shrink-0 transition-transform ${expandedId === m.id ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </div>
          </div>

          {/* Expanded details */}
          {expandedId === m.id && (
            <div className="border-t border-subtle p-4 space-y-4">
              {/* Capabilities */}
              <div>
                <p className="text-xs font-medium text-tertiary mb-1">Capabilities</p>
                <div className="flex flex-wrap gap-2">
                  {m.capabilities.os && (
                    <span className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}>
                      {m.capabilities.os} {m.capabilities.arch}
                    </span>
                  )}
                  {(m.capabilities.installed_clis ?? []).map(cli => {
                    const info = cliChipInfo(cli, m.capabilities.cli_status, m.cli_pins, cliBinaries)
                    return (
                      <span
                        key={cli}
                        title={info.title}
                        className={`${BADGE_BASE} ${BADGE_TONE[info.mismatch ? 'warning' : 'info']}`}
                      >
                        {info.label}
                      </span>
                    )
                  })}
                  {/* The engines this satellite runs (its `engines` capability,
                      0.5.124+), named through the catalog. An older satellite
                      advertises none and shows no chips. */}
                  {(m.capabilities.engines ?? []).map(engine => (
                    <span
                      key={`engine:${engine}`}
                      title={`Runs ${engineNames[engine] ?? engine} sessions`}
                      className={`${BADGE_BASE} ${BADGE_TONE.neutral}`}
                    >
                      {engineNames[engine] ?? engine}
                    </span>
                  ))}
                </div>
                {m.capabilities.os_user && (
                  <p className="text-xs text-tertiary mt-1">
                    Running as {m.capabilities.os_user}
                    {m.capabilities.home_dir && ` (${m.capabilities.home_dir})`}
                  </p>
                )}
                {m.last_seen && (
                  <p className="text-xs text-tertiary mt-1">
                    Last seen: {new Date(m.last_seen).toLocaleString()}
                  </p>
                )}
              </div>

              {/* Assigned agents */}
              <div>
                <p className="text-xs font-medium text-tertiary mb-1">Assigned Agents</p>
                <div className="flex flex-wrap gap-2">
                  {m.assigned_agents.map(slug => (
                    <span key={slug} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-chip text-xs bg-surface-muted text-primary">
                      {slug}
                      <button
                        onClick={(e) => { e.stopPropagation(); handleUnassign(m.id, slug) }}
                        className="text-tertiary hover:text-status-danger-fg ml-0.5"
                      >x</button>
                    </span>
                  ))}
                  {/* Add agent dropdown */}
                  <Select compact
                    className="max-w-48"
                    value=""
                    onChange={e => { if (e.target.value) handleAssign(m.id, e.target.value) }}
                  >
                    <option value="">+ Add agent</option>
                    {eligibleAgents
                      .filter(a => !m.assigned_agents.includes(a.name))
                      // A satellite that says which engines it runs (0.5.124+)
                      // gets only agents on one of them; an older one is offered
                      // every eligible agent, as before.
                      .filter(a => !m.capabilities.engines || m.capabilities.engines.includes(a.execution_path))
                      .map(a => (
                        <option key={a.name} value={a.name}>{a.display_name}</option>
                      ))}
                  </Select>
                </div>
              </div>

              {/* Live capacity + per-machine max_sessions override */}
              <MachineCapacityControls machine={m} stat={statByMachine.get(m.id)} />

              {/* Auto-update controls */}
              <MachineUpdateControls machine={m} />

              {/* Filesystem-access policy */}
              <MachineFsPolicyControls machine={m} />

              {/* Device-control consent */}
              <MachineDeviceGrantsControls machine={m} />

              {/* Danger zone */}
              <div className="pt-2 border-t border-subtle">
                {deleteConfirm === m.id ? (
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-status-danger-fg">Remove this machine?</span>
                    <Button variant="danger" size="xs" onClick={() => { void handleDelete(m.id).catch(toastError('The machine was not removed')) }}>
                      Confirm
                    </Button>
                    <Button size="xs" onClick={() => setDeleteConfirm(null)}>
                      Cancel
                    </Button>
                  </div>
                ) : (
                  <button
                    onClick={() => setDeleteConfirm(m.id)}
                    className="text-xs text-status-danger-fg hover:underline"
                  >
                    Remove Machine
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      ))}

      {/* User-Paired Remote Machines — an overview, collapsed by default,
          of ALL user-paired machines regardless of status so admins can
          audit who paired what. The one action is Remove (the same
          self-uninstall cascade as the owner's own Remove, with a confirm
          that names the owner); assignment, filesystem scope and device
          grants stay with the registering user. */}
      <div className={CARD}>
        <button
          type="button"
          onClick={() => setShowUserPaired(v => !v)}
          className="w-full flex items-center justify-between p-4 text-left hover:bg-surface-hover rounded-card"
        >
          <div>
            <h3 className="text-base font-semibold text-primary">
              User-Paired Remote Machines{' '}
              <span className="text-tertiary font-normal">({userMachines.length})</span>
            </h3>
            <p className="text-xs text-tertiary mt-0.5">
              Machines that users paired themselves, in User Settings, Remote Machines. An admin can remove one. Everything else stays with its owner.
            </p>
          </div>
          <svg
            className={`w-4 h-4 text-tertiary transition-transform ${showUserPaired ? 'rotate-180' : ''}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>
        {showUserPaired && (
          <div className="border-t border-subtle">
            {userMachines.length === 0 ? (
              <p className="p-4 text-sm text-tertiary">
                No users have paired their own machines yet.
              </p>
            ) : (
              <div className="divide-y divide-subtle">
                {userMachines.map(m => (
                  <div key={m.id} className="flex items-center gap-3 p-3">
                    <RemoteBadge
                      state={m.status ?? null}
                      machineName={m.name}
                      lastSeenIso={m.last_seen}
                      heartbeatAgeS={m.last_heartbeat_age_s ?? null}
                      size="xs"
                    />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-primary truncate">{m.name}</p>
                      <p className="text-xs text-tertiary truncate">
                        Owner: {m.owner_display_name || 'Deleted user'}
                        {m.owner_email ? ` <${m.owner_email}>` : ''}
                        {' · '}
                        {m.capabilities?.os || 'unknown'}
                      </p>
                    </div>
                    <span className={`${BADGE_BASE} ${STATUS_COLORS[m.status] ?? STATUS_COLORS.never_connected} shrink-0`}>
                      {m.status}
                    </span>
                    <span className="hidden sm:inline text-xs text-tertiary w-44 text-right truncate">
                      {m.last_seen
                        ? `Last seen ${new Date(m.last_seen).toLocaleString()}`
                        : 'Never connected'}
                    </span>
                    {/* Icon-only on phones, "Remove" from sm: — the row
                        must never push the page wider than the screen. */}
                    <button
                      type="button"
                      onClick={() => { setRemoveError(''); setRemoveUserMachine(m) }}
                      title="Remove this machine"
                      aria-label={`Remove ${m.name}`}
                      className="shrink-0 inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-control border border-status-danger-border text-status-danger-fg hover:bg-status-danger-bg"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                      <span className="hidden sm:inline">Remove</span>
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Remove a user-paired machine — confirm first. Stacked buttons on
          phones, a row from sm:. */}
      {removeUserMachine && (
        <AlertDialog
          title={`Remove ${removeUserMachine.name}?`}
          confirmLabel="Remove machine"
          tone="danger"
          busy={deleteMachine.isPending}
          busyLabel="Removing…"
          error={removeError || null}
          onCancel={() => setRemoveUserMachine(null)}
          onConfirm={() => {
            const id = removeUserMachine.id
            void handleDelete(id)
              .then(() => setRemoveUserMachine(null))
              .catch((e: unknown) => setRemoveError(errorText(e) || 'The machine was not removed.'))
          }}
        >
          This machine was paired by{' '}
          <span className="font-medium text-primary">
            {removeUserMachine.owner_display_name || 'a deleted user'}
            {removeUserMachine.owner_email ? ` <${removeUserMachine.owner_email}>` : ''}
          </span>.
          OtoDock on their machine uninstalls itself. An offline
          machine does it the next time it connects. From their next turn,
          agents pinned to it run where they run by default. Chats keep
          their history. A resumed chat starts with fresh context. The
          owner is notified.
        </AlertDialog>
      )}

      {/* Pair Modal */}
      {showPairModal && (
        <Dialog
          onClose={() => setShowPairModal(false)}
          aria-label={pairResult ? 'Machine paired' : 'Pair a new machine'}
          // The install command is shown once: a stray click must not lose it.
          dismissible={pairResult ? 'escape' : 'any'}
          busy={pairMachine.isPending}
          panelClassName={`${DIALOG_PANEL} p-6 max-w-lg`}
        >
            {pairResult ? (
              <div className="space-y-4">
                <h3 className="font-display text-lg font-semibold text-primary">Machine Paired</h3>
                <PairInstallCommand
                  pairResult={pairResult}
                  introText="Run this command on the remote machine to complete setup:"
                />
                <div className="bg-status-warning-bg border border-status-warning-border rounded-card p-3">
                  <p className="text-xs text-status-warning-fg">
                    The pairing token in this command expires in {pairResult.expires_in_hours} hour(s). It works only once.
                  </p>
                </div>
                <Button
                  variant="primary"
                  size="md"
                  className="w-full"
                  onClick={() => setShowPairModal(false)}
                >
                  Done
                </Button>
              </div>
            ) : (
              <div className="space-y-4">
                <h3 className="font-display text-lg font-semibold text-primary">Pair New Machine</h3>
                <div>
                  <label className="block text-sm font-medium text-primary mb-1">Machine Name</label>
                  <Input
                    autoFocus
                    value={pairName}
                    onChange={e => setPairName(e.target.value)}
                    placeholder="For example, dev-laptop or prod-server"
                  />
                </div>
                <CheckboxRow
                  label="Allow full filesystem access"
                  checked={pairAllowFullFs}
                  onChange={setPairAllowFullFs}
                  description="The home folder limit is a guardrail, not full isolation. Only pair machines you trust with the agent."
                  info={<>
                    When on, agents can read and write any path the OS user can reach.
                    That includes system files and services. The machine's own OtoDock folder
                    stays off limits. When off, agents stay inside the OS user's home
                    folder. Either way, agents run as the OS user.
                  </>}
                />
                {pairError && (
                  <p className="text-sm text-status-danger-fg">{pairError}</p>
                )}
                <div className="flex gap-2 justify-end">
                  <Button onClick={() => setShowPairModal(false)}>
                    Cancel
                  </Button>
                  <Button
                    variant="primary"
                    onClick={handlePair}
                    disabled={!pairName.trim() || pairMachine.isPending}
                  >
                    {pairMachine.isPending ? 'Pairing...' : 'Generate Pairing Token'}
                  </Button>
                </div>
              </div>
            )}
        </Dialog>
      )}
    </div>
  )
}
