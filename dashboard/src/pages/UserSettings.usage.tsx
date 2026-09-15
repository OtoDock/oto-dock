import { useState } from 'react'
import { useMyUsage, useMyLimits, useSetMyLimit, PeriodUsage, UsageLimit } from '../api/usage'
import { MyPoolCapSection } from '../components/usage/PoolCapSection'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

export function UsageBar({ period }: { period: PeriodUsage | null }) {
  if (!period) return null
  const hasLimit = period.limit !== null && period.limit !== undefined
  const pct = hasLimit ? Math.min(period.percent, 100) : 0
  const barColor = !hasLimit ? 'bg-brand' : pct >= 100 ? 'bg-p-error' : pct >= 80 ? 'bg-p-accent-yellow' : 'bg-brand'

  return (
    <div>
      <div className="flex justify-between text-sm mb-1">
        <span className="text-p-text-secondary">
          ${period.used.toFixed(2)}
          {hasLimit ? ` / $${period.limit!.toFixed(2)}` : ''}
        </span>
        {hasLimit && (
          <span className={`font-medium ${pct >= 100 ? 'text-p-error' : pct >= 80 ? 'text-p-accent-yellow' : 'text-p-text-secondary'}`}>
            {period.percent.toFixed(0)}%
          </span>
        )}
      </div>
      {hasLimit && (
        <div className="h-2 rounded-full bg-p-surface overflow-hidden">
          <div className={`h-full rounded-full transition-all ${barColor}`} style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  )
}

// The user's own API-key budget: a monthly and a weekly dollar cap on the
// spend of their own keys (user_self limits), set by the user alone.
export function MyLimitForm({ limits, onClose }: { limits: UsageLimit[]; onClose: () => void }) {
  const setLimit = useSetMyLimit()
  const current = (period: string) => {
    const l = limits.find(x => x.period === period)
    return l && l.cost_limit_usd != null ? String(l.cost_limit_usd) : ''
  }
  const [monthly, setMonthly] = useState(() => current('monthly'))
  const [weekly, setWeekly] = useState(() => current('weekly'))

  const handleSave = () => {
    const m = monthly.trim() === '' ? null : parseFloat(monthly)
    const w = weekly.trim() === '' ? null : parseFloat(weekly)
    setLimit.mutate({ period: 'monthly', cost_limit_usd: m == null || isNaN(m) ? null : m })
    setLimit.mutate({ period: 'weekly', cost_limit_usd: w == null || isNaN(w) ? null : w })
    onClose()
  }

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div>
        <div className="text-xs text-p-text-secondary mb-1">Monthly ($)</div>
        <input type="number" min="0" step="1" placeholder="No cap" aria-label="Monthly ($)"
          value={monthly} onChange={e => setMonthly(e.target.value)}
          className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text" />
      </div>
      <div>
        <div className="text-xs text-p-text-secondary mb-1">Weekly ($)</div>
        <input type="number" min="0" step="1" placeholder="No cap" aria-label="Weekly ($)"
          value={weekly} onChange={e => setWeekly(e.target.value)}
          className="w-24 px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-p-surface text-sm text-p-text" />
      </div>
      <button onClick={handleSave} className="px-3 py-1.5 text-xs rounded-sm bg-brand text-white hover:bg-brand-hover">
        Save
      </button>
      <button onClick={onClose} className="px-3 py-1.5 text-xs rounded-sm border border-p-border-light text-p-text-secondary hover:bg-p-surface">
        Cancel
      </button>
    </div>
  )
}

export function MyApiKeysSection({ selfLimits }: { selfLimits: { monthly: PeriodUsage; weekly: PeriodUsage } }) {
  const { data: limitsData } = useMyLimits()
  const [editing, setEditing] = useState(false)
  return (
    <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4 space-y-4 mb-4">
      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-xs font-medium text-p-text-secondary uppercase tracking-wide">My API keys</div>
          <div className="text-xs text-p-text-light mt-0.5">
            What your own Anthropic or OpenAI API keys spend, and the cap you set on it.
          </div>
        </div>
        <button onClick={() => setEditing(e => !e)} className="text-xs text-brand hover:underline">
          {editing ? 'Close' : 'Set cap'}
        </button>
      </div>
      <div>
        <div className="text-xs text-p-text-light mb-1">This month</div>
        <UsageBar period={selfLimits.monthly} />
      </div>
      <div>
        <div className="text-xs text-p-text-light mb-1">This week</div>
        <UsageBar period={selfLimits.weekly} />
      </div>
      {editing && (
        <div className="pt-3 border-t border-p-border-light">
          <MyLimitForm limits={limitsData?.limits ?? []} onClose={() => setEditing(false)} />
        </div>
      )}
    </div>
  )
}

export function UsageSection() {
  const { data: usage, isLoading } = useMyUsage()

  if (isLoading) {
    return (
      <div className="mb-8">
        <h2 className="text-lg font-medium text-p-text mb-3">Usage</h2>
        <div className="text-sm text-p-text-light">Loading...</div>
      </div>
    )
  }

  if (!usage) return null

  return (
    <div className="mb-8">
      <h2 className="text-lg font-medium text-p-text mb-3">Usage</h2>
      <p className="text-sm text-p-text-secondary mb-4">
        Your subscription costs are measured at the equivalent API cost.
      </p>

      {/* Period summaries */}
      <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4 space-y-4 mb-4">
        {usage.monthly && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-xs font-medium text-p-text-secondary uppercase tracking-wide">This Month · Platform API</span>
              <span className="text-xs text-p-text-light">Own accounts: ${(usage.monthly.self_used ?? 0).toFixed(2)}</span>
            </div>
            <UsageBar period={usage.monthly} />
          </div>
        )}
        {usage.weekly && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-xs font-medium text-p-text-secondary uppercase tracking-wide">This Week · Platform API</span>
              <span className="text-xs text-p-text-light">Own accounts: ${(usage.weekly.self_used ?? 0).toFixed(2)}</span>
            </div>
            <UsageBar period={usage.weekly} />
          </div>
        )}
        {!usage.monthly && !usage.weekly && (
          <div className="text-sm text-p-text-light">No usage data yet.</div>
        )}
      </div>

      {/* The user's own subscription accounts and the cap on them. */}
      <div className="mb-4">
        <MyPoolCapSection />
      </div>

      {/* Own API keys: the self-paid subset of "Own accounts" that is real money. */}
      {usage.self_limits && <MyApiKeysSection selfLimits={usage.self_limits} />}

      {/* Daily chart */}
      {usage.daily_chart.length > 0 && (
        <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4 mb-4">
          <div className="text-xs font-medium text-p-text-secondary uppercase tracking-wide mb-3">Daily Usage (30 days)</div>
          <div className="h-40">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={usage.daily_chart}>
                <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(d: string) => d.slice(5)} />
                <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => `$${v.toFixed(0)}`} width={35} />
                <Tooltip
                  formatter={(value: number) => [`$${value.toFixed(4)}`, 'Cost']}
                  labelFormatter={(label: string) => label}
                  contentStyle={{ fontSize: 12 }}
                />
                <Bar dataKey="cost" fill="#146bb5" radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Agent breakdown */}
      {usage.agent_breakdown.length > 0 && (
        <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4">
          <div className="text-xs font-medium text-p-text-secondary uppercase tracking-wide mb-3">By Agent (This Month)</div>
          <div className="space-y-2">
            {usage.agent_breakdown.map(a => (
              <div key={a.agent} className="flex justify-between text-sm">
                <span className="text-p-text">{a.agent}</span>
                <div className="flex gap-4">
                  <span className="text-p-text-secondary">${a.cost.toFixed(2)}</span>
                  <span className="text-p-text-light w-16 text-right">{a.messages} msgs</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
