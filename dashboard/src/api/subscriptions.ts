/**
 * Vendor webhook subscriptions.
 *
 * CRUD over the new `webhook_subscriptions` table. Subscriptions are the
 * vendor-side state for receiving Slack/GitHub/Linear/MS-Graph/Zoom
 * events. Triggers reference them via `subscription_id` + `event_filter`.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface WebhookSubscription {
  id: string
  scope: 'user' | 'service'
  owner: string
  agent: string | null
  mcp_name: string
  provider_id: string
  account_label: string
  vendor_target: string
  vendor_subscription_id: string | null
  selected_events: string[]
  selected_subevents: Record<string, string[]>
  status:
    | 'creating'
    | 'active'
    | 'failed'
    | 'renew_failed'
    | 'expired'
    | 'disabled'
  last_error: string | null
  last_event_at: string | null
  event_count: number
  expires_at: string | null
  created_by: string
  created_at: string
  updated_at: string
  /** 'vendor' = the vendor calls this install directly; 'relay' = events
   * arrive via the OtoDock relay (hosted delivery, no console steps). */
  delivery_mode: 'vendor' | 'relay'
}

export interface WebhookEventCatalogEntry {
  key: string
  label: string
  description?: string
  required_scopes?: string[]
  subevents?: string[]
  default_selected?: boolean
  /** 'bot' events reach the company bot install (admin/service-account). */
  delivery?: 'user' | 'bot'
  admin_only?: boolean
  /** MS-Graph-style resource pairing: this event applies when the chosen
   * static-list vendor target contains this substring (case-insensitive).
   * The subscribe modal filters + auto-selects events by it. */
  resource_contains?: string
}

export interface VendorTargetSpec {
  kind: 'free_text' | 'remote_list' | 'static_list'
  label: string
  placeholder?: string
  validation_regex?: string
  help_text?: string
  static_options?: Array<{ value: string; label: string }>
  // remote_list details are server-side; the dashboard fetches via
  // `/v1/mcps/{name}/webhook-vendor-targets` when kind='remote_list'.
}

export interface WebhookEventCatalogResponse {
  provider_id: string
  event_catalog: WebhookEventCatalogEntry[]
  vendor_target_spec: VendorTargetSpec
  /** DASHBOARD_PUBLIC_URL — base for manual-mode webhook URLs ('' = unset). */
  webhook_base: string
  /** EFFECTIVE mode for the queried account: relay (hosted, zero console
   * steps) / auto / manual. */
  registration: {
    mode: 'relay' | 'auto' | 'manual'
    manual_instructions_url?: string
  }
  per_subscription_secret: boolean
  /** Prefill for the vendor-target input (slack: the account's team_id). */
  vendor_target_prefill?: string
}

export interface CreateSubscriptionRequest {
  scope: 'user' | 'service'
  mcp_name: string
  account_label: string
  vendor_target: string
  selected_events: string[]
  selected_subevents?: Record<string, string[]>
  agent?: string // required for scope='service'
}

export interface SubscriptionFilters {
  scope?: 'user' | 'service'
  agent?: string
  mcp_name?: string
  provider_id?: string
  account_label?: string
}

export const useSubscriptions = (filters: SubscriptionFilters = {}) =>
  useQuery({
    queryKey: [
      'subscriptions',
      filters.scope,
      filters.agent,
      filters.mcp_name,
      filters.provider_id,
      filters.account_label,
    ],
    queryFn: async (): Promise<WebhookSubscription[]> => {
      const qs = new URLSearchParams()
      if (filters.scope) qs.set('scope', filters.scope)
      if (filters.agent) qs.set('agent', filters.agent)
      if (filters.mcp_name) qs.set('mcp_name', filters.mcp_name)
      if (filters.provider_id) qs.set('provider_id', filters.provider_id)
      if (filters.account_label !== undefined)
        qs.set('account_label', filters.account_label)
      const url = `/v1/subscriptions${qs.toString() ? `?${qs}` : ''}`
      const res = await apiFetch(url)
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      return data.subscriptions ?? []
    },
    refetchInterval: 30_000,
  })

export const useCreateSubscription = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: CreateSubscriptionRequest) => {
      const res = await apiFetch('/v1/subscriptions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      })
      if (!res.ok) {
        const text = await res.text()
        let message = text || res.statusText
        try {
          const parsed = JSON.parse(text)
          // Bubble structured errors (e.g. missing_scopes) to callers verbatim.
          // The throw lives OUTSIDE this try — throwing in here would be
          // swallowed by the catch and replaced with the raw JSON envelope.
          const detail = parsed.detail ?? parsed
          message = typeof detail === 'string' ? detail : JSON.stringify(detail)
        } catch {
          // Not JSON — keep the raw text.
        }
        throw new Error(message)
      }
      return res.json() as Promise<WebhookSubscription>
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['subscriptions'] })
    },
  })
}

export const useDeleteSubscription = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      const res = await apiFetch(`/v1/subscriptions/${id}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['subscriptions'] })
    },
  })
}

/**
 * Fetch the MCP's webhook event catalog + vendor_target_spec.
 *
 * Used by the Subscribe-to-events modal to render the event checkboxes
 * and the vendor-target input (free_text / static_list dropdowns; remote_list
 * picker is deferred until the matching list endpoint lands).
 */
export const useWebhookEventCatalog = (
  mcpName: string | undefined,
  opts: {
    accountLabel?: string
    scope?: 'user' | 'service'
    agent?: string | null
  } = {},
) =>
  useQuery({
    queryKey: [
      'webhook-event-catalog',
      mcpName,
      opts.accountLabel,
      opts.scope,
      opts.agent,
    ],
    queryFn: async (): Promise<WebhookEventCatalogResponse> => {
      const qs = new URLSearchParams()
      if (opts.accountLabel) qs.set('account_label', opts.accountLabel)
      if (opts.scope) qs.set('scope', opts.scope)
      if (opts.agent) qs.set('agent', opts.agent)
      const res = await apiFetch(
        `/v1/mcps/${encodeURIComponent(mcpName!)}/webhook-event-catalog` +
          (qs.toString() ? `?${qs}` : ''),
      )
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    enabled: !!mcpName,
    staleTime: 5 * 60_000,
  })

/** Reveal a manual-mode subscription's per-subscription signing secret. */
export const fetchSigningSecret = async (id: string): Promise<string> => {
  const res = await apiFetch(`/v1/subscriptions/${id}/signing-secret`)
  if (!res.ok) throw new Error(await res.text())
  const data = await res.json()
  return data.signing_secret ?? ''
}
