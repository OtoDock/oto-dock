import { useLayoutEffect, useRef, useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useFlag } from '../hooks/useFlag'
import { roleBadge, type AgentRole } from '../lib/permissions'
import { useMyRemoteMachines, usePairMyMachine, useDeleteMyMachine, useSetMyRemoteTarget, useRemoveMyRemoteTarget, useSetMyAllowFullFs, useSetMyDeviceGrants, DEVICE_CAPABILITY_INFO, deviceGrantBody, type PairResult } from '../api/remoteMachines'
import { PAIRING_SCOPE } from '../lib/placement'
import RemoteBadge from '../components/RemoteBadge'
import PairInstallCommand from '../components/PairInstallCommand'
import { BrowserModeSelect, BrowserTokenField } from '../components/BrowserModeControls'
import { browserModeDesc, browserModeOf } from '../lib/remote/browserMode'
import { errorText, toastError } from '../lib/http/apiJson'
import { useDialogs } from '../components/ui/dialogs'
import Dialog from '../components/ui/Dialog'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'
import { BADGE_BASE, BADGE_TONE, CARD, DIALOG_PANEL } from '../components/ui/classes'
import InfoTip from '../components/ui/InfoTip'
import { CheckboxRow, SaveState } from '../components/ui/SettingsControls'
import { useSaveState } from '../hooks/useSaveState'

export function MyMachinesSection() {
  const dialogs = useDialogs()
  const { user } = useAuth()
  const { data, isLoading } = useMyRemoteMachines()
  const pairMachine = usePairMyMachine()
  const deleteMachine = useDeleteMyMachine()
  const setTarget = useSetMyRemoteTarget()
  const removeTarget = useRemoveMyRemoteTarget()
  const setAllowFullFs = useSetMyAllowFullFs()
  const { stateOf, run } = useSaveState()
  const setDeviceGrants = useSetMyDeviceGrants()

  const [showPair, setShowPair] = useState(false)
  const [pairResult, setPairResult] = useState<PairResult | null>(null)
  const [pairName, setPairName] = useState('')
  const [pairAllowFullFs, setPairAllowFullFs] = useState(false)  // user default
  const [pairError, setPairError] = useState('')
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null)
  // Which machine pills are expanded. Tracked in a Set at the section level so
  // we don't need a hook per row (hooks can't be called inside the .map below).
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const toggleExpanded = (id: string) =>
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) { next.delete(id) } else { next.add(id) }
      return next
    })

  // If the admin disabled user-paired machines, replace the whole
  // section with a clear "disabled by admin" message so the user doesn't see
  // ghost pair/select controls that'd just 403. Evaluated AFTER the hooks above
  // (it gates the render, not the hook calls) so hook order stays stable.
  const allowed = useFlag('allow_user_paired_machines')

  const machines = data?.machines ?? []
  const machinesRef = useRef(machines)
  useLayoutEffect(() => { machinesRef.current = machines })
  const targets = data?.targets ?? []
  // Per-agent targeting. Map agent_slug → machine_id so we can
  // render checkbox state in O(1) per row.
  const targetByAgent = new Map<string, string>()
  for (const t of targets) {
    if (t.agent_slug) targetByAgent.set(t.agent_slug, t.machine_id)
  }
  // User's agents (intersect user_agents). Sorted for stable UI ordering.
  const userAgentEntries: [string, AgentRole][] = Object.entries(user?.agent_roles ?? {})
    .sort((a, b) => a[0].localeCompare(b[0])) as [string, AgentRole][]

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


  if (!allowed) {
    return (
      <div className={`${CARD} p-6 text-center`}>
        <p className="text-sm text-tertiary">
          Remote machines are disabled by your administrator.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header: short description + Pair button. On mobile the button drops
          below the text (stacked) rather than crowding the right edge. */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between mb-4">
        <p className="text-sm text-secondary">
          Connect your own machine and run agents on it with OtoDock Satellite.
        </p>
        <Button
          variant="primary"
          className="self-start sm:self-auto shrink-0"
          onClick={() => { setShowPair(true); setPairResult(null); setPairName(''); setPairAllowFullFs(false); setPairError('') }}
        >
          Pair Machine
        </Button>
      </div>

      {isLoading ? (
        <div className="text-sm text-tertiary">Loading...</div>
      ) : machines.length === 0 ? (
        <div className={`${CARD} text-sm text-tertiary p-6 text-center`}>
          No machines paired. Click "Pair Machine" to connect your computer.
        </div>
      ) : (
        <div className="space-y-3">
          {machines.map(m => {
            const isAdminPaired = m.pairing_scope === PAIRING_SCOPE.ADMIN
            const expanded = expandedIds.has(m.id)
            return (
            <div key={m.id} className={CARD}>
              {/* Collapsed pill header — toggles the detail panel (mirrors the
                  Integrations / AI-engine cards). The RemoteBadge dot stops its
                  own click, so tapping the status dot won't toggle the row. */}
              <button
                className="w-full flex items-center justify-between gap-3 p-4 text-left"
                onClick={() => toggleExpanded(m.id)}
              >
                <div className="flex items-center gap-3 min-w-0">
                  <RemoteBadge
                    state={m.status ?? null}
                    machineName={m.name}
                    lastSeenIso={m.last_seen}
                    heartbeatAgeS={m.last_heartbeat_age_s ?? null}
                  />
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-medium text-primary truncate">{m.name}</p>
                      {isAdminPaired && (
                        <span
                          className={`${BADGE_BASE} ${BADGE_TONE.info}`}
                          title="Paired by an admin for the whole platform. An admin removes it in Admin, Remote Machines."
                        >
                          Platform
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-tertiary truncate">
                      {m.id.slice(0, 8)} · {m.status}
                      {m.capabilities?.os_user ? ` · ${m.capabilities.os_user}` : ''}
                    </p>
                  </div>
                </div>
                <svg
                  className={`w-4 h-4 shrink-0 text-tertiary transition-transform ${expanded ? 'rotate-180' : ''}`}
                  fill="none" viewBox="0 0 24 24" stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>

              {expanded && (
                <div className="px-4 pb-1 border-t border-subtle divide-y divide-subtle">
                  {/* Remove — user-owned machines only (admin-paired ones are
                      managed from the admin Remote Machines page). */}
                  {!isAdminPaired && (
                    <div className="py-3 flex justify-end">
                      {deleteConfirm === m.id ? (
                        <div className="flex items-center gap-1">
                          <Button
                            variant="danger"
                            size="xs"
                            onClick={() => { deleteMachine.mutateAsync(m.id).then(() => setDeleteConfirm(null)).catch(toastError('The machine was not removed')) }}
                          >Confirm</Button>
                          <Button size="xs" onClick={() => setDeleteConfirm(null)}>Cancel</Button>
                        </div>
                      ) : (
                        <button onClick={() => setDeleteConfirm(m.id)}
                          className="text-xs text-status-danger-fg hover:underline">Remove machine</button>
                      )}
                    </div>
                  )}

              {/* Per-machine filesystem-access policy. Only
                  shown for user-owned machines (admin-paired machines
                  are managed from the admin Remote Machines page). */}
              {!isAdminPaired && (
                <div className="py-3">
                  <div className="flex items-center justify-between gap-3 flex-wrap">
                    <div className="flex flex-col gap-0.5 max-w-md">
                      <p className="text-xs font-medium text-tertiary">
                        Filesystem access
                        <InfoTip label="Filesystem access">
                          {(m.allow_full_fs ?? false)
                            ? "The machine's own OtoDock folder stays off limits."
                            : 'Agents stay in their agent folders and your OS home folder.'}
                        </InfoTip>
                        <SaveState state={stateOf(`${m.id}:fs`)} />
                      </p>
                      <p className="text-xs text-primary">
                        {(m.allow_full_fs ?? false)
                          ? 'Full filesystem access. Agents can read and write any path your OS user can reach.'
                          : 'Home folder only. This is a guardrail, not full isolation.'}
                      </p>
                    </div>
                    <label className="inline-flex items-center gap-1.5 text-xs text-primary cursor-pointer">
                      <input
                        type="checkbox"
                        checked={m.allow_full_fs ?? false}
                        disabled={setAllowFullFs.isPending}
                        onChange={e => {
                          const enabled = e.target.checked
                          void run(`${m.id}:fs`, () => setAllowFullFs.mutateAsync({ machineId: m.id, enabled }),
                            { failTitle: 'The filesystem access was not changed' })
                        }}
                        className="accent-action"
                      />
                      Allow full FS
                    </label>
                  </div>
                </div>
              )}

              {/* Device-control consent (owner-only,
                  user-paired machines). Off by default; each capability is a
                  separate, revocable grant — strictly more powerful than
                  full-FS, so enabling asks for confirmation. */}
              {!isAdminPaired && (
                <div className="py-3">
                  <p className="text-xs font-medium text-tertiary mb-1">
                    Device control
                    <SaveState state={stateOf(`${m.id}:grants`)} />
                  </p>
                  {m.capabilities?.display?.has_display === false && (
                    <p className="text-3xs text-status-warning-fg max-w-md mb-2">
                      This computer reported no screen. Computer and browser control
                      work once someone is signed in to its desktop.
                    </p>
                  )}
                  <div className="flex flex-col gap-1">
                    {DEVICE_CAPABILITY_INFO.map(cap => {
                      const on = (m.device_grants ?? []).includes(cap.key)
                      const isBrowser = cap.key === 'browser'
                      // The Browser-control row carries its mode selector while
                      // granted, and its description follows the selected mode.
                      const desc = isBrowser && on ? browserModeDesc(browserModeOf(m)) : cap.desc
                      return (
                        <div key={cap.key} className="flex flex-col">
                          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-primary">
                            <label className="inline-flex items-center gap-1.5 cursor-pointer whitespace-nowrap">
                              <input
                                type="checkbox"
                                checked={on}
                                disabled={setDeviceGrants.isPending}
                                onChange={async e => {
                                  const next_on = e.target.checked
                                  if (next_on && !(await dialogs.confirm({
                                    title: `Grant "${cap.label}" on ${m.name}?`,
                                    body: deviceGrantBody(cap, false),
                                    confirmLabel: 'Grant',
                                  }))) return
                                  // The grants as they are now: a refresh may have landed while the dialog was open.
                                  const current = machinesRef.current.find(x => x.id === m.id) ?? m
                                  const next = new Set(current.device_grants ?? [])
                                  if (next_on) { next.add(cap.key) } else { next.delete(cap.key) }
                                  void run(`${m.id}:grants`, () => setDeviceGrants.mutateAsync({ machineId: m.id, grants: [...next] }),
                                    { failTitle: `The ${cap.label} permission was not changed` })
                                }}
                                className="accent-action"
                              />
                              <span className="font-medium">{cap.label}</span>
                            </label>
                            <InfoTip label={cap.label}>{desc}</InfoTip>
                            {isBrowser && on && <BrowserModeSelect machine={m} scope="me" />}
                          </div>
                          {/* Own mode's token block belongs to this row, not to the list. */}
                          {isBrowser && on && <BrowserTokenField machine={m} scope="me" />}
                        </div>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* Per-agent selection: which of MY agents should run on THIS machine? */}
              <div className="py-3">
                <p className="text-xs font-medium text-tertiary mb-2">
                  Run these agents on this machine
                  <SaveState state={stateOf(`${m.id}:agents`)} />
                </p>
                {userAgentEntries.length === 0 ? (
                  <p className="text-xs text-tertiary italic">
                    No agents assigned to you yet. Ask an admin to add you to an agent.
                  </p>
                ) : (
                  <div className="space-y-1">
                    {userAgentEntries.map(([slug, role]) => {
                      const targetMachine = targetByAgent.get(slug)
                      const onThisMachine = targetMachine === m.id
                      const onOtherMachine = targetMachine && targetMachine !== m.id
                      const otherName = onOtherMachine
                        ? machines.find(mm => mm.id === targetMachine)?.name ?? targetMachine?.slice(0, 8)
                        : null
                      return (
                        <label key={slug} className="flex items-center gap-2 text-sm py-1 cursor-pointer hover:bg-surface-hover rounded-control px-2">
                          <input
                            type="checkbox"
                            checked={onThisMachine}
                            disabled={setTarget.isPending || removeTarget.isPending}
                            onChange={(e) => {
                              if (e.target.checked) {
                                void run(`${m.id}:agents`, () => setTarget.mutateAsync({ agent_slug: slug, machine_id: m.id }),
                                  { failTitle: 'The agent was not moved to this machine' })
                              } else {
                                void run(`${m.id}:agents`, () => removeTarget.mutateAsync(slug),
                                  { failTitle: 'The agent was not taken off this machine' })
                              }
                            }}
                            className="h-4 w-4 accent-action"
                          />
                          <span className="font-mono text-xs">{slug}</span>
                          <span className={`${BADGE_BASE} ${roleBadge(role)}`}>
                            {role}
                          </span>
                          {onOtherMachine && (
                            <span className="text-xs text-status-warning-fg">
                              Currently on {otherName}
                            </span>
                          )}
                        </label>
                      )
                    })}
                  </div>
                )}
              </div>
                </div>
              )}
            </div>
            )
          })}
        </div>
      )}

      {/* Pair modal */}
      {showPair && (
        <Dialog
          onClose={() => setShowPair(false)}
          aria-label={pairResult ? 'Machine paired' : 'Pair a new machine'}
          // The install command is shown once: a stray click must not lose it.
          dismissible={pairResult ? 'escape' : 'any'}
          busy={pairMachine.isPending}
          panelClassName={`${DIALOG_PANEL} max-w-lg p-6`}
        >
            {pairResult ? (
              <div className="space-y-4">
                <h3 className="font-display text-lg font-semibold text-primary">Machine Paired</h3>
                <PairInstallCommand
                  pairResult={pairResult}
                  introText="Run this on the remote machine:"
                />
                <p className="text-xs text-status-warning-fg">The pairing token in this command expires in {pairResult.expires_in_hours} hour(s). It works only once.</p>
                <Button variant="primary" size="md" className="w-full" onClick={() => setShowPair(false)}>Done</Button>
              </div>
            ) : (
              <div className="space-y-4">
                <h3 className="font-display text-lg font-semibold text-primary">Pair New Machine</h3>
                <div>
                  <label className="block text-sm font-medium text-primary mb-1">Machine Name</label>
                  <Input autoFocus value={pairName} onChange={e => setPairName(e.target.value)} placeholder="e.g. my-laptop" />
                </div>
                <CheckboxRow
                  label="Allow full filesystem access"
                  checked={pairAllowFullFs}
                  onChange={setPairAllowFullFs}
                  description="The home folder limit is a guardrail, not full isolation. Only pair machines you trust."
                  info={<>
                    By default, agents can only read and write files in your home folder.
                    Turn this on if agents should manage system services or edit files
                    outside your home. They can then reach any path your OS account can.
                    The machine's own OtoDock folder stays off limits. Either way, agents
                    run as your OS user. You can change this later.
                  </>}
                />
                {pairError && <p className="text-sm text-status-danger-fg">{pairError}</p>}
                <div className="flex gap-2 justify-end">
                  <Button onClick={() => setShowPair(false)}>Cancel</Button>
                  <Button
                    variant="primary"
                    onClick={handlePair}
                    disabled={!pairName.trim() || pairMachine.isPending}
                  >
                    {pairMachine.isPending ? 'Pairing...' : 'Pair'}
                  </Button>
                </div>
              </div>
            )}
        </Dialog>
      )}
    </div>
  )
}
