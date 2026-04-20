import { useState, Fragment } from 'react'
import {
  useAdminUsageOverview,
  useAdminUsageLimits,
  useSetUsageLimit,
  useDeleteUsageLimit,
  AdminUserUsage,
  ProviderBreakdownEntry,
  ProviderTotal,
  ModelTotal,
  UsageLimit,
} from '../../api/usage'
import { useAgents } from '../../api/agents'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

// ---------------------------------------------------------------------------
// Stat card
// ---------------------------------------------------------------------------

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4">
      <div className="text-2xl font-semibold text-p-text">{value}</div>
      <div className="text-xs text-p-text-secondary mt-1">{label}</div>
      {sub && <div className="text-xs text-p-text-light mt-0.5">{sub}</div>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Percent badge
// ---------------------------------------------------------------------------

function PctBadge({ pct, limit }: { pct: number; limit: number | null }) {
  if (limit === null) return <span className="text-xs text-p-text-light">No limit</span>
  const color = pct >= 100 ? 'text-p-error' : pct >= 80 ? 'text-p-accent-yellow' : 'text-p-text-secondary'
  return <span className={`text-xs font-medium ${color}`}>{pct.toFixed(0)}%</span>
}

// ---------------------------------------------------------------------------
// Horizontal bar row — used by the "Costs by Provider" / "Costs by Model"
// sections. Mobile-first: full-width bar, label/cost on one line above it.
// ---------------------------------------------------------------------------

function CostBarRow({ label, sublabel, cost, percent }: {
  label: React.ReactNode; sublabel?: React.ReactNode; cost: number; percent: number
}) {
  return (
    <div>
      <div className="flex justify-between items-baseline text-sm mb-1 gap-2">
        <div className="min-w-0 flex items-center gap-2">
          {sublabel && (
            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded-sm bg-p-bg text-p-text-secondary whitespace-nowrap">
              {sublabel}
            </span>
          )}
          <span className="font-medium text-p-text truncate">{label}</span>
        </div>
        <span className="text-p-text-secondary whitespace-nowrap">
          ${cost.toFixed(2)}{' '}
          <span className="text-xs text-p-text-light">{percent.toFixed(0)}%</span>
        </span>
      </div>
      <div className="h-1.5 bg-p-bg rounded-full overflow-hidden">
        <div
          className="h-full bg-brand rounded-full transition-all"
          style={{ width: `${Math.min(percent, 100)}%` }}
        />
      </div>
    </div>
  )
}

function ProviderTotalsSection({ rows }: { rows: ProviderTotal[] }) {
  const total = rows.reduce((acc, r) => acc + r.cost, 0)
  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4">
      <h3 className="text-sm font-medium text-p-text-secondary uppercase tracking-wide mb-3">
        Costs by Provider
      </h3>
      {rows.length === 0 ? (
        <div className="text-xs text-p-text-light italic">No usage this period.</div>
      ) : (
        <div className="space-y-2.5">
          {rows.map(r => (
            <CostBarRow
              key={r.provider}
              label={r.provider}
              cost={r.cost}
              percent={total > 0 ? (r.cost / total) * 100 : 0}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ModelTotalsSection({ rows }: { rows: ModelTotal[] }) {
  const total = rows.reduce((acc, r) => acc + r.cost, 0)
  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4">
      <h3 className="text-sm font-medium text-p-text-secondary uppercase tracking-wide mb-3">
        Costs by Model
      </h3>
      {rows.length === 0 ? (
        <div className="text-xs text-p-text-light italic">No usage this period.</div>
      ) : (
        <div className="space-y-2.5">
          {rows.map((r, i) => (
            <CostBarRow
              key={`${r.provider}-${r.model}-${i}`}
              label={<span className="font-mono text-xs sm:text-sm">{r.model || '—'}</span>}
              sublabel={r.provider}
              cost={r.cost}
              percent={total > 0 ? (r.cost / total) * 100 : 0}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Provider × model cost breakdown (admin drill-down)
// ---------------------------------------------------------------------------

function BreakdownPanel({ rows }: { rows: ProviderBreakdownEntry[] }) {
  if (!rows || rows.length === 0) {
    return (
      <div className="text-xs text-p-text-light italic px-2 py-1">
        No usage this period.
      </div>
    )
  }
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-p-text-light border-b border-p-border-light">
          <th className="px-2 py-1 font-normal">Provider</th>
          <th className="px-2 py-1 font-normal">Model</th>
          <th className="px-2 py-1 font-normal text-right">Cost</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={`${r.provider}-${r.model}-${i}`} className="border-b border-p-border-light/50 last:border-0">
            <td className="px-2 py-1 text-p-text">{r.provider}</td>
            <td className="px-2 py-1 text-p-text-secondary font-mono">{r.model || <span className="italic text-p-text-light">—</span>}</td>
            <td className="px-2 py-1 text-right text-p-text">${r.cost.toFixed(4)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// ---------------------------------------------------------------------------
// Role defaults editor
// ---------------------------------------------------------------------------

const ROLES = ['admin', 'creator', 'member'] as const
const PERIODS = ['monthly', 'weekly'] as const

function RoleDefaultsEditor({ limits }: { limits: UsageLimit[] }) {
  const setLimit = useSetUsageLimit()
  const deleteLimit = useDeleteUsageLimit()
  const [edits, setEdits] = useState<Record<string, { value: string; noLimit: boolean }>>({})

  const getKey = (role: string, period: string) => `${role}:${period}`
  const findLimit = (role: string, period: string) =>
    limits.find(l => l.limit_type === 'role_default' && l.target === role && l.period === period)

  const getEdit = (role: string, period: string) => {
    const key = getKey(role, period)
    if (edits[key]) return edits[key]
    const existing = findLimit(role, period)
    return {
      value: existing?.cost_limit_usd != null ? String(existing.cost_limit_usd) : '',
      noLimit: existing ? existing.cost_limit_usd === null : true,
    }
  }

  const handleSave = (role: string, period: string) => {
    const edit = getEdit(role, period)
    if (edit.noLimit) {
      const existing = findLimit(role, period)
      if (existing) {
        // Set explicit "no limit" (null)
        setLimit.mutate({ limit_type: 'role_default', target: role, period, cost_limit_usd: null })
      }
      // If no existing row and noLimit, nothing to do
    } else {
      const val = parseFloat(edit.value)
      if (!isNaN(val) && val >= 0) {
        setLimit.mutate({ limit_type: 'role_default', target: role, period, cost_limit_usd: val })
      }
    }
    setEdits(prev => {
      const next = { ...prev }
      delete next[getKey(role, period)]
      return next
    })
  }

  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4">
      <h3 className="text-sm font-medium text-p-text">Platform-Auth Budget (by role)</h3>
      <p className="text-xs text-p-text-light mt-0.5 mb-3">
        Per-role cap on <strong>Platform API</strong> spend (borrowed credentials). Own-subscription usage is never limited.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-p-text-secondary border-b border-p-border-light">
              <th className="pb-2 font-medium">Role</th>
              {PERIODS.map(p => <th key={p} className="pb-2 font-medium capitalize">{p} ($)</th>)}
              <th className="pb-2 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {ROLES.map(role => {
              const hasChanges = PERIODS.some(p => edits[getKey(role, p)])
              return (
                <tr key={role} className="border-b border-p-border-light last:border-0">
                  <td className="py-2 capitalize font-medium text-p-text">{role}</td>
                  {PERIODS.map(period => {
                    const edit = getEdit(role, period)
                    const key = getKey(role, period)
                    return (
                      <td key={period} className="py-2">
                        <div className="flex items-center gap-2">
                          <input
                            type="number"
                            min="0"
                            step="1"
                            placeholder="0"
                            disabled={edit.noLimit}
                            value={edit.noLimit ? '' : edit.value}
                            onChange={e => setEdits(prev => ({
                              ...prev,
                              [key]: { value: e.target.value, noLimit: false },
                            }))}
                            className="w-20 px-2 py-1 rounded-sm border border-p-border-light bg-p-bg text-p-text text-sm disabled:opacity-40"
                          />
                          <label className="flex items-center gap-1 text-xs text-p-text-secondary whitespace-nowrap">
                            <input
                              type="checkbox"
                              checked={edit.noLimit}
                              onChange={e => setEdits(prev => ({
                                ...prev,
                                [key]: { value: edit.value, noLimit: e.target.checked },
                              }))}
                              className="rounded-sm"
                            />
                            No limit
                          </label>
                        </div>
                      </td>
                    )
                  })}
                  <td className="py-2">
                    {hasChanges && (
                      <button
                        onClick={() => PERIODS.forEach(p => { if (edits[getKey(role, p)]) handleSave(role, p) })}
                        className="px-3 py-1 text-xs rounded-sm bg-brand text-white hover:bg-brand-hover"
                      >
                        Save
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// User limit editor (inline)
// ---------------------------------------------------------------------------

function UserLimitForm({ user, limits, onClose }: {
  user: AdminUserUsage; limits: UsageLimit[]; onClose: () => void
}) {
  const setLimit = useSetUsageLimit()
  const deleteLimit = useDeleteUsageLimit()
  const [monthly, setMonthly] = useState(() => {
    const l = limits.find(l => l.limit_type === 'user_override' && l.target === user.sub && l.period === 'monthly')
    return l ? (l.cost_limit_usd != null ? String(l.cost_limit_usd) : '') : ''
  })
  const [weekly, setWeekly] = useState(() => {
    const l = limits.find(l => l.limit_type === 'user_override' && l.target === user.sub && l.period === 'weekly')
    return l ? (l.cost_limit_usd != null ? String(l.cost_limit_usd) : '') : ''
  })
  const [useDefault, setUseDefault] = useState(() => {
    return !limits.some(l => l.limit_type === 'user_override' && l.target === user.sub)
  })

  const handleSave = () => {
    if (useDefault) {
      deleteLimit.mutate({ limit_type: 'user_override', target: user.sub, period: 'monthly' })
      deleteLimit.mutate({ limit_type: 'user_override', target: user.sub, period: 'weekly' })
    } else {
      const m = monthly ? parseFloat(monthly) : null
      setLimit.mutate({ limit_type: 'user_override', target: user.sub, period: 'monthly', cost_limit_usd: isNaN(m as any) ? null : m })
      const w = weekly ? parseFloat(weekly) : null
      setLimit.mutate({ limit_type: 'user_override', target: user.sub, period: 'weekly', cost_limit_usd: isNaN(w as any) ? null : w })
    }
    onClose()
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-p-text-light">
        Platform-Auth budget — caps this user's <strong>Platform API</strong> spend (borrowed credentials) only.
      </p>
      <div className="flex flex-wrap items-end gap-3">
      <label className="flex items-center gap-1 text-xs text-p-text-secondary">
        <input type="checkbox" checked={useDefault} onChange={e => setUseDefault(e.target.checked)} className="rounded-sm" />
        Use role default
      </label>
      {!useDefault && (
        <>
          <div>
            <div className="text-xs text-p-text-secondary mb-1">Monthly ($)</div>
            <input
              type="number" min="0" step="1" placeholder="No limit"
              value={monthly} onChange={e => setMonthly(e.target.value)}
              className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text"
            />
          </div>
          <div>
            <div className="text-xs text-p-text-secondary mb-1">Weekly ($)</div>
            <input
              type="number" min="0" step="1" placeholder="No limit"
              value={weekly} onChange={e => setWeekly(e.target.value)}
              className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text"
            />
          </div>
        </>
      )}
      <button onClick={handleSave} className="px-3 py-1.5 text-xs rounded-sm bg-brand text-white hover:bg-brand-hover">
        Save
      </button>
      <button onClick={onClose} className="px-3 py-1.5 text-xs rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface">
        Cancel
      </button>
      </div>
    </div>
  )
}

function UserLimitEditor({ user, limits, onClose }: {
  user: AdminUserUsage; limits: UsageLimit[]; onClose: () => void
}) {
  return (
    <tr>
      <td colSpan={7} className="p-3 bg-p-bg/50">
        <UserLimitForm user={user} limits={limits} onClose={onClose} />
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Per-agent limit editor (inline) — caps agent-scoped spend on the platform
// pool. The backend (PUT /v1/admin/usage/limits) already accepts limit_type
// 'agent'; agent limits have no role fallback, so "No limit" just deletes.
// ---------------------------------------------------------------------------

function AgentLimitForm({ agent, limits, onClose }: {
  agent: string; limits: UsageLimit[]; onClose: () => void
}) {
  const setLimit = useSetUsageLimit()
  const deleteLimit = useDeleteUsageLimit()
  const find = (period: string) =>
    limits.find(l => l.limit_type === 'agent' && l.target === agent && l.period === period)
  const [monthly, setMonthly] = useState(() => {
    const l = find('monthly'); return l && l.cost_limit_usd != null ? String(l.cost_limit_usd) : ''
  })
  const [weekly, setWeekly] = useState(() => {
    const l = find('weekly'); return l && l.cost_limit_usd != null ? String(l.cost_limit_usd) : ''
  })
  const [noLimit, setNoLimit] = useState(() =>
    !limits.some(l => l.limit_type === 'agent' && l.target === agent))

  const handleSave = () => {
    if (noLimit) {
      if (find('monthly')) deleteLimit.mutate({ limit_type: 'agent', target: agent, period: 'monthly' })
      if (find('weekly')) deleteLimit.mutate({ limit_type: 'agent', target: agent, period: 'weekly' })
    } else {
      const m = monthly ? parseFloat(monthly) : null
      setLimit.mutate({ limit_type: 'agent', target: agent, period: 'monthly', cost_limit_usd: isNaN(m as any) ? null : m })
      const w = weekly ? parseFloat(weekly) : null
      setLimit.mutate({ limit_type: 'agent', target: agent, period: 'weekly', cost_limit_usd: isNaN(w as any) ? null : w })
    }
    onClose()
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-p-text-light">
        Caps <strong>agent-scoped</strong> spend (scheduled tasks, triggers, meetings) on the platform pool.
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex items-center gap-1 text-xs text-p-text-secondary">
          <input type="checkbox" checked={noLimit} onChange={e => setNoLimit(e.target.checked)} className="rounded-sm" />
          No limit
        </label>
        {!noLimit && (
          <>
            <div>
              <div className="text-xs text-p-text-secondary mb-1">Monthly ($)</div>
              <input type="number" min="0" step="1" placeholder="No limit" value={monthly}
                onChange={e => setMonthly(e.target.value)}
                className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text" />
            </div>
            <div>
              <div className="text-xs text-p-text-secondary mb-1">Weekly ($)</div>
              <input type="number" min="0" step="1" placeholder="No limit" value={weekly}
                onChange={e => setWeekly(e.target.value)}
                className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text" />
            </div>
          </>
        )}
        <button onClick={handleSave} className="px-3 py-1.5 text-xs rounded-sm bg-brand text-white hover:bg-brand-hover">Save</button>
        <button onClick={onClose} className="px-3 py-1.5 text-xs rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface">Cancel</button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Agent Budgets — proactively cap any agent's agent-scoped spend, even before
// it has incurred usage (the usage table only lists agents that already spent).
// ---------------------------------------------------------------------------

function AgentBudgetsSection({ limits }: { limits: UsageLimit[] }) {
  const { data: agents } = useAgents({ all: true })
  const [editing, setEditing] = useState<string | null>(null)

  const limitCell = (agent: string, period: string) => {
    const l = limits.find(x => x.limit_type === 'agent' && x.target === agent && x.period === period)
    if (!l) return <span className="text-p-text-light">—</span>
    return l.cost_limit_usd == null
      ? <span className="text-p-text-light">No limit</span>
      : <span className="text-p-text">${l.cost_limit_usd.toFixed(0)}</span>
  }

  const list = (agents || []).slice().sort((a, b) => a.display_name.localeCompare(b.display_name))

  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden">
      <div className="px-4 py-3 border-b border-p-border-light">
        <h3 className="text-sm font-medium text-p-text">Agent Budgets</h3>
        <p className="text-xs text-p-text-light mt-0.5">
          Cap each agent's <strong>agent-scoped</strong> spend (scheduled tasks, triggers, meetings, agent chats) on the platform pool. Set a budget before usage accrues.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-p-text-secondary border-b border-p-border-light bg-p-bg/30">
              <th className="px-4 py-2 font-medium">Agent</th>
              <th className="px-4 py-2 font-medium text-right">Monthly</th>
              <th className="px-4 py-2 font-medium text-right">Weekly</th>
              <th className="px-4 py-2 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.length === 0 && (
              <tr><td colSpan={4} className="px-4 py-3 text-xs text-p-text-light">No agents.</td></tr>
            )}
            {list.map(a => (
              <Fragment key={a.name}>
                <tr className="border-b border-p-border-light last:border-0">
                  <td className="px-4 py-2">
                    <span className="font-medium text-p-text">{a.display_name}</span>
                  </td>
                  <td className="px-4 py-2 text-right">{limitCell(a.name, 'monthly')}</td>
                  <td className="px-4 py-2 text-right">{limitCell(a.name, 'weekly')}</td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => setEditing(editing === a.name ? null : a.name)}
                      className="text-xs text-brand hover:underline"
                    >
                      {editing === a.name ? 'Close' : 'Set Limit'}
                    </button>
                  </td>
                </tr>
                {editing === a.name && (
                  <tr>
                    <td colSpan={4} className="p-3 bg-p-bg/50">
                      <AgentLimitForm agent={a.name} limits={limits} onClose={() => setEditing(null)} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function UsagePage() {
  const { data: overview, isLoading } = useAdminUsageOverview()
  const { data: limitsData } = useAdminUsageLimits()
  const [editingUser, setEditingUser] = useState<string | null>(null)
  const [expandedUser, setExpandedUser] = useState<string | null>(null)
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null)
  const [editingAgent, setEditingAgent] = useState<string | null>(null)
  const limits = limitsData?.limits || []

  if (isLoading) {
    return <div className="p-6 text-sm text-p-text-light">Loading usage data...</div>
  }

  if (!overview) {
    return <div className="p-6 text-sm text-p-text-light">No usage data available.</div>
  }

  const nearLimit = overview.users.filter(u => u.monthly_limit && u.monthly_percent >= 80).length

  return (
    <div className="space-y-6 max-w-6xl">
      <h1 className="text-xl font-medium text-p-text">Usage & Limits</h1>

      {/* Overview cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Total Cost (Month)" value={`$${overview.totals.cost.toFixed(2)}`} />
        <StatCard label="Total Messages" value={String(overview.totals.messages)} />
        <StatCard label="Active Users" value={String(overview.totals.active_users)} />
        <StatCard
          label="Near Limit"
          value={String(nearLimit)}
          sub={nearLimit > 0 ? 'users at 80%+' : undefined}
        />
      </div>

      {/* Daily chart */}
      {overview.daily_chart.length > 0 && (
        <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4">
          <h3 className="text-sm font-medium text-p-text-secondary uppercase tracking-wide mb-3">Platform Daily Usage (30 days)</h3>
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={overview.daily_chart}>
                <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(d: string) => d.slice(5)} />
                {/* Sub-$10 ranges get fractional ticks — whole-dollar rounding
                    would render duplicate labels ($2, $2). */}
                <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => `$${v < 10 && v % 1 !== 0 ? v.toFixed(2) : v.toFixed(0)}`} width={40} />
                <Tooltip
                  formatter={(value: number) => [`$${value.toFixed(4)}`, 'Cost']}
                  contentStyle={{ fontSize: 12 }}
                />
                <Bar dataKey="cost" fill="#146bb5" radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Platform-wide cost breakdown */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ProviderTotalsSection rows={overview.provider_totals || []} />
        <ModelTotalsSection rows={overview.model_totals || []} />
      </div>

      {/* Per-user usage table */}
      <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden">
        <div className="px-4 py-3 border-b border-p-border-light">
          <h3 className="text-sm font-medium text-p-text">Per-User Usage (This Month)</h3>
          <p className="text-xs text-p-text-light mt-0.5">
            Limits gate only <strong>Platform API</strong> spend (borrowed API keys / direct-LLM).
            <strong> Own</strong> = the user's own subscription — reference only, never limited.
          </p>
        </div>
        {/* Desktop table */}
        <div className="hidden md:block overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-p-text-secondary border-b border-p-border-light bg-p-bg/30">
                <th className="px-4 py-2 font-medium">User</th>
                <th className="px-4 py-2 font-medium">Role</th>
                <th className="px-4 py-2 font-medium text-right">Own</th>
                <th className="px-4 py-2 font-medium text-right">Platform API</th>
                <th className="px-4 py-2 font-medium text-right">Limit</th>
                <th className="px-4 py-2 font-medium text-right">%</th>
                <th className="px-4 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {overview.users.map(u => (
                <Fragment key={u.sub}>
                  <tr className="border-b border-p-border-light last:border-0 hover:bg-p-bg/20">
                    <td className="px-4 py-2">
                      <button
                        onClick={() => setExpandedUser(expandedUser === u.sub ? null : u.sub)}
                        className="mr-1.5 inline-flex items-center gap-1 text-xs text-p-text-light hover:text-p-text align-middle rounded-sm px-1 -ml-1 hover:bg-p-bg/50"
                        title="Show provider/model breakdown"
                      >
                        <span className="w-3 inline-block">{expandedUser === u.sub ? '▾' : '▸'}</span>
                      </button>
                      <span className="font-medium text-p-text">{u.name}</span>
                      <div className="text-xs text-p-text-light pl-5">{u.email}</div>
                    </td>
                    <td className="px-4 py-2 capitalize text-p-text-secondary">{u.role}</td>
                    <td className="px-4 py-2 text-right text-p-text-secondary">${(u.self_cost ?? 0).toFixed(2)}</td>
                    <td className="px-4 py-2 text-right text-p-text font-medium">${(u.platform_cost ?? 0).toFixed(2)}</td>
                    <td className="px-4 py-2 text-right">
                      {u.monthly_limit != null ? `$${u.monthly_limit.toFixed(0)}` : <span className="text-p-text-light">None</span>}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <PctBadge pct={u.monthly_percent} limit={u.monthly_limit} />
                    </td>
                    <td className="px-4 py-2">
                      <button
                        onClick={() => setEditingUser(editingUser === u.sub ? null : u.sub)}
                        className="text-xs text-brand hover:underline"
                      >
                        {editingUser === u.sub ? 'Close' : 'Set Limit'}
                      </button>
                    </td>
                  </tr>
                  {expandedUser === u.sub && (
                    <tr key={`bd-${u.sub}`}>
                      <td colSpan={7} className="px-6 py-2 bg-p-bg/30">
                        <BreakdownPanel rows={u.breakdown || []} />
                      </td>
                    </tr>
                  )}
                  {editingUser === u.sub && (
                    <UserLimitEditor
                      key={`edit-${u.sub}`}
                      user={u}
                      limits={limits}
                      onClose={() => setEditingUser(null)}
                    />
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
        {/* Mobile cards */}
        <div className="md:hidden divide-y divide-p-border-light">
          {overview.users.map(u => (
            <div key={u.sub} className="p-4 space-y-2">
              <div className="flex justify-between items-start">
                <div>
                  <div className="font-medium text-p-text text-sm">{u.name}</div>
                  <div className="text-xs text-p-text-light">{u.email}</div>
                </div>
                <span className="text-xs capitalize text-p-text-secondary px-2 py-0.5 rounded-sm bg-p-bg">{u.role}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-p-text-secondary">Platform API: <strong className="text-p-text">${(u.platform_cost ?? 0).toFixed(2)}</strong></span>
                <span className="text-p-text-secondary">
                  Limit: {u.monthly_limit != null ? `$${u.monthly_limit.toFixed(0)}` : 'None'}
                </span>
              </div>
              <div className="text-xs text-p-text-light">Own subscription (reference): ${(u.self_cost ?? 0).toFixed(2)}</div>
              {u.monthly_limit != null && (
                <div className="h-1.5 rounded-full bg-p-surface overflow-hidden">
                  <div
                    className={`h-full rounded-full ${u.monthly_percent >= 100 ? 'bg-p-error' : u.monthly_percent >= 80 ? 'bg-p-accent-yellow' : 'bg-brand'}`}
                    style={{ width: `${Math.min(u.monthly_percent, 100)}%` }}
                  />
                </div>
              )}
              <div className="flex gap-3">
                <button
                  onClick={() => setEditingUser(editingUser === u.sub ? null : u.sub)}
                  className="text-xs text-brand hover:underline"
                >
                  {editingUser === u.sub ? 'Close limit' : 'Set Limit'}
                </button>
                <button
                  onClick={() => setExpandedUser(expandedUser === u.sub ? null : u.sub)}
                  className="text-xs text-brand hover:underline"
                >
                  {expandedUser === u.sub ? 'Hide breakdown' : 'Show breakdown'}
                </button>
              </div>
              {expandedUser === u.sub && (
                <div className="pt-2 border-t border-p-border-light mt-2 overflow-x-auto">
                  <BreakdownPanel rows={u.breakdown || []} />
                </div>
              )}
              {editingUser === u.sub && (
                <div className="pt-2 border-t border-p-border-light mt-2">
                  <UserLimitForm user={u} limits={limits} onClose={() => setEditingUser(null)} />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Per-agent usage */}
      {overview.agents.length > 0 && (
        <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface overflow-hidden">
          <div className="px-4 py-3 border-b border-p-border-light">
            <h3 className="text-sm font-medium text-p-text">Agent-Scoped Usage (This Month)</h3>
            <p className="text-xs text-p-text-light mt-0.5">Costs from agent-scoped work (schedules, triggers, meetings). Set budgets here or in <strong>Agent Budgets</strong> below.</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-p-text-secondary border-b border-p-border-light bg-p-bg/30">
                  <th className="px-4 py-2 font-medium">Agent</th>
                  <th className="px-4 py-2 font-medium text-right">Cost</th>
                  <th className="px-4 py-2 font-medium text-right">Runs</th>
                  <th className="px-4 py-2 font-medium text-right">Limit</th>
                  <th className="px-4 py-2 font-medium text-right">%</th>
                  <th className="px-4 py-2 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {overview.agents.map(a => (
                  <Fragment key={a.agent}>
                    <tr className="border-b border-p-border-light last:border-0">
                      <td className="px-4 py-2">
                        <button
                          onClick={() => setExpandedAgent(expandedAgent === a.agent ? null : a.agent)}
                          className="mr-1 inline-block w-4 text-p-text-light hover:text-p-text align-middle"
                          title="Show provider/model breakdown"
                        >
                          {expandedAgent === a.agent ? '▾' : '▸'}
                        </button>
                        <span className="font-medium text-p-text">{a.agent}</span>
                      </td>
                      <td className="px-4 py-2 text-right text-p-text">${a.total_cost.toFixed(2)}</td>
                      <td className="px-4 py-2 text-right text-p-text-secondary">{a.record_count}</td>
                      <td className="px-4 py-2 text-right">
                        {a.monthly_limit != null ? `$${a.monthly_limit.toFixed(0)}` : <span className="text-p-text-light">None</span>}
                      </td>
                      <td className="px-4 py-2 text-right">
                        <PctBadge pct={a.monthly_percent} limit={a.monthly_limit} />
                      </td>
                      <td className="px-4 py-2">
                        <button
                          onClick={() => setEditingAgent(editingAgent === a.agent ? null : a.agent)}
                          className="text-xs text-brand hover:underline"
                        >
                          {editingAgent === a.agent ? 'Close' : 'Set Limit'}
                        </button>
                      </td>
                    </tr>
                    {expandedAgent === a.agent && (
                      <tr key={`bd-${a.agent}`}>
                        <td colSpan={6} className="px-6 py-2 bg-p-bg/30">
                          <BreakdownPanel rows={a.breakdown || []} />
                        </td>
                      </tr>
                    )}
                    {editingAgent === a.agent && (
                      <tr key={`edit-${a.agent}`}>
                        <td colSpan={6} className="p-3 bg-p-bg/50">
                          <AgentLimitForm agent={a.agent} limits={limits} onClose={() => setEditingAgent(null)} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Platform-Auth Budget (by role) */}
      <RoleDefaultsEditor limits={limits} />

      {/* Agent budgets — always available (set a cap before any usage). */}
      <AgentBudgetsSection limits={limits} />
    </div>
  )
}
