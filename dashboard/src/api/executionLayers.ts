/**
 * Execution Layer Subscription & Model management API hooks.
 *
 * Admin: platform subscriptions + models + pool status + user auth toggle.
 * User:  personal subscriptions + platform availability.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Subscription {
  id: string
  layer: string
  provider: string
  auth_type: string       // 'oauth' | 'api_key' | 'local_endpoint' | 'relay' (hosted OtoDock relay)
  owner_sub: string       // the connector ('' = owner-less platform infra, e.g. the relay)
  use_personal: boolean   // owner may use it for their own (user-scoped) work
  contribute_platform: boolean  // feeds the platform/agent pool (admin-only to enable)
  is_mine?: boolean       // set on pool/list responses: owner_sub === caller.sub
  label: string
  is_primary: number
  oauth_email: string
  active_sessions: number
  status: string          // 'active' | 'disabled' | 'expired'
  created_at: string
  updated_at: string
}

export interface LayerModel {
  id: number
  layer: string
  provider: string
  model_id: string
  display_name: string
  is_builtin: number
  enabled: number
  context_window: number
  pricing_input: number
  pricing_output: number
  pricing_cache_write: number
  pricing_cache_read: number
  supports_reasoning: number
  supports_xhigh: number
  created_at: string
  updated_at: string
}

export interface PoolStats {
  total: number
  active: number
  total_sessions: number
  available: number
}

export interface ExecutionLayerInfo {
  name: string
  display_name: string
  capabilities: Record<string, unknown>
  subscriptions: {
    platform: Subscription[]
    user_count: number
  }
  models: LayerModel[]
  pool_stats: PoolStats
}

export interface UserLayerInfo {
  name: string
  display_name: string
  user_subscriptions: Subscription[]
  platform_available: boolean
  allow_platform_auth: boolean
}

// ---------------------------------------------------------------------------
// Admin hooks
// ---------------------------------------------------------------------------

export const useAdminExecutionLayers = () =>
  useQuery({
    queryKey: ['admin-execution-layers'],
    queryFn: async (): Promise<ExecutionLayerInfo[]> => {
      const res = await apiFetch('/v1/admin/execution-layers')
      if (!res.ok) throw new Error('Failed to fetch execution layers')
      const data = await res.json()
      return data.layers ?? []
    },
  })

export function useAddSubscription() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      layer,
      ...body
    }: {
      layer: string
      provider: string
      auth_type: string
      label?: string
      api_key?: string
      endpoint_url?: string
      is_primary?: boolean
      use_personal?: boolean
      contribute_platform?: boolean
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/subscriptions`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add subscription')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-execution-layers'] }),
  })
}

export function useUpdateSubscription() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      layer,
      id,
      ...body
    }: {
      layer: string
      id: string
      label?: string
      is_primary?: boolean
      status?: string
      use_personal?: boolean
      contribute_platform?: boolean
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/subscriptions/${id}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('Failed to update subscription')
      return res.json()
    },
    // Optimistic: reflect the toggle instantly in User Settings (the user-scope
    // query it reads from), then reconcile. Without this the row only updated
    // after a full reload, because the prior code invalidated ONLY the admin
    // query key — never ['user-execution-layers'] that User Settings renders.
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: ['user-execution-layers'] })
      const prev = qc.getQueryData(['user-execution-layers'])
      qc.setQueryData(['user-execution-layers'], (old: any) =>
        Array.isArray(old)
          ? old.map((l: any) => ({
              ...l,
              user_subscriptions: (l.user_subscriptions ?? []).map((s: any) =>
                s.id === vars.id ? { ...s, ...vars } : s),
            }))
          : old)
      return { prev }
    },
    onError: (_e, _vars, ctx: any) => {
      if (ctx?.prev !== undefined) qc.setQueryData(['user-execution-layers'], ctx.prev)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['admin-execution-layers'] })
      qc.invalidateQueries({ queryKey: ['user-execution-layers'] })
    },
  })
}

/** Owner-scoped update (any role, own subscriptions only) — User Settings'
 *  per-account toggles. `contribute_platform` is still rejected server-side
 *  for non-admins; the admin tab keeps using useUpdateSubscription. */
export function useUserUpdateSubscription() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      layer,
      id,
      ...body
    }: {
      layer: string
      id: string
      label?: string
      is_primary?: boolean
      use_personal?: boolean
      contribute_platform?: boolean
    }) => {
      const res = await apiFetch(`/v1/users/me/execution-layers/${layer}/subscriptions/${id}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('Failed to update subscription')
      return res.json()
    },
    // Same optimistic pattern as useUpdateSubscription — reflect the toggle
    // instantly in the user-scope query User Settings renders from.
    onMutate: async (vars) => {
      await qc.cancelQueries({ queryKey: ['user-execution-layers'] })
      const prev = qc.getQueryData(['user-execution-layers'])
      qc.setQueryData(['user-execution-layers'], (old: any) =>
        Array.isArray(old)
          ? old.map((l: any) => ({
              ...l,
              user_subscriptions: (l.user_subscriptions ?? []).map((s: any) =>
                s.id === vars.id ? { ...s, ...vars } : s),
            }))
          : old)
      return { prev }
    },
    onError: (_e, _vars, ctx: any) => {
      if (ctx?.prev !== undefined) qc.setQueryData(['user-execution-layers'], ctx.prev)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['user-execution-layers'] })
      qc.invalidateQueries({ queryKey: ['admin-execution-layers'] })
    },
  })
}

export function useDeleteSubscription() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ layer, id }: { layer: string; id: string }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/subscriptions/${id}`, {
        method: 'DELETE',
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to delete subscription')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-execution-layers'] }),
  })
}

// ---------------------------------------------------------------------------
// Admin: Models
// ---------------------------------------------------------------------------

export function useAddModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      layer,
      ...body
    }: {
      layer: string
      model_id: string
      display_name: string
      provider?: string
      context_window?: number
      pricing_input?: number
      pricing_output?: number
      pricing_cache_write?: number
      pricing_cache_read?: number
      supports_reasoning?: boolean
      supports_xhigh?: boolean
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/models`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add model')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-execution-layers'] }),
  })
}

export function useUpdateModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      layer,
      id,
      ...body
    }: {
      layer: string
      id: number
      enabled?: boolean
      context_window?: number
      pricing_input?: number
      pricing_output?: number
      pricing_cache_write?: number
      pricing_cache_read?: number
      supports_reasoning?: boolean
      supports_xhigh?: boolean
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/models/${id}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('Failed to update model')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-execution-layers'] }),
  })
}

export function useDeleteModel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ layer, id }: { layer: string; id: number }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/models/${id}`, {
        method: 'DELETE',
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to delete model')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-execution-layers'] }),
  })
}

// ---------------------------------------------------------------------------
// Admin: Discover models
// ---------------------------------------------------------------------------

export interface DiscoveredModel {
  model_id: string
  display_name: string
}

export function useDiscoverModels() {
  return useMutation({
    mutationFn: async ({
      layer,
      subscriptionId,
    }: {
      layer: string
      subscriptionId: string
    }): Promise<{ models: DiscoveredModel[]; provider: string }> => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/discover-models`, {
        method: 'POST',
        body: JSON.stringify({ subscription_id: subscriptionId }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to discover models')
      }
      return res.json()
    },
  })
}

export function useBulkAddModels() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      layer,
      models,
      provider,
    }: {
      layer: string
      models: { model_id: string; display_name: string }[]
      provider: string
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/models/bulk`, {
        method: 'POST',
        body: JSON.stringify({ models, provider }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add models')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-execution-layers'] }),
  })
}

// ---------------------------------------------------------------------------
// Admin: User platform auth
// ---------------------------------------------------------------------------

export function useSetPlatformAuth() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ userSub, allowed }: { userSub: string; allowed: boolean }) => {
      const res = await apiFetch(`/v1/admin/users/${userSub}/platform-auth`, {
        method: 'PUT',
        body: JSON.stringify({ allowed }),
      })
      if (!res.ok) throw new Error('Failed to update platform auth')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-users'] }),
  })
}

// ---------------------------------------------------------------------------
// Claude OAuth flow
// ---------------------------------------------------------------------------

export function useStartClaudeOAuth() {
  return useMutation({
    mutationFn: async ({ layer, ownerType }: { layer: string; ownerType: 'platform' | 'user' }) => {
      const res = await apiFetch('/v1/oauth/claude/start', {
        method: 'POST',
        body: JSON.stringify({ layer, owner_type: ownerType }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to start OAuth')
      }
      return res.json() as Promise<{ url: string; state: string }>
    },
  })
}

export function useExchangeClaudeOAuth() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ code, state, layer, label }: { code: string; state: string; layer: string; label?: string }) => {
      const res = await apiFetch('/v1/oauth/claude/exchange', {
        method: 'POST',
        body: JSON.stringify({ code, state, layer, label }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to exchange OAuth code')
      }
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-execution-layers'] })
      qc.invalidateQueries({ queryKey: ['user-execution-layers'] })
    },
  })
}

// ---------------------------------------------------------------------------
// OpenAI OAuth flow (server-side codex login)
// ---------------------------------------------------------------------------

export function useStartOpenAIOAuth() {
  return useMutation({
    mutationFn: async ({ layer, ownerType }: { layer: string; ownerType: 'platform' | 'user' }) => {
      const res = await apiFetch('/v1/oauth/openai/start', {
        method: 'POST',
        body: JSON.stringify({ layer, owner_type: ownerType }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to start OpenAI login')
      }
      return res.json() as Promise<{ url: string; user_code: string; login_id: string }>
    },
  })
}

export function useOpenAIOAuthStatus() {
  return useMutation({
    mutationFn: async ({ loginId }: { loginId: string }) => {
      const res = await apiFetch(`/v1/oauth/openai/status?login_id=${encodeURIComponent(loginId)}`)
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Status check failed')
      }
      return res.json() as Promise<{ status: 'pending' | 'completed' | 'failed'; message?: string }>
    },
  })
}

export function useFinishOpenAIOAuth() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ loginId, layer, label }: { loginId: string; layer: string; label?: string }) => {
      const res = await apiFetch('/v1/oauth/openai/finish', {
        method: 'POST',
        body: JSON.stringify({ login_id: loginId, layer, label }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to finish OpenAI login')
      }
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-execution-layers'] })
      qc.invalidateQueries({ queryKey: ['user-execution-layers'] })
    },
  })
}

// ---------------------------------------------------------------------------
// User hooks
// ---------------------------------------------------------------------------

export const useUserExecutionLayers = () =>
  useQuery({
    queryKey: ['user-execution-layers'],
    queryFn: async (): Promise<UserLayerInfo[]> => {
      const res = await apiFetch('/v1/users/me/execution-layers')
      if (!res.ok) throw new Error('Failed to fetch execution layers')
      const data = await res.json()
      return data.layers ?? []
    },
  })

export function useUserDeleteSubscription() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ layer, id }: { layer: string; id: string }) => {
      const res = await apiFetch(`/v1/users/me/execution-layers/${layer}/subscriptions/${id}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error('Failed to delete subscription')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-execution-layers'] }),
  })
}
