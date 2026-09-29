/**
 * Execution Layer Subscription & Model management API hooks.
 *
 * Admin: platform subscriptions + models + pool status + user auth toggle.
 * User:  personal subscriptions + platform availability.
 */

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import type { EngineDescriptor } from './engineDescriptor'
import type { EngineSubscriptionStatus } from '../lib/status/engineSubscription'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** One rate-limit window of an OAuth account as the vendor reports it. */
export interface SubscriptionWindow {
  pct: number             // 0..100 (above 100 when the vendor says so)
  resets_at: string | null
}

/** The vendor's own reading of an OAuth account's usage windows (Claude and
 *  ChatGPT both cap a subscription with a rolling session window and a
 *  rolling weekly one; Claude adds per-model weekly windows). Present on
 *  OAuth rows while the platform reads it; null until the first sample. */
export interface SubscriptionWindows {
  five_hour: SubscriptionWindow | null
  seven_day: SubscriptionWindow | null
  scoped: Array<SubscriptionWindow & { key: string; label: string; active: boolean }>
  reached: string         // '' | 'five_hour' | 'seven_day' | 'scoped:<key>'
  plan: string
  observed_at: string
  source: string
}

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
  oauth_email: string
  active_sessions: number
  status: EngineSubscriptionStatus
  created_at: string
  updated_at: string
  // Absent on non-OAuth rows and while the platform setting is off.
  windows?: SubscriptionWindows | null
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
  // Capability tier (1 = frontier … 4 = fast; null = untiered) and the
  // one-line "good at". Builtins carry the platform's; admins tag custom
  // rows. Optional: absent on an older proxy.
  tier?: number | null
  good_at?: string
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
  // The engine's descriptor — the card reads its vendor, auth types, login
  // flow, pricing policy from here (lib/engines.ts), never from `name`.
  capabilities: EngineDescriptor
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
  // The engine's descriptor rides with the row (as on the admin tab), so the
  // user card reads the vendor, the account label, the auth types and the
  // login flow from it instead of comparing the engine id.
  capabilities: EngineDescriptor
  user_subscriptions: Subscription[]
  platform_available: boolean
  allow_platform_auth: boolean
  // Server-computed "can this user run this engine" (own sub OR borrowable
  // platform credential) — the single predicate behind the chat page's
  // engine/model filtering and the cross-engine switch options. Optional:
  // absent on stale caches from an older proxy → treat as true (no filtering).
  can_run?: boolean
}

// ---------------------------------------------------------------------------
// Admin hooks
// ---------------------------------------------------------------------------

// Every subscription / model / engine mutation refreshes all three readers:
// the admin tab, the user's own engines, AND the public execution-layers
// query the chat and agent-settings model pickers read. That last one is
// cached for five minutes (useExecutionLayers), so a mutation that only
// touched the admin key left a newly discovered model invisible until a
// full reload.
const ENGINE_QUERY_KEYS = ['admin-execution-layers', 'user-execution-layers', 'execution-layers'] as const

function invalidateEngineQueries(qc: ReturnType<typeof useQueryClient>) {
  for (const key of ENGINE_QUERY_KEYS) qc.invalidateQueries({ queryKey: [key] })
}

/** One self-hosted OpenAI-compatible endpoint, listed once across the engines
 *  that can use it. `engines` holds the per-engine subscription row (absent =
 *  not enabled for that engine). */
export interface LocalEndpointGroup {
  group: string
  provider: string
  endpoint_url: string
  label: string
  has_api_key: boolean
  engines: Record<string, { id: string; status: EngineSubscriptionStatus; active_sessions: number; is_mine: boolean }>
}

interface AdminEnginesPayload {
  layers: ExecutionLayerInfo[]
  local_endpoints: LocalEndpointGroup[]
}

const fetchAdminEngines = async (): Promise<AdminEnginesPayload> => {
  const res = await apiFetch('/v1/admin/execution-layers')
  if (!res.ok) throw new Error('Failed to fetch execution layers')
  const data = await res.json()
  return { layers: data.layers ?? [], local_endpoints: data.local_endpoints ?? [] }
}

export const useAdminExecutionLayers = () =>
  useQuery({
    queryKey: ['admin-execution-layers'],
    queryFn: fetchAdminEngines,
    select: (d) => d.layers,
  })

export const useAdminLocalEndpoints = () =>
  useQuery({
    queryKey: ['admin-execution-layers'],
    queryFn: fetchAdminEngines,
    select: (d) => d.local_endpoints,
  })

export function useAddLocalEndpoint() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: {
      provider: string
      label?: string
      endpoint_url: string
      api_key?: string
      layers: string[]
    }) => {
      const res = await apiFetch('/v1/admin/execution-layers/local-endpoints', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add local endpoint')
      }
      return res.json() as Promise<LocalEndpointGroup>
    },
    onSuccess: () => invalidateEngineQueries(qc),
  })
}

export function useSetLocalEndpointEngine() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ group, layer, enabled }: { group: string; layer: string; enabled: boolean }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/local-endpoints/${encodeURIComponent(group)}`, {
        method: 'PUT',
        body: JSON.stringify({ layer, enabled }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to update local endpoint')
      }
      return res.json() as Promise<LocalEndpointGroup>
    },
    onSuccess: () => invalidateEngineQueries(qc),
  })
}

export function useDeleteLocalEndpoint() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ group, force }: { group: string; force?: boolean }) => {
      const qs = force ? '?force=true' : ''
      const res = await apiFetch(`/v1/admin/execution-layers/local-endpoints/${encodeURIComponent(group)}${qs}`, {
        method: 'DELETE',
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw Object.assign(new Error(err.detail || 'Failed to remove local endpoint'), { status: res.status })
      }
      return res.json()
    },
    onSuccess: () => invalidateEngineQueries(qc),
  })
}

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
    onSuccess: () => invalidateEngineQueries(qc),
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
    onSettled: () => invalidateEngineQueries(qc),
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
    onSettled: () => invalidateEngineQueries(qc),
  })
}

export function useDeleteSubscription() {
  const qc = useQueryClient()
  return useMutation({
    // `force` deletes past live sessions: the server re-homes them onto the
    // remaining subscriptions. A stale counter alone never blocks — the
    // server reconciles it against the live bindings first.
    mutationFn: async ({ layer, id, force }: { layer: string; id: string; force?: boolean }) => {
      const qs = force ? '?force=true' : ''
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/subscriptions/${id}${qs}`, {
        method: 'DELETE',
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw Object.assign(new Error(err.detail || 'Failed to delete subscription'), { status: res.status })
      }
      return res.json()
    },
    onSuccess: () => invalidateEngineQueries(qc),
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
    onSuccess: () => invalidateEngineQueries(qc),
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
      // `tier: null` clears the tier; custom rows only (a builtin's tier
      // comes with the platform and the server refuses the edit).
      tier?: number | null
      good_at?: string
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/models/${id}`, {
        method: 'PUT',
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('Failed to update model')
      return res.json()
    },
    onSuccess: () => invalidateEngineQueries(qc),
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
    onSuccess: () => invalidateEngineQueries(qc),
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
      layers,
    }: {
      layer: string
      models: { model_id: string; display_name: string }[]
      provider: string
      layers?: string[]
    }) => {
      const res = await apiFetch(`/v1/admin/execution-layers/${layer}/models/bulk`, {
        method: 'POST',
        body: JSON.stringify({ models, provider, ...(layers?.length ? { layers } : {}) }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add models')
      }
      return res.json()
    },
    onSuccess: () => invalidateEngineQueries(qc),
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

/** What a code paste did: `created` is false when the login matched an
 * account already connected (its tokens were refreshed, nothing added);
 * `previous_status` is that row's status before the refresh. */
export interface ClaudeExchangeResult {
  subscription: Subscription
  subscription_type: string
  rate_limit_tier: string
  created: boolean
  previous_status: string | null
}

/** A refused exchange. The state was consumed by the attempt, so the forms
 * start a fresh flow before offering another try; a 403 (another user's
 * state) is not retried. */
export class ClaudeExchangeError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ClaudeExchangeError'
    this.status = status
  }
}

export function useExchangeClaudeOAuth() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ code, state, layer, label }: { code: string; state: string; layer: string; label?: string }): Promise<ClaudeExchangeResult> => {
      const res = await apiFetch('/v1/oauth/claude/exchange', {
        method: 'POST',
        body: JSON.stringify({ code, state, layer, label }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new ClaudeExchangeError(res.status, err.detail || 'Failed to exchange OAuth code')
      }
      return res.json()
    },
    onSuccess: () => invalidateEngineQueries(qc),
  })
}

/** The sentence both connect forms show above the code box: the popup's
 * session decides which account is connected. */
export const CLAUDE_SECOND_ACCOUNT_HINT =
  'The popup signs you in on platform.claude.com; to add a second account, sign out of your Claude account there first or use a private window.'

/** The message when a paste matched an account that is already connected. */
export function alreadyConnectedMessage(email: string): string {
  return (
    `That is the account you already connected (${email || 'the same account'}). ` +
    'Its login was refreshed and no new account was added. To add a different account, ' +
    'sign out of platform.claude.com in this browser first, or use a private window, then start again.'
  )
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

/** A finish that answers "not found" met a login an earlier finish already
 *  consumed, with the subscription stored; any other refusal is a real one
 *  and belongs on the card. */
export function loginAlreadyFinished(err: unknown): boolean {
  return err instanceof Error && /not found/i.test(err.message)
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
    onSuccess: () => invalidateEngineQueries(qc),
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

/** A user's own API key on one of the two CLI engines (the server accepts
 *  api_key only there). The user card sends `contribute_platform: false`
 *  explicitly so an admin's personal key never defaults into the agent pool. */
export function useUserAddSubscription() {
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
      contribute_platform?: boolean
    }) => {
      const res = await apiFetch(`/v1/users/me/execution-layers/${layer}/subscriptions`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to add the API key')
      }
      return res.json()
    },
    onSuccess: () => invalidateEngineQueries(qc),
  })
}

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
    onSuccess: () => invalidateEngineQueries(qc),
  })
}
