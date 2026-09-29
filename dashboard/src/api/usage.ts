import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PeriodUsage {
  limit: number | null
  used: number          // platform-paid spend (what the budget gates)
  percent: number
  start: string
  end: string
  self_used?: number    // own-subscription spend (reference, not gated)
  total_used?: number   // grand total (platform + self + unattributed)
}

export interface DailyUsage {
  date: string
  cost: number
  messages: number
}

export interface AgentBreakdown {
  agent: string
  cost: number
  messages: number
}

// The subscription pool cap (one per user pool, one for the platform pool):
// caps and readings per engine. Percent = share of the accounts' weekly
// windows, dollars = spend attributed to the accounts; null = not set /
// no reading.
export type PoolCapField = 'week_pct' | 'day_pct' | 'week_usd' | 'day_usd'
export type PoolCapFields = Record<PoolCapField, number | null>

export interface PoolCapStatus {
  scope: 'user' | 'platform'
  layer: string
  configured: boolean
  caps: PoolCapFields
  readings: PoolCapFields
  on_reached: 'stop' | 'continue'
  accounts: number
  hits: PoolCapField[]
  warning: boolean
  allowed: boolean
}

export interface PoolCapResponse {
  caps: PoolCapFields
  on_reached: 'stop' | 'continue'
  engines: Record<string, PoolCapStatus>
}

export interface PoolCapUpdate extends Partial<PoolCapFields> {
  on_reached?: 'stop' | 'continue'
}

export interface UserUsageSummary {
  monthly: PeriodUsage | null
  weekly: PeriodUsage | null
  // The user's own API-key budget (their user_self cap, or limit null).
  self_limits: { monthly: PeriodUsage; weekly: PeriodUsage }
  pool: Record<string, PoolCapStatus>
  daily_chart: DailyUsage[]
  agent_breakdown: AgentBreakdown[]
}

// The `limit_warning` / `limit_reached` payloads: the platform budget's
// periods, the own API-key budget under `self`, the pool cap under `pool`.
export interface LimitPayload {
  monthly?: PeriodUsage | null
  weekly?: PeriodUsage | null
  self?: { monthly?: PeriodUsage | null; weekly?: PeriodUsage | null }
  pool?: PoolCapStatus
}

export interface UsageCheck {
  allowed: boolean
  warning: boolean
  periods: {
    monthly: PeriodUsage | null
    weekly: PeriodUsage | null
    self: { monthly: PeriodUsage | null; weekly: PeriodUsage | null }
  }
}

export interface ProviderBreakdownEntry {
  provider: string
  model: string
  cost: number
}

export interface AdminUserUsage {
  sub: string
  email: string
  name: string
  role: string
  total_cost: number       // grand total (display)
  platform_cost: number    // borrowed platform credentials — what the limit gates
  self_cost: number        // user's own subscription (reference only)
  message_count: number
  monthly_limit: number | null
  monthly_percent: number  // platform_cost / limit
  breakdown: ProviderBreakdownEntry[]
}

export interface AdminAgentUsage {
  agent: string
  total_cost: number
  record_count: number
  monthly_limit: number | null
  monthly_percent: number
  breakdown: ProviderBreakdownEntry[]
}

/** Unattended spend per app (APPS.md "Handlers"): the runs an app's
 * handler fired, named by the app while its row exists. */
export interface AdminAppUsage {
  app_id: string
  title: string
  slug: string
  agent: string
  scope: 'shared' | 'personal' | 'gone'
  total_cost: number
  run_count: number
  message_count: number
}

export interface ProviderTotal {
  provider: string
  cost: number
  message_count: number
}

export interface ModelTotal {
  provider: string
  model: string
  cost: number
  message_count: number
}

export interface AdminUsageOverview {
  totals: { cost: number; messages: number; active_users: number }
  daily_chart: DailyUsage[]
  provider_totals: ProviderTotal[]
  model_totals: ModelTotal[]
  users: AdminUserUsage[]
  agents: AdminAgentUsage[]
  /** Absent on an older proxy. */
  apps?: AdminAppUsage[]
  /** Judge spend per check (CHECKS.md "Spend"); absent on an older proxy. */
  checks?: AdminCheckUsage[]
  pool: Record<string, PoolCapStatus>
}

export interface AdminCheckUsage {
  agent: string
  check_name: string
  total_cost: number
  run_count: number
  message_count: number
}

export interface UsageLimit {
  id: number
  limit_type: string
  target: string
  period: string
  cost_limit_usd: number | null
  updated_at: string
  updated_by: string
}

// ---------------------------------------------------------------------------
// User hooks
// ---------------------------------------------------------------------------

export function useMyUsage(days = 30) {
  return useQuery({
    queryKey: ['my-usage', days],
    queryFn: async (): Promise<UserUsageSummary> => {
      const res = await apiFetch(`/v1/usage/me?days=${days}`)
      if (!res.ok) throw new Error('Failed to fetch usage')
      return res.json()
    },
    staleTime: 30_000,
  })
}

export function useMyLimits() {
  return useQuery({
    queryKey: ['my-usage-limits'],
    queryFn: async (): Promise<{ limits: UsageLimit[] }> => {
      const res = await apiFetch('/v1/usage/me/limits')
      if (!res.ok) throw new Error('Failed to fetch limits')
      return res.json()
    },
  })
}

// The user's own API-key budget: `cost_limit_usd: null` clears the period.
export function useSetMyLimit() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: { period: string; cost_limit_usd: number | null }) => {
      const res = await apiFetch('/v1/usage/me/limits', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to set limit')
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['my-usage-limits'] })
      qc.invalidateQueries({ queryKey: ['my-usage'] })
    },
  })
}

async function fetchPoolCap(path: string): Promise<PoolCapResponse> {
  const res = await apiFetch(path)
  if (!res.ok) throw new Error('Failed to fetch the pool cap')
  return res.json()
}

async function putPoolCap(path: string, data: PoolCapUpdate): Promise<PoolCapResponse> {
  const res = await apiFetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Failed to save the cap')
  }
  return res.json()
}

export function useMyPoolCap() {
  return useQuery({
    queryKey: ['my-pool-cap'],
    queryFn: () => fetchPoolCap('/v1/usage/me/pool-cap'),
    staleTime: 30_000,
  })
}

export function useSetMyPoolCap() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: PoolCapUpdate) => putPoolCap('/v1/usage/me/pool-cap', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['my-pool-cap'] })
      qc.invalidateQueries({ queryKey: ['my-usage'] })
    },
  })
}

// ---------------------------------------------------------------------------
// Admin hooks
// ---------------------------------------------------------------------------

export function useAdminPoolCap() {
  return useQuery({
    queryKey: ['admin-pool-cap'],
    queryFn: () => fetchPoolCap('/v1/admin/usage/pool-cap'),
    staleTime: 30_000,
  })
}

export function useSetAdminPoolCap() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data: PoolCapUpdate) => putPoolCap('/v1/admin/usage/pool-cap', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-pool-cap'] })
      qc.invalidateQueries({ queryKey: ['admin-usage-overview'] })
    },
  })
}

export function useAdminUsageOverview(days = 30) {
  return useQuery({
    queryKey: ['admin-usage-overview', days],
    queryFn: async (): Promise<AdminUsageOverview> => {
      const res = await apiFetch(`/v1/admin/usage/overview?days=${days}`)
      if (!res.ok) throw new Error('Failed to fetch admin usage')
      return res.json()
    },
    staleTime: 30_000,
  })
}

export function useAdminUsageLimits() {
  return useQuery({
    queryKey: ['admin-usage-limits'],
    queryFn: async (): Promise<{ limits: UsageLimit[] }> => {
      const res = await apiFetch('/v1/admin/usage/limits')
      if (!res.ok) throw new Error('Failed to fetch limits')
      return res.json()
    },
  })
}

export function useSetUsageLimit() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: { limit_type: string; target: string; period: string; cost_limit_usd: number | null }) => {
      const res = await apiFetch('/v1/admin/usage/limits', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to set limit')
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-usage-limits'] })
      qc.invalidateQueries({ queryKey: ['admin-usage-overview'] })
    },
  })
}

export function useDeleteUsageLimit() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: { limit_type: string; target: string; period: string }) => {
      const res = await apiFetch('/v1/admin/usage/limits/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to delete limit')
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-usage-limits'] })
      qc.invalidateQueries({ queryKey: ['admin-usage-overview'] })
    },
  })
}
