import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  useRemoteMachines, usePairMachine, useDeleteMachine, useAssignAgent, useUnassignAgent,
  useSetAutoUpdate, useTriggerUpdateNow, useSetAllowFullFs, useSetDeviceGrants,
  useSetMaxSessions,
  DEVICE_CAPABILITY_INFO,
  type RemoteMachine, type PairResult,
} from '../../api/remoteMachines'
import { PAIRING_SCOPE } from '../../lib/placement'
import { MACHINE_STATE, type MachineState } from '../../lib/status/machine'
import { useAgents, useExecutionLayers } from '../../api/agents'
import { apiFetch } from '../../api/auth'
import { useAuth } from '../../contexts/AuthContext'
import { hasAgentScope, modeOfAgent } from '../../lib/visibility'
import { engineLabels, installedCliBinaries, runsRemote } from '../../lib/engines'
import { cliChipInfo } from '../../lib/cliChip'
import RemoteBadge from '../../components/RemoteBadge'
import PairInstallCommand from '../../components/PairInstallCommand'
import {
  BrowserModeSelect, BrowserTokenField, browserModeDesc, browserModeOf,
} from '../../components/BrowserModeControls'

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
      const res = await apiFetch('/v1/admin/concurrency-stats')
      if (!res.ok) throw new Error('Failed to fetch')
      return res.json()
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
  // Local edit buffer seeded from the persisted override (null → empty).
  const [value, setValue] = useState(
    machine.max_sessions != null ? String(machine.max_sessions) : '',
  )
  useEffect(() => {
    setValue(machine.max_sessions != null ? String(machine.max_sessions) : '')
  }, [machine.max_sessions])

  const persisted = machine.max_sessions != null ? String(machine.max_sessions) : ''
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
    setMaxSessions.mutate({ machineId: machine.id, value: parsed })
  }

  const sessionsLabel = stat
    ? `${stat.active_sessions} / ${stat.max_sessions ?? '∞'}`
    : '—'

  return (
    <div className="pt-2 border-t border-p-border-light space-y-2">
      <p className="text-xs font-medium text-p-text-light">Capacity</p>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5">
          <p className="text-xs text-p-text">
            Active sessions: <span className="font-mono">{sessionsLabel}</span>
            {stat && (
              <span className="ml-2 text-p-text-light">
                CPU <span className="font-mono">{Math.round(stat.cpu_pct)}%</span>
                {' · '}
                Mem <span className="font-mono">{Math.round(stat.mem_pct)}%</span>
              </span>
            )}
          </p>
          <p className="text-[10px] text-p-text-light max-w-md">
            Max concurrent sessions the proxy will route here. Blank = the
            satellite's own recommendation{recommended != null ? ` (${recommended})` : ''}.
            The satellite also hard-caps at its physical max.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <label className="text-xs text-p-text-light">Max sessions</label>
          <input
            type="number"
            min={1}
            value={value}
            placeholder={recommended != null ? `Auto (${recommended})` : 'Auto'}
            onChange={e => setValue(e.target.value)}
            onBlur={save}
            disabled={setMaxSessions.isPending}
            className="w-24 px-2 py-1.5 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30 text-right disabled:opacity-50"
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
  online: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  stale: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  paused: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  disconnected: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  never_connected: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
}

// Per-machine update controls — version + auto-update toggle +
// manual "Update now" button. Rendered inside the expanded machine row.
function MachineUpdateControls({ machine }: { machine: RemoteMachine }) {
  const setAutoUpdate = useSetAutoUpdate()
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
    <div className="pt-2 border-t border-p-border-light space-y-2">
      <p className="text-xs font-medium text-p-text-light">Updates</p>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5 min-w-0">
          <p className="text-xs text-p-text">
            Satellite version: <span className="font-mono">{version}</span>
            {pending && (
              <span className="ml-2 px-1.5 py-0.5 rounded-sm text-[10px] bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400">
                update queued for next reconnect
              </span>
            )}
          </p>
          {updateError && (
            <p className="text-xs text-red-600 dark:text-red-400">
              Last update failed: {updateError}
            </p>
          )}
          {autoPaused && (
            <p className="text-xs text-amber-700 dark:text-amber-400" data-testid="auto-update-paused">
              Automatic updates paused — the update to{' '}
              <span className="font-mono">{rollbackTarget}</span> rolled back{' '}
              {rollbackCount} times. Click "Update now" to retry.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <label className="inline-flex items-center gap-1.5 text-xs text-p-text cursor-pointer">
            <input
              type="checkbox"
              checked={autoEnabled}
              disabled={setAutoUpdate.isPending}
              onChange={e => setAutoUpdate.mutate({
                machine_id: machine.id,
                enabled: e.target.checked,
              })}
              className="rounded-sm"
            />
            Auto-update
          </label>
          <button
            type="button"
            onClick={() => triggerUpdate.mutate(machine.id)}
            disabled={triggerUpdate.isPending}
            className="px-2 py-1 text-xs font-medium rounded-sm border border-p-border-light text-p-text hover:bg-p-surface disabled:opacity-50"
            title={
              machine.status === MACHINE_STATE.ONLINE
                ? 'Push the latest satellite tarball now'
                : 'Mark machine for forced update on next reconnect'
            }
          >
            {triggerUpdate.isPending ? 'Triggering…' : 'Update now'}
          </button>
        </div>
      </div>
      {!autoEnabled && (
        <p className="text-[10px] text-p-text-light">
          Auto-update disabled — this satellite will be rejected if the proxy
          requires a newer version. Click "Update now" to force a push.
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
  const allowFullFs = machine.allow_full_fs ?? false
  return (
    <div className="pt-2 border-t border-p-border-light space-y-2">
      <p className="text-xs font-medium text-p-text-light">Filesystem access</p>
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-col gap-0.5 max-w-md">
          <p className="text-xs text-p-text">
            {allowFullFs
              ? "Full filesystem access. Agents can read and write any path the OS user can reach, except the machine's own OtoDock folder."
              : "Home folder only. Agents are limited to the agent tree and the OS user's home folder, a guardrail, not a sandbox."}
          </p>
        </div>
        <label className="inline-flex items-center gap-1.5 text-xs text-p-text cursor-pointer">
          <input
            type="checkbox"
            checked={allowFullFs}
            disabled={setAllowFullFs.isPending}
            onChange={e => setAllowFullFs.mutate({
              machineId: machine.id,
              enabled: e.target.checked,
            })}
            className="rounded-sm"
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
  const granted = new Set(machine.device_grants ?? [])
  const noDisplay = machine.capabilities?.display?.has_display === false
  const toggle = (key: string, label: string, on: boolean) => {
    if (on && !window.confirm(
      `Grant "${label}" on ${machine.name}?\n\n` +
      'This lets assigned agents drive real input/output on this machine — it ' +
      'can click system/sudo prompts and use a browser with saved logins. Only ' +
      'enable on a machine you trust for this.',
    )) return
    const next = new Set(granted)
    if (on) { next.add(key) } else { next.delete(key) }
    setDeviceGrants.mutate({ machineId: machine.id, grants: [...next] })
  }
  return (
    <div className="pt-2 border-t border-p-border-light space-y-2">
      <p className="text-xs font-medium text-p-text-light">Device control</p>
      <p className="text-[10px] text-p-text-light max-w-md">
        Let assigned agents control this machine’s screen, browser or apps. Off
        by default.
      </p>
      {noDisplay && (
        <p className="text-[10px] text-amber-600 max-w-md">
          This machine reported no interactive display — computer/browser
          control won’t work until it has a GUI session.
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
              <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-p-text">
                <label className="inline-flex items-center gap-1.5 cursor-pointer whitespace-nowrap">
                  <input
                    type="checkbox"
                    checked={on}
                    disabled={setDeviceGrants.isPending}
                    onChange={e => toggle(cap.key, cap.label, e.target.checked)}
                    className="rounded-sm"
                  />
                  <span className="font-medium">{cap.label}</span>
                </label>
                {isBrowser && on && <BrowserModeSelect machine={machine} scope="admin" />}
                <span className="text-p-text-light">— {desc}</span>
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
  const { user } = useAuth()
  const { data: machines, isLoading } = useRemoteMachines()
  const { data: satelliteStats } = useSatelliteStats()
  const { data: agents } = useAgents({ all: true })

  // This build ships without the remote-machines feature (the nav entry is
  // already hidden — this covers a direct URL). Evaluated after the hooks
  // so hook order stays stable.
  const featureAvailable = user?.feature_flags?.remote_machines_available !== false

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
        <div className="rounded-lg border border-p-border bg-p-surface p-4 text-sm text-p-text-secondary">
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
    } catch (e: any) {
      setPairError(e.message)
    }
  }

  const handleDelete = async (id: string) => {
    await deleteMachine.mutateAsync(id)
    setDeleteConfirm(null)
    setExpandedId(null)
  }

  const handleAssign = async (machineId: string, agentSlug: string) => {
    try {
      await assignAgent.mutateAsync({ machineId, agentSlug })
    } catch (e: any) {
      alert(e.message)
    }
  }

  const handleUnassign = async (machineId: string, agentSlug: string) => {
    await unassignAgent.mutateAsync({ machineId, agentSlug })
  }

  // Agents eligible for remote assignment: their engine must run on a
  // satellite (the descriptor's `runtime.supports_remote_execution` — an
  // in-process engine has nothing to relocate) and they must have a shared
  // (agent) workspace to run in — a remote machine runs the agent scope, so
  // Personal-only agents (no shared space) are excluded. A machine that
  // advertises the engines it can run narrows the list further (below).
  const { data: layers } = useExecutionLayers()
  const engineNames = engineLabels(layers)
  const cliBinaries = installedCliBinaries(layers)
  const eligibleAgents = (agents ?? []).filter(
    a => runsRemote(layers?.[a.execution_path]) && hasAgentScope(modeOfAgent(a))
  )

  if (isLoading) return <div className="p-6 text-p-text-light">Loading...</div>

  // Split admin-paired (this section is fully managed here) from
  // user-paired (read-only observability section below).
  const adminMachines = (machines ?? []).filter(m => m.pairing_scope !== PAIRING_SCOPE.USER)
  const userMachines = (machines ?? []).filter(m => m.pairing_scope === PAIRING_SCOPE.USER)

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-p-text">Platform Remote Machines</h2>
          <p className="text-sm text-p-text-light mt-0.5">
            Remote Machines paired by an admin for platform-wide agent execution.
          </p>
        </div>
        <button
          onClick={() => { setShowPairModal(true); setPairResult(null); setPairName(''); setPairAllowFullFs(false); setPairError('') }}
          className="self-start sm:self-auto shrink-0 px-3 py-1.5 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover"
        >
          Pair New Machine
        </button>
      </div>

      {adminMachines.length === 0 && (
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-8 text-center">
          <p className="text-p-text-light">No platform remote machines paired yet.</p>
          <p className="text-sm text-p-text-light mt-1">
            Click "Pair New Machine" to connect a remote machine.
          </p>
        </div>
      )}

      {adminMachines.map((m: RemoteMachine) => (
        <div key={m.id} className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
          {/* Header row */}
          <div
            className="flex items-center justify-between gap-2 p-4 cursor-pointer hover:bg-p-surface-hover rounded-xl"
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
                <p className="font-medium text-p-text truncate">{m.name}</p>
                <p className="text-xs text-p-text-light truncate">{m.id.slice(0, 8)} · {m.capabilities?.os || 'unknown'}</p>
              </div>
            </div>
            <div className="flex items-center gap-2 sm:gap-3 shrink-0">
              <span className={`inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${STATUS_COLORS[m.status] ?? STATUS_COLORS.never_connected}`}>
                {m.status}
              </span>
              {m.assigned_agents.length > 0 && (
                <span className="hidden sm:inline text-xs text-p-text-light whitespace-nowrap">
                  {m.assigned_agents.length} agent{m.assigned_agents.length !== 1 ? 's' : ''}
                </span>
              )}
              <svg className={`w-4 h-4 text-p-text-light shrink-0 transition-transform ${expandedId === m.id ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </div>
          </div>

          {/* Expanded details */}
          {expandedId === m.id && (
            <div className="border-t border-p-border-light p-4 space-y-4">
              {/* Capabilities */}
              <div>
                <p className="text-xs font-medium text-p-text-light mb-1">Capabilities</p>
                <div className="flex flex-wrap gap-2">
                  {m.capabilities.os && (
                    <span className="px-2 py-0.5 rounded-sm text-xs bg-p-surface text-p-text">
                      {m.capabilities.os} {m.capabilities.arch}
                    </span>
                  )}
                  {(m.capabilities.installed_clis ?? []).map(cli => {
                    const info = cliChipInfo(cli, m.capabilities.cli_status, m.cli_pins, cliBinaries)
                    return (
                      <span
                        key={cli}
                        title={info.title}
                        className={`px-2 py-0.5 rounded-sm text-xs ${info.mismatch
                          ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
                          : 'bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400'}`}
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
                      className="px-2 py-0.5 rounded-sm text-xs bg-p-surface text-p-text"
                    >
                      {engineNames[engine] ?? engine}
                    </span>
                  ))}
                </div>
                {m.capabilities.os_user && (
                  <p className="text-xs text-p-text-light mt-1">
                    Running as {m.capabilities.os_user}
                    {m.capabilities.home_dir && ` (${m.capabilities.home_dir})`}
                  </p>
                )}
                {m.last_seen && (
                  <p className="text-xs text-p-text-light mt-1">
                    Last seen: {new Date(m.last_seen).toLocaleString()}
                  </p>
                )}
              </div>

              {/* Assigned agents */}
              <div>
                <p className="text-xs font-medium text-p-text-light mb-1">Assigned Agents</p>
                <div className="flex flex-wrap gap-2">
                  {m.assigned_agents.map(slug => (
                    <span key={slug} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-sm text-xs bg-p-surface text-p-text">
                      {slug}
                      <button
                        onClick={(e) => { e.stopPropagation(); handleUnassign(m.id, slug) }}
                        className="text-p-text-light hover:text-red-500 ml-0.5"
                      >x</button>
                    </span>
                  ))}
                  {/* Add agent dropdown */}
                  <select
                    className="px-2 py-0.5 text-xs border border-p-border-light rounded-sm bg-p-bg text-p-text"
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
                  </select>
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
              <div className="pt-2 border-t border-p-border-light">
                {deleteConfirm === m.id ? (
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-red-600">Remove this machine?</span>
                    <button
                      onClick={() => handleDelete(m.id)}
                      className="px-2 py-1 text-xs font-medium rounded-sm bg-red-600 text-white hover:bg-red-700"
                    >
                      Confirm
                    </button>
                    <button
                      onClick={() => setDeleteConfirm(null)}
                      className="px-2 py-1 text-xs font-medium rounded-sm border border-p-border-light text-p-text hover:bg-p-surface"
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => setDeleteConfirm(m.id)}
                    className="text-xs text-red-600 hover:text-red-700"
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
      <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
        <button
          type="button"
          onClick={() => setShowUserPaired(v => !v)}
          className="w-full flex items-center justify-between p-4 text-left hover:bg-p-surface-hover rounded-xl"
        >
          <div>
            <h3 className="text-base font-semibold text-p-text">
              User-Paired Remote Machines{' '}
              <span className="text-p-text-light font-normal">({userMachines.length})</span>
            </h3>
            <p className="text-xs text-p-text-light mt-0.5">
              Machines paired by individual users via User Settings → Remote Machines. An admin can remove one; everything else stays with its owner.
            </p>
          </div>
          <svg
            className={`w-4 h-4 text-p-text-light transition-transform ${showUserPaired ? 'rotate-180' : ''}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>
        {showUserPaired && (
          <div className="border-t border-p-border-light">
            {userMachines.length === 0 ? (
              <p className="p-4 text-sm text-p-text-light">
                No users have paired their own machines yet.
              </p>
            ) : (
              <div className="divide-y divide-p-border-light">
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
                      <p className="text-sm font-medium text-p-text truncate">{m.name}</p>
                      <p className="text-xs text-p-text-light truncate">
                        Owner: {m.owner_display_name || '(deleted user)'}
                        {m.owner_email ? ` <${m.owner_email}>` : ''}
                        {' · '}
                        {m.capabilities?.os || 'unknown'}
                      </p>
                    </div>
                    <span className={`shrink-0 inline-flex items-center px-2 py-0.5 rounded-sm text-xs font-medium ${STATUS_COLORS[m.status] ?? STATUS_COLORS.never_connected}`}>
                      {m.status}
                    </span>
                    <span className="hidden sm:inline text-xs text-p-text-light w-44 text-right truncate">
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
                      className="shrink-0 inline-flex items-center gap-1 px-2 py-1 text-xs font-medium rounded-lg border border-red-200 dark:border-red-900/50 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
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
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setRemoveUserMachine(null)}>
          <div
            role="dialog"
            aria-labelledby="remove-user-machine-title"
            className="bg-white dark:bg-p-surface rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl space-y-4"
            onClick={e => e.stopPropagation()}
          >
            <h3 id="remove-user-machine-title" className="text-lg font-semibold text-p-text">
              Remove {removeUserMachine.name}?
            </h3>
            <p className="text-sm text-p-text-secondary">
              This machine was paired by{' '}
              <span className="font-medium text-p-text">
                {removeUserMachine.owner_display_name || '(deleted user)'}
                {removeUserMachine.owner_email ? ` <${removeUserMachine.owner_email}>` : ''}
              </span>.
              The satellite will uninstall itself from their machine (an offline
              one the next time it connects). Their agents pinned to it run
              where those agents run by default from their next turn — chats
              keep their history, a resumed one starts with fresh context. The
              owner is notified.
            </p>
            {removeError && <p className="text-sm text-red-600">{removeError}</p>}
            <div className="flex flex-col-reverse sm:flex-row gap-2 sm:justify-end">
              <button
                onClick={() => setRemoveUserMachine(null)}
                className="px-3 py-2 sm:py-1.5 text-sm font-medium rounded-lg border border-p-border-light text-p-text hover:bg-p-surface"
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  const id = removeUserMachine.id
                  try {
                    await handleDelete(id)
                    setRemoveUserMachine(null)
                  } catch (e: any) {
                    setRemoveError(e?.message || 'Failed to remove the machine')
                  }
                }}
                disabled={deleteMachine.isPending}
                className="px-3 py-2 sm:py-1.5 text-sm font-medium rounded-lg bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
              >
                {deleteMachine.isPending ? 'Removing…' : 'Remove machine'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Pair Modal */}
      {showPairModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowPairModal(false)}>
          <div className="bg-white dark:bg-p-surface rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
            {pairResult ? (
              <div className="space-y-4">
                <h3 className="text-lg font-semibold text-p-text">Machine Paired</h3>
                <PairInstallCommand
                  pairResult={pairResult}
                  introText="Run this command on the remote machine to complete setup:"
                />
                <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-3">
                  <p className="text-xs text-amber-700 dark:text-amber-400">
                    This token expires in {pairResult.expires_in_hours} hour(s). It can only be used once.
                  </p>
                </div>
                <button
                  onClick={() => setShowPairModal(false)}
                  className="w-full px-3 py-2 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover"
                >
                  Done
                </button>
              </div>
            ) : (
              <div className="space-y-4">
                <h3 className="text-lg font-semibold text-p-text">Pair New Machine</h3>
                <div>
                  <label className="block text-sm font-medium text-p-text mb-1">Machine Name</label>
                  <input
                    value={pairName}
                    onChange={e => setPairName(e.target.value)}
                    placeholder="e.g. dev-laptop, prod-server"
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30"
                  />
                </div>
                <label className="flex items-start gap-2 text-sm text-p-text cursor-pointer">
                  <input
                    type="checkbox"
                    checked={pairAllowFullFs}
                    onChange={e => setPairAllowFullFs(e.target.checked)}
                    className="mt-0.5 rounded-sm"
                  />
                  <span>
                    <span className="font-medium">Allow full filesystem access</span>
                    <span className="block text-xs text-p-text-light">
                      When enabled, agents on this machine can read and write any path the
                      OS user can reach (system files, services), except the machine's own
                      OtoDock folder, which no session may touch. When disabled, agents stay
                      inside the OS user's home folder. Agents run natively as the OS user
                      either way: the home folder setting is a guardrail, not a kernel
                      sandbox, so only pair machines you trust with the agent.
                    </span>
                  </span>
                </label>
                {pairError && (
                  <p className="text-sm text-red-600">{pairError}</p>
                )}
                <div className="flex gap-2 justify-end">
                  <button
                    onClick={() => setShowPairModal(false)}
                    className="px-3 py-1.5 text-sm font-medium rounded-lg border border-p-border-light text-p-text hover:bg-p-surface"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handlePair}
                    disabled={!pairName.trim() || pairMachine.isPending}
                    className="px-3 py-1.5 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-50"
                  >
                    {pairMachine.isPending ? 'Pairing...' : 'Generate Pairing Token'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
