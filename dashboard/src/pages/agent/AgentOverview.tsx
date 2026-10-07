import { useParams } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'
import { allowedOnSharedOnly, canManageAgent, isAdmin, roleBadge } from '../../lib/permissions'
import { useAgentInfo, useAgentUsers } from '../../api/agents'
import { useRuns } from '../../api/runs'
import { MODE_LABEL, MODE_SUMMARY, isSharedOnly, modeOfAgent } from '../../lib/visibility'
import GroupedRunsTable from '../../components/GroupedRunsTable'

export default function AgentOverview() {
  const { name } = useParams<{ name: string }>()
  const { user } = useAuth()
  const { data: info, isLoading: infoLoading } = useAgentInfo(name!)
  const canManage = canManageAgent(user, name || '')
  const { data: recentData } = useRuns({ agent: name, limit: 20 })

  if (infoLoading) return <p className="text-sm text-p-text-secondary">Loading...</p>

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold text-p-text">{info?.display_name || name}</h1>
        {info?.display_name && info.display_name !== name && (
          <p className="text-sm text-p-text-light mt-0.5">{name}</p>
        )}
      </div>

      {/* Description */}
      {info?.description && (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
          <p className="text-xs font-semibold text-p-text-secondary uppercase mb-2">Description</p>
          <p className="text-sm text-p-text">{info.description}</p>
        </div>
      )}

      {/* MCPs */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
        <p className="text-xs font-semibold text-p-text-secondary uppercase mb-2">MCP Tools</p>
        {info && info.mcps.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {info.mcps.map((mcp) => (
              <span
                key={mcp}
                className="inline-flex items-center px-2.5 py-1 rounded-sm text-xs bg-brand-50 text-brand"
              >
                {mcp}
              </span>
            ))}
          </div>
        ) : (
          <p className="text-sm text-p-text-light">No MCPs configured.</p>
        )}
      </div>

      {/* Visibility — managers/admins only. Shows the agent's current mode. */}
      {canManage && info && (
        <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
          <p className="text-xs font-semibold text-p-text-secondary uppercase mb-2">Visibility</p>
          <div className="space-y-1.5 text-sm">
            <span className="inline-flex items-center px-2 py-0.5 rounded-sm text-xs bg-p-surface text-p-text-secondary">
              {MODE_LABEL[modeOfAgent(info)]}
            </span>
            <p className="text-xs text-p-text-light">{MODE_SUMMARY[modeOfAgent(info)]}</p>
          </div>
        </div>
      )}

      {/* Users — managers/admins only. Who's attached to this agent + their role. */}
      {canManage && <AgentUsersCard name={name!} sharedOnly={!!info && isSharedOnly(modeOfAgent(info))} />}

      {/* Recent Activity */}
      <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
        <p className="text-xs font-semibold text-p-text-secondary uppercase mb-3">Recent Activity</p>
        <GroupedRunsTable runs={recentData?.runs ?? []} showAgent={false} maxGroups={5} />
      </div>

    </div>
  )
}

function AgentUsersCard({ name, sharedOnly }: { name: string; sharedOnly: boolean }) {
  const { data: users, isLoading } = useAgentUsers(name)
  return (
    <div className="bg-white dark:bg-p-surface rounded-xl border border-p-border-light p-4">
      <p className="text-xs font-semibold text-p-text-secondary uppercase mb-3">
        Users{users && users.length > 0 ? ` (${users.length})` : ''}
      </p>
      {isLoading ? (
        <p className="text-sm text-p-text-light">Loading…</p>
      ) : !users || users.length === 0 ? (
        <p className="text-sm text-p-text-light">No users are assigned to this agent yet.</p>
      ) : (
        <ul className="space-y-2">
          {users.map((u) => (
            <li key={u.sub} className="flex items-center gap-3">
              <span className="w-7 h-7 rounded-full shrink-0 flex items-center justify-center text-white text-[11px] font-bold bg-slate-400 dark:bg-slate-600">
                {(u.name || u.email || '?').charAt(0).toUpperCase()}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-sm text-p-text truncate">{u.name}</p>
                {u.email && <p className="text-xs text-p-text-light truncate">{u.email}</p>}
              </div>
              {/* An older install may hold a lower row on a Shared-only
                  agent: it opens no chat there, and an admin raises or removes it. */}
              {sharedOnly && !allowedOnSharedOnly(u.role) && !isAdmin(u.platform_role) && (
                <span className="text-[10px] text-amber-600 shrink-0"
                      title="Shared only takes the editor role or above: this assignment opens no chat. An admin raises it or removes it (Admin, Users).">
                  no chat access
                </span>
              )}
              <span className={`px-1.5 py-0.5 rounded-sm text-xs font-medium ${roleBadge(u.role)}`}>
                {u.role}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
