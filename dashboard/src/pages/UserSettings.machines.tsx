import { useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useMyRemoteMachines, usePairMyMachine, useDeleteMyMachine, useSetMyRemoteTarget, useRemoveMyRemoteTarget, useSetMyAllowFullFs, useSetMyDeviceGrants, DEVICE_CAPABILITY_INFO, type PairResult } from '../api/remoteMachines'
import RemoteBadge from '../components/RemoteBadge'
import PairInstallCommand from '../components/PairInstallCommand'

export function MyMachinesSection() {
  const { user } = useAuth()
  const { data, isLoading } = useMyRemoteMachines()
  const pairMachine = usePairMyMachine()
  const deleteMachine = useDeleteMyMachine()
  const setTarget = useSetMyRemoteTarget()
  const removeTarget = useRemoveMyRemoteTarget()
  const setAllowFullFs = useSetMyAllowFullFs()
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
  const allowed = user?.feature_flags?.allow_user_paired_machines !== false

  const machines = data?.machines ?? []
  const targets = data?.targets ?? []
  // Per-agent targeting. Map agent_slug → machine_id so we can
  // render checkbox state in O(1) per row.
  const targetByAgent = new Map<string, string>()
  for (const t of targets) {
    if (t.agent_slug) targetByAgent.set(t.agent_slug, t.machine_id)
  }
  // User's agents (intersect user_agents). Sorted for stable UI ordering.
  const userAgentEntries: [string, 'manager' | 'editor' | 'viewer'][] = Object.entries(user?.agent_roles ?? {})
    .sort((a, b) => a[0].localeCompare(b[0])) as [string, 'manager' | 'editor' | 'viewer'][]

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


  if (!allowed) {
    return (
      <div className="border border-p-border-light rounded-xl p-6 text-center">
        <p className="text-sm text-p-text-light">
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
        <p className="text-sm text-p-text-secondary">
          Connect your own machine to run agents locally via the OtoDock Satellite.
        </p>
        <button
          onClick={() => { setShowPair(true); setPairResult(null); setPairName(''); setPairAllowFullFs(false); setPairError('') }}
          className="self-start sm:self-auto shrink-0 px-3 py-1.5 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover"
        >
          Pair Machine
        </button>
      </div>

      {isLoading ? (
        <div className="text-sm text-p-text-light">Loading...</div>
      ) : machines.length === 0 ? (
        <div className="text-sm text-p-text-light border border-p-border-light rounded-xl p-6 text-center">
          No machines paired. Click "Pair Machine" to connect your computer.
        </div>
      ) : (
        <div className="space-y-3">
          {machines.map(m => {
            const isAdminPaired = m.pairing_scope === 'admin'
            const expanded = expandedIds.has(m.id)
            return (
            <div key={m.id} className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface">
              {/* Collapsed pill header — toggles the detail panel (mirrors the
                  Integrations / AI-engine cards). The RemoteBadge dot stops its
                  own click, so tapping the status dot won't toggle the row. */}
              <button
                className="w-full flex items-center justify-between gap-3 p-4 text-left"
                onClick={() => toggleExpanded(m.id)}
              >
                <div className="flex items-center gap-3 min-w-0">
                  <RemoteBadge
                    state={(m.status as any) ?? null}
                    machineName={m.name}
                    lastSeenIso={m.last_seen}
                    heartbeatAgeS={m.last_heartbeat_age_s ?? null}
                  />
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-medium text-p-text truncate">{m.name}</p>
                      {isAdminPaired && (
                        <span
                          className="px-1.5 py-0.5 rounded-sm text-[10px] font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                          title="Platform-wide machine paired by an admin. Delete from the admin Remote Machines page."
                        >
                          Platform
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-p-text-light truncate">
                      {m.id.slice(0, 8)} — {m.status}
                      {m.capabilities?.os_user ? ` · ${m.capabilities.os_user}` : ''}
                    </p>
                  </div>
                </div>
                <svg
                  className={`w-4 h-4 shrink-0 text-p-text-light transition-transform ${expanded ? 'rotate-180' : ''}`}
                  fill="none" viewBox="0 0 24 24" stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>

              {expanded && (
                <div className="px-4 pb-1 border-t border-p-border-light divide-y divide-p-border-light">
                  {/* Remove — user-owned machines only (admin-paired ones are
                      managed from the admin Remote Machines page). */}
                  {!isAdminPaired && (
                    <div className="py-3 flex justify-end">
                      {deleteConfirm === m.id ? (
                        <div className="flex items-center gap-1">
                          <button onClick={() => { deleteMachine.mutate(m.id); setDeleteConfirm(null) }}
                            className="px-2 py-1 text-xs font-medium rounded-sm bg-red-600 text-white">Confirm</button>
                          <button onClick={() => setDeleteConfirm(null)}
                            className="px-2 py-1 text-xs font-medium rounded-sm border border-p-border-light text-p-text">Cancel</button>
                        </div>
                      ) : (
                        <button onClick={() => setDeleteConfirm(m.id)}
                          className="text-xs text-red-600 hover:text-red-700">Remove machine</button>
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
                      <p className="text-xs font-medium text-p-text-light">
                        Filesystem access
                      </p>
                      <p className="text-xs text-p-text">
                        {(m.allow_full_fs ?? false)
                          ? 'Full filesystem access — agents can read/write any path your OS user can reach.'
                          : 'Home-only — agents are limited to the agent tree and your OS home directory.'}
                      </p>
                    </div>
                    <label className="inline-flex items-center gap-1.5 text-xs text-p-text cursor-pointer">
                      <input
                        type="checkbox"
                        checked={m.allow_full_fs ?? false}
                        disabled={setAllowFullFs.isPending}
                        onChange={e => setAllowFullFs.mutate({
                          machineId: m.id,
                          enabled: e.target.checked,
                        })}
                        className="rounded-sm"
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
                  <p className="text-xs font-medium text-p-text-light mb-1">
                    Device control
                  </p>
                  {m.capabilities?.display?.has_display === false && (
                    <p className="text-[10px] text-amber-600 max-w-md mb-2">
                      This machine reported no interactive display —
                      computer/browser control won’t work until it has a GUI session.
                    </p>
                  )}
                  <div className="flex flex-col gap-1">
                    {DEVICE_CAPABILITY_INFO.map(cap => (
                      <label
                        key={cap.key}
                        className="inline-flex items-center gap-1.5 text-xs text-p-text cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={(m.device_grants ?? []).includes(cap.key)}
                          disabled={setDeviceGrants.isPending}
                          onChange={e => {
                            const on = e.target.checked
                            if (on && !window.confirm(
                              `Grant "${cap.label}" on ${m.name}?\n\n` +
                              'This lets your agents drive real input/output on this ' +
                              'machine — it can click system/sudo prompts and use a ' +
                              'browser with your saved logins.',
                            )) return
                            const next = new Set(m.device_grants ?? [])
                            if (on) { next.add(cap.key) } else { next.delete(cap.key) }
                            setDeviceGrants.mutate({ machineId: m.id, grants: [...next] })
                          }}
                          className="rounded-sm"
                        />
                        <span className="font-medium">{cap.label}</span>
                        <span className="text-p-text-light">— {cap.desc}</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}

              {/* Per-agent selection: which of MY agents should run on THIS machine? */}
              <div className="py-3">
                <p className="text-xs font-medium text-p-text-light mb-2">
                  Run these agents on this machine
                </p>
                {userAgentEntries.length === 0 ? (
                  <p className="text-xs text-p-text-light italic">
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
                        <label key={slug} className="flex items-center gap-2 text-sm py-1 cursor-pointer hover:bg-p-surface rounded-sm px-2">
                          <input
                            type="checkbox"
                            checked={onThisMachine}
                            disabled={setTarget.isPending || removeTarget.isPending}
                            onChange={(e) => {
                              if (e.target.checked) {
                                setTarget.mutate({ agent_slug: slug, machine_id: m.id })
                              } else {
                                removeTarget.mutate(slug)
                              }
                            }}
                            className="h-4 w-4 rounded-sm text-brand focus:ring-brand"
                          />
                          <span className="font-mono text-xs">{slug}</span>
                          <span className={`text-xs px-1.5 py-0.5 rounded-sm ${
                            role === 'manager' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400' :
                            role === 'editor' ? 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400' :
                            'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400'
                          }`}>
                            {role}
                          </span>
                          {onOtherMachine && (
                            <span className="text-xs text-amber-600">
                              currently on {otherName}
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
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setShowPair(false)}>
          <div className="bg-white dark:bg-p-surface rounded-xl p-6 w-full max-w-lg mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
            {pairResult ? (
              <div className="space-y-4">
                <h3 className="text-lg font-semibold text-p-text">Machine Paired</h3>
                <PairInstallCommand
                  pairResult={pairResult}
                  introText="Run this on the remote machine:"
                />
                <p className="text-xs text-amber-600">Token expires in {pairResult.expires_in_hours} hour(s). Single use only.</p>
                <button onClick={() => setShowPair(false)} className="w-full px-3 py-2 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover">Done</button>
              </div>
            ) : (
              <div className="space-y-4">
                <h3 className="text-lg font-semibold text-p-text">Pair New Machine</h3>
                <div>
                  <label className="block text-sm font-medium text-p-text mb-1">Machine Name</label>
                  <input value={pairName} onChange={e => setPairName(e.target.value)} placeholder="e.g. my-laptop"
                    className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-p-bg text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
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
                      By default agents on your machine can only read/write files under your
                      home directory. Enable this if you want agents to manage system services
                      or edit files outside your home — they'll be able to touch any path your
                      OS account can reach. Agents run natively as your OS user (this is a scope
                      guardrail, not a kernel sandbox), so only pair machines you trust. You can
                      change this later.
                    </span>
                  </span>
                </label>
                {pairError && <p className="text-sm text-red-600">{pairError}</p>}
                <div className="flex gap-2 justify-end">
                  <button onClick={() => setShowPair(false)} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-p-border-light text-p-text hover:bg-p-surface">Cancel</button>
                  <button onClick={handlePair} disabled={!pairName.trim() || pairMachine.isPending}
                    className="px-3 py-1.5 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover disabled:opacity-50">
                    {pairMachine.isPending ? 'Pairing...' : 'Pair'}
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
