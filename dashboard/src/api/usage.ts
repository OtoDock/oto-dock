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

export interface UserUsageSummary {
  monthly: PeriodUsage | null
  weekly: PeriodUsage | null
  daily_chart: DailyUsage[]
  agent_breakdown: AgentBreakdown[]
}

export interface UsageCheck {
  allowed: boolean
  warning: boolean
  periods: {
    monthly: PeriodUsage | null
    weekly: PeriodUsage | null
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

// ---------------------------------------------------------------------------
// Admin hooks
// ---------------------------------------------------------------------------

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
