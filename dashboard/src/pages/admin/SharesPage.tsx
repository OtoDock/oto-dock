import { useMemo, useState } from 'react'
import { useAdminShares, usePatchShare, type AdminShare } from '../../api/shares'
import { useAgents } from '../../api/agents'
import { formatExpiry, formatRelativeTime } from '../../lib/format'

/**
 * Admin → Shares (SHARING.md): every external link on the platform, who
 * made it, what it opens, how it is protected, when it was last opened,
 * and a revoke. Internal grants are the sharer's business and stay in the
 * app's own share popover.
 */
export default function SharesPage() {
  const { data, isLoading, error } = useAdminShares()
  const { data: agents } = useAgents({ all: true })
  const patch = usePatchShare()
  const [agentFilter, setAgentFilter] = useState('')
  const [actionError, setActionError] = useState('')

  const agentName = useMemo(() => {
    const map: Record<string, string> = {}
    for (const a of agents || []) map[a.name] = a.display_name || a.name
    return map
  }, [agents])

  const rows = (data ?? []).filter((s) => !agentFilter || s.agent === agentFilter)

  const protection = (s: AdminShare) => {
    if (s.public) return 'public'
    return 'password'
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-bold text-p-text">Shares</h1>
        <p className="text-sm text-p-text-secondary mt-1">
          External links members made. Revoking a link closes it at once; the switches that allow links at all live under Setup → Security.
        </p>
      </div>
      <div className="flex gap-3 items-center flex-wrap">
        <select value={agentFilter} onChange={(e) => setAgentFilter(e.target.value)}
          aria-label="Filter by agent"
          className="text-sm border border-p-border-light rounded-sm px-2 py-1 bg-white dark:bg-p-surface text-p-text">
          <option value="">All agents</option>
          {(agents || []).map((a) => <option key={a.name} value={a.name}>{a.display_name || a.name}</option>)}
        </select>
        <span className="text-xs text-p-text-light">{rows.length} link{rows.length === 1 ? '' : 's'}</span>
      </div>
      {actionError && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{actionError}</p>}
      {isLoading ? (
        <p className="text-sm text-p-text-light">Loading…</p>
      ) : error ? (
        <p className="text-sm text-red-600 dark:text-red-400">{(error as Error).message}</p>
      ) : !rows.length ? (
        <p className="text-sm text-p-text-light">No external links.</p>
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
              {rows.map((s) => (
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
                    <button type="button"
                      onClick={() => { setActionError(''); patch.mutate({ id: s.id, revoke: true }, { onError: (e) => setActionError(e instanceof Error ? e.message : 'The link was not revoked') }) }}
                      className="rounded-md px-2 py-0.5 text-xs text-red-600 hover:bg-red-500/10 dark:text-red-400">
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
