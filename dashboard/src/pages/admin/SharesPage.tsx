import { useMemo, useState } from 'react'
import {
  ADMIN_SHARE_KIND, GRANTEE_KIND, SHARE_SCOPE, SHARE_STANDING, SHARE_TARGET,
  useAdminShares, usePatchShare, type AdminShare, type AdminShareKind, type ShareStanding,
} from '../../api/shares'
import { useAgents } from '../../api/agents'
import { roleLabel } from '../../lib/permissions'
import { formatExpiry, formatRelativeTime } from '../../lib/format'

/**
 * Admin → Shares (SHARING.md): every share on the platform, the newest 500
 * of each kind, filtered by kind, agent and standing on the server. The
 * shares to people, agents and departments (what, whose agent, to whom, the
 * role, the standing, who shared it and when) and the external links (who
 * made them, what they open, how they are protected, when last opened).
 * An admin may revoke any share and nobody is notified; a share that still
 * gives access asks first and names who loses it.
 */

const STANDINGS: { value: ShareStanding; label: string }[] = [
  { value: SHARE_STANDING.LIVE, label: 'Live' },
  { value: SHARE_STANDING.WAITING, label: 'Waiting' },
  { value: SHARE_STANDING.PAUSED, label: 'Paused' },
  { value: SHARE_STANDING.EXPIRED, label: 'Expired' },
  { value: SHARE_STANDING.DECLINED, label: 'Declined' },
]

/** Who a share went to, by kind. */
function recipient(s: AdminShare): string {
  if (s.to_department) return `Department: ${s.to_department.name || s.to_department.id}`
  if (s.to_agent) return `Agent: ${s.to_agent.name}`
  return s.grantee?.name || s.grantee?.username || s.grantee?.sub || ''
}

/** What the row says about a share's standing. */
function standingText(s: AdminShare): string {
  switch (s.standing) {
    case SHARE_STANDING.DECLINED: return s.decided_by_name ? `declined by ${s.decided_by_name}` : 'declined'
    case SHARE_STANDING.EXPIRED: return 'expired'
    case SHARE_STANDING.PAUSED: return 'paused while the app is unpinned'
    case SHARE_STANDING.WAITING: return 'waiting to be accepted'
    default: {
      const word = s.grantee_kind !== GRANTEE_KIND.PERSON ? 'placed' : s.target_kind === SHARE_TARGET.APP ? 'accepted' : 'shared'
      return [word, formatExpiry(s.expires_at)].filter(Boolean).join(' · ')
    }
  }
}

const targetWord = (s: AdminShare) => (s.target_kind === SHARE_TARGET.CHAT ? 'a chat' : 'an app')

/** Who loses what when a share that gives access, or would again once its
 * app is pinned, is revoked. */
function revokeConsequence(s: AdminShare): string {
  const title = s.title || targetWord(s)
  const dept = s.to_department ? s.to_department.name || s.to_department.id : ''
  if (s.standing === SHARE_STANDING.PAUSED) {
    if (dept) return `“${title}” will not come back to the agents of the ${dept} department when the app is pinned again.`
    if (s.to_agent) return `“${title}” will not come back to ${s.to_agent.name}'s apps when the app is pinned again.`
    return `${recipient(s) || 'The person it went to'} will not get “${title}” back when the app is pinned again.`
  }
  if (dept) return `“${title}” leaves every agent of the ${dept} department.`
  if (s.to_agent) return `“${title}” leaves ${s.to_agent.name}'s apps for everyone there.`
  const who = recipient(s) || 'The person it went to'
  if (s.target_kind === SHARE_TARGET.CHAT) return `${who} loses the shared copy of “${title}”.`
  return `${who} loses access to “${title}”.`
}

/** A declined or expired share gives no access: its revoke only clears the row. */
const asksFirst = (s: AdminShare) => s.standing !== SHARE_STANDING.DECLINED && s.standing !== SHARE_STANDING.EXPIRED

export default function SharesPage() {
  const [kind, setKind] = useState<AdminShareKind>(ADMIN_SHARE_KIND.ALL)
  const [agentFilter, setAgentFilter] = useState('')
  const [standing, setStanding] = useState<ShareStanding | ''>('')
  const { data, isLoading, isPlaceholderData, error } = useAdminShares({ kind, agent: agentFilter, standing })
  const { data: agents } = useAgents({ all: true })
  const patch = usePatchShare()
  const [actionError, setActionError] = useState('')
  const [confirmId, setConfirmId] = useState<string | null>(null)

  const agentName = useMemo(() => {
    const map: Record<string, string> = {}
    for (const a of agents || []) map[a.name] = a.display_name || a.name
    return map
  }, [agents])

  const shares = data?.shares ?? []
  const internal = shares.filter((s) => s.scope === SHARE_SCOPE.INTERNAL)
  const links = shares.filter((s) => s.scope === SHARE_SCOPE.EXTERNAL)
  const cut = !!(data?.truncated.internal || data?.truncated.external)
  // The previous filter's answer stays on screen, dimmed, while the next
  // loads; its empty lines would be false, so they wait for the answer.
  const stale = isPlaceholderData

  const revoke = (s: AdminShare) => {
    setActionError('')
    setConfirmId(null)
    patch.mutate({ id: s.id, revoke: true }, { onError: (e) => setActionError(e instanceof Error ? e.message : 'The share was not revoked') })
  }

  const protection = (s: AdminShare) => {
    if (s.public) return 'public'
    return 'password'
  }

  const select = 'text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text'
  const revokeButton = 'rounded-md px-2 py-0.5 text-xs text-red-600 hover:bg-red-500/10 dark:text-red-400'

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-bold text-p-text">Shares</h1>
        <p className="text-sm text-p-text-secondary mt-1">
          Every share on the platform, to people, agents and departments or by link. Revoke ends a share at once and tells nobody. Sharing is switched on and off under Setup → Security.
        </p>
      </div>
      <div className="flex gap-3 items-center flex-wrap">
        <select value={kind} onChange={(e) => setKind(e.target.value as AdminShareKind)} aria-label="Filter by kind" className={select}>
          <option value={ADMIN_SHARE_KIND.ALL}>All shares</option>
          <option value={ADMIN_SHARE_KIND.INTERNAL}>People, agents and departments</option>
          <option value={ADMIN_SHARE_KIND.EXTERNAL}>External links</option>
        </select>
        <select value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)} aria-label="Filter by agent" className={select}>
          <option value="">All agents</option>
          {(agents || []).map((a) => <option key={a.name} value={a.name}>{a.display_name || a.name}</option>)}
        </select>
        <select value={standing} onChange={(e) => setStanding(e.target.value as ShareStanding | '')} aria-label="Filter by standing" className={select}>
          <option value="">Any standing</option>
          {STANDINGS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <span className="text-xs text-p-text-light">{shares.length} share{shares.length === 1 ? '' : 's'}</span>
      </div>
      {cut && (
        <p className="text-xs text-p-text-secondary" data-testid="admin-shares-cut">
          Showing the newest 500 of each kind. Narrow the filters to reach older ones.
        </p>
      )}
      {actionError && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{actionError}</p>}
      {isLoading ? (
        <p className="text-sm text-p-text-light">Loading…</p>
      ) : error ? (
        <p className="text-sm text-red-600 dark:text-red-400">{(error as Error).message}</p>
      ) : (
        <div className={stale ? 'space-y-4 opacity-60' : 'space-y-4'} aria-busy={stale}>
          {kind !== ADMIN_SHARE_KIND.EXTERNAL && (
            <section className="space-y-2">
              <h2 className="text-sm font-semibold text-p-text">Shared inside the platform</h2>
              {!internal.length ? (
                !stale && <p className="text-sm text-p-text-light">No shares to people, agents or departments.</p>
              ) : (
                <div className="overflow-x-auto rounded-xl border border-p-border-light">
                  <table className="w-full text-sm">
                    <thead className="bg-p-surface text-left text-xs uppercase tracking-wide text-p-text-light">
                      <tr>
                        <th className="px-3 py-2">What</th>
                        <th className="px-3 py-2">Agent</th>
                        <th className="px-3 py-2">To</th>
                        <th className="px-3 py-2">Role</th>
                        <th className="px-3 py-2">Standing</th>
                        <th className="px-3 py-2">By</th>
                        <th className="px-3 py-2">Shared</th>
                        <th className="px-3 py-2" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-p-border-light/60">
                      {internal.map((s) => (
                        <InternalRow key={s.id} s={s} agentName={agentName[s.agent] || s.agent}
                          confirming={confirmId === s.id} asks={asksFirst(s)}
                          onRevoke={() => (asksFirst(s) ? setConfirmId(s.id) : revoke(s))}
                          onConfirm={() => revoke(s)} onCancel={() => setConfirmId(null)}
                          revokeButton={revokeButton} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
          {kind !== ADMIN_SHARE_KIND.INTERNAL && (
            <section className="space-y-2">
              <h2 className="text-sm font-semibold text-p-text">External links</h2>
              {!links.length ? (
                !stale && <p className="text-sm text-p-text-light">No external links.</p>
              ) : (
                <div className="overflow-x-auto rounded-xl border border-p-border-light">
                  <table className="w-full text-sm">
                    <thead className="bg-p-surface text-left text-xs uppercase tracking-wide text-p-text-light">
                      <tr>
                        <th className="px-3 py-2">What</th>
                        <th className="px-3 py-2">Agent</th>
                        <th className="px-3 py-2">By</th>
                        <th className="px-3 py-2">Protection</th>
                        <th className="px-3 py-2">Buttons</th>
                        <th className="px-3 py-2">Opened</th>
                        <th className="px-3 py-2">Expires</th>
                        <th className="px-3 py-2" />
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-p-border-light/60">
                      {links.map((s) => (
                        <tr key={s.id} className="text-p-text">
                          <td className="px-3 py-2">
                            <span className="font-medium">{s.title || 'an app'}</span>
                            <span className="ml-1 text-xs text-p-text-light">{s.target_kind}</span>
                            {s.state === 'suspended' && <span className="ml-1 text-xs text-amber-600">paused</span>}
                          </td>
                          <td className="px-3 py-2 text-p-text-secondary">{agentName[s.agent] || s.agent}</td>
                          <td className="px-3 py-2 text-p-text-secondary">{s.created_by_name || s.created_by}</td>
                          <td className="px-3 py-2">
                            <span className={protection(s) === 'public' ? 'rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-700 dark:text-amber-300' : 'text-xs text-p-text-secondary'}>
                              {protection(s)}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-xs text-p-text-secondary">{s.allow_actions ? 'on' : 'off'}</td>
                          <td className="px-3 py-2 text-xs text-p-text-secondary">
                            {s.access_count ? `${s.access_count}× · ${s.last_access_at ? formatRelativeTime(s.last_access_at) : ''}` : 'never'}
                          </td>
                          <td className="px-3 py-2 text-xs text-p-text-secondary">{s.expires_at ? formatExpiry(s.expires_at) : 'never'}</td>
                          <td className="px-3 py-2 text-right">
                            <button type="button" onClick={() => revoke(s)} className={revokeButton}>
                              Revoke
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
        </div>
      )}
    </div>
  )
}

function InternalRow({ s, agentName, confirming, asks, onRevoke, onConfirm, onCancel, revokeButton }: {
  s: AdminShare; agentName: string; confirming: boolean; asks: boolean
  onRevoke: () => void; onConfirm: () => void; onCancel: () => void; revokeButton: string
}) {
  return (
    <>
      <tr className="text-p-text">
        <td className="px-3 py-2">
          <span className="font-medium">{s.title || targetWord(s)}</span>
          <span className="ml-1 text-xs text-p-text-light">{s.target_kind}</span>
        </td>
        <td className="px-3 py-2 text-p-text-secondary">{agentName}</td>
        <td className="px-3 py-2">{recipient(s)}</td>
        <td className="px-3 py-2 text-xs text-p-text-secondary">{s.target_kind === SHARE_TARGET.CHAT ? 'read-only' : roleLabel(s.role_cap).toLowerCase()}</td>
        <td className={`px-3 py-2 text-xs ${s.standing === SHARE_STANDING.LIVE ? 'text-p-text-secondary' : 'text-amber-600 dark:text-amber-400'}`}>{standingText(s)}</td>
        <td className="px-3 py-2 text-p-text-secondary">{s.created_by_name || s.created_by}</td>
        <td className="px-3 py-2 text-xs text-p-text-secondary">{s.created_at ? formatRelativeTime(s.created_at) : ''}</td>
        <td className="px-3 py-2 text-right">
          <button type="button" onClick={onRevoke} className={revokeButton} aria-expanded={asks ? confirming : undefined}>
            Revoke
          </button>
        </td>
      </tr>
      {confirming && (
        <tr>
          <td colSpan={8} className="px-3 pb-2.5">
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-p-border-light bg-p-surface px-3 py-2.5 text-xs" data-testid="admin-revoke-confirm">
              <span className="text-p-text">{revokeConsequence(s)} Nobody is notified.</span>
              <div className="ml-auto flex shrink-0 items-center gap-2">
                <button type="button" onClick={onConfirm}
                  className="rounded-md bg-red-600 px-2.5 py-1 font-medium text-white transition-colors hover:bg-red-700">
                  Revoke the share
                </button>
                <button type="button" onClick={onCancel}
                  className="rounded-md border border-p-border-light px-2.5 py-1 font-medium text-p-text-secondary transition-colors hover:bg-p-surface-hover">
                  Cancel
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
