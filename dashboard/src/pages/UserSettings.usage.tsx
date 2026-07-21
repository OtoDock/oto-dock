import { useMyUsage, PeriodUsage } from '../api/usage'
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
        Usage is estimated at standard API pricing. Your <strong>own-subscription</strong> chats
        and tasks aren’t charged or capped here — your provider enforces those. Any
        {' '}<strong>Platform API</strong> usage (when you borrow the platform’s API keys /
        direct-LLM) counts toward the budget below.
      </p>

      {/* Period summaries */}
      <div className="rounded-xl border border-p-border-light bg-white dark:bg-p-surface p-4 space-y-4 mb-4">
        {usage.monthly && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-xs font-medium text-p-text-secondary uppercase tracking-wide">This Month · Platform API</span>
              <span className="text-xs text-p-text-light">Own subscription: ${(usage.monthly.self_used ?? 0).toFixed(2)}</span>
            </div>
            <UsageBar period={usage.monthly} />
          </div>
        )}
        {usage.weekly && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-xs font-medium text-p-text-secondary uppercase tracking-wide">This Week · Platform API</span>
              <span className="text-xs text-p-text-light">Own subscription: ${(usage.weekly.self_used ?? 0).toFixed(2)}</span>
            </div>
            <UsageBar period={usage.weekly} />
          </div>
        )}
        {!usage.monthly && !usage.weekly && (
          <div className="text-sm text-p-text-light">No usage data yet.</div>
        )}
      </div>

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
