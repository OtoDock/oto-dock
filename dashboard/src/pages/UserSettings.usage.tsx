import { useState } from 'react'
import { useMyUsage, useMyLimits, useSetMyLimit, PeriodUsage, UsageLimit } from '../api/usage'
import { MyPoolCapSection } from '../components/usage/PoolCapSection'
import { tooltipProps, useChartColors } from '../components/usage/chartColors'
import { toastError } from '../lib/http/apiJson'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'
import { CARD } from '../components/ui/classes'
import { PAGE_TITLE } from '../components/ui/classes'

export function UsageBar({ period }: { period: PeriodUsage | null }) {
  if (!period) return null
  const hasLimit = period.limit !== null && period.limit !== undefined
  const pct = hasLimit ? Math.min(period.percent, 100) : 0
  const barColor = !hasLimit ? 'bg-status-info-solid' : pct >= 100 ? 'bg-status-danger-solid' : pct >= 80 ? 'bg-status-warning-solid' : 'bg-status-info-solid'

  return (
    <div>
      <div className="flex justify-between text-sm mb-1">
        <span className="text-secondary">
          ${period.used.toFixed(2)}
          {hasLimit ? ` / $${period.limit!.toFixed(2)}` : ''}
        </span>
        {hasLimit && (
          <span className={`font-medium ${pct >= 100 ? 'text-status-danger-fg' : pct >= 80 ? 'text-status-warning-fg' : 'text-secondary'}`}>
            {period.percent.toFixed(0)}%
          </span>
        )}
      </div>
      {hasLimit && (
        <div className="h-2 rounded-full bg-surface-muted overflow-hidden">
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
  // Both periods save in one tick: Save stays off until both have answered.
  const [saving, setSaving] = useState(false)

  const handleSave = () => {
    const m = monthly.trim() === '' ? null : parseFloat(monthly)
    const w = weekly.trim() === '' ? null : parseFloat(weekly)
    setSaving(true)
    Promise.all([
      setLimit.mutateAsync({ period: 'monthly', cost_limit_usd: m == null || isNaN(m) ? null : m }),
      setLimit.mutateAsync({ period: 'weekly', cost_limit_usd: w == null || isNaN(w) ? null : w }),
    ])
      .then(onClose, toastError('The limits were not saved'))
      .finally(() => setSaving(false))
  }

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="w-24">
        <div className="text-xs text-secondary mb-1">Monthly ($)</div>
        <Input type="number" min="0" step="1" placeholder="No cap" aria-label="Monthly ($)"
          value={monthly} onChange={e => setMonthly(e.target.value)} />
      </div>
      <div className="w-24">
        <div className="text-xs text-secondary mb-1">Weekly ($)</div>
        <Input type="number" min="0" step="1" placeholder="No cap" aria-label="Weekly ($)"
          value={weekly} onChange={e => setWeekly(e.target.value)} />
      </div>
      <Button variant="primary" size="xs" onClick={handleSave} disabled={saving}>
        Save
      </Button>
      <Button variant="secondary" size="xs" onClick={onClose}>
        Cancel
      </Button>
    </div>
  )
}

export function MyApiKeysSection({ selfLimits }: { selfLimits: { monthly: PeriodUsage; weekly: PeriodUsage } }) {
  const { data: limitsData } = useMyLimits()
  const [editing, setEditing] = useState(false)
  return (
    <div className={`${CARD} p-4 space-y-4 mb-4`}>
      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-xs font-medium text-secondary uppercase tracking-wide">My API keys</div>
          <div className="text-xs text-tertiary mt-0.5">
            What your own Anthropic or OpenAI API keys spend. You can set a cap on it.
          </div>
        </div>
        <button onClick={() => setEditing(e => !e)} className="text-xs text-link hover:underline">
          {editing ? 'Close' : 'Set cap'}
        </button>
      </div>
      <div>
        <div className="text-xs text-tertiary mb-1">This month</div>
        <UsageBar period={selfLimits.monthly} />
      </div>
      <div>
        <div className="text-xs text-tertiary mb-1">This week</div>
        <UsageBar period={selfLimits.weekly} />
      </div>
      {editing && (
        <div className="pt-3 border-t border-subtle">
          <MyLimitForm limits={limitsData?.limits ?? []} onClose={() => setEditing(false)} />
        </div>
      )}
    </div>
  )
}

export function UsageSection() {
  const { data: usage, isLoading } = useMyUsage()
  const chart = useChartColors()

  if (isLoading) {
    return (
      <div className="mb-8">
        <h2 className={`${PAGE_TITLE} mb-3`}>Usage</h2>
        <div className="text-sm text-tertiary">Loading...</div>
      </div>
    )
  }

  if (!usage) return null

  return (
    <div className="mb-8">
      <h2 className={`${PAGE_TITLE} mb-3`}>Usage</h2>
      <p className="text-sm text-secondary mb-4">
        Your subscription costs are measured at the equivalent API cost.
      </p>

      {/* Period summaries */}
      <div className={`${CARD} p-4 space-y-4 mb-4`}>
        {usage.monthly && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-xs font-medium text-secondary uppercase tracking-wide">This Month · Platform API</span>
              <span className="text-xs text-tertiary">Own accounts: ${(usage.monthly.self_used ?? 0).toFixed(2)}</span>
            </div>
            <UsageBar period={usage.monthly} />
          </div>
        )}
        {usage.weekly && (
          <div>
            <div className="flex items-baseline justify-between mb-1">
              <span className="text-xs font-medium text-secondary uppercase tracking-wide">This Week · Platform API</span>
              <span className="text-xs text-tertiary">Own accounts: ${(usage.weekly.self_used ?? 0).toFixed(2)}</span>
            </div>
            <UsageBar period={usage.weekly} />
          </div>
        )}
        {!usage.monthly && !usage.weekly && (
          <div className="text-sm text-tertiary">No usage data yet.</div>
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
        <div className={`${CARD} p-4 mb-4`}>
          <div className="text-xs font-medium text-secondary uppercase tracking-wide mb-3">Daily Usage, Last 30 Days</div>
          <div className="h-40">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={usage.daily_chart}>
                <XAxis dataKey="date" stroke={chart.axis} tick={{ fontSize: 10, fill: chart.axis }} tickFormatter={(d: string) => d.slice(5)} />
                <YAxis stroke={chart.axis} tick={{ fontSize: 10, fill: chart.axis }} tickFormatter={(v: number) => `$${v.toFixed(0)}`} width={35} />
                <Tooltip
                  formatter={(value: number) => [`$${value.toFixed(4)}`, 'Cost']}
                  labelFormatter={(label: string) => label}
                  {...tooltipProps(chart)}
                />
                <Bar dataKey="cost" fill={chart.bar} radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Agent breakdown */}
      {usage.agent_breakdown.length > 0 && (
        <div className={`${CARD} p-4`}>
          <div className="text-xs font-medium text-secondary uppercase tracking-wide mb-3">By Agent, This Month</div>
          <div className="space-y-2">
            {usage.agent_breakdown.map(a => (
              <div key={a.agent} className="flex justify-between text-sm">
                <span className="text-primary">{a.agent}</span>
                <div className="flex gap-4">
                  <span className="text-secondary">${a.cost.toFixed(2)}</span>
                  <span className="text-tertiary w-16 text-right">{a.messages} msgs</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
