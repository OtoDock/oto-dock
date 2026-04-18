import { apiFetch } from './auth'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

export interface Trigger {
  id: string
  slug: string
  name: string
  scope: 'user' | 'agent'
  agent: string
  created_by: string
  created_by_name?: string | null
  task_id: string | null
  task_name?: string | null
  notify_enabled: boolean
  notify_severity: string
  notify_title: string | null
  notify_body: string | null
  notify_target_scope: string | null
  notify_target: string | null
  debounce_seconds: number
  enabled: boolean
  fired_count: number
  last_fired_at: string | null
  last_error: string | null
  // Vendor-subscription linkage
  subscription_id: string | null
  event_filter: Record<string, unknown> | null
  created_at: string
  updated_at: string
  webhook_path?: string | null
  // Permission flags from API
  can_edit: boolean
  can_delete: boolean
  can_pause: boolean
  can_resume: boolean
  can_fire: boolean
}

export interface NotifyConfig {
  enabled?: boolean
  severity?: 'info' | 'success' | 'warning' | 'danger'
  title?: string
  body?: string
  target_scope?: 'user' | 'agent' | 'global'
  target?: string
}

export interface CreateTriggerRequest {
  name: string
  scope?: 'user' | 'agent'
  agent: string
  slug?: string
  task_id?: string
  notify?: NotifyConfig
  debounce_seconds?: number
  enabled?: boolean
  // When subscription_id is set, the trigger fires when the
  // linked vendor subscription receives an event matching event_filter
  // (instead of via a generic webhook URL).
  subscription_id?: string
  event_filter?: Record<string, unknown>
}

export interface EditTriggerRequest {
  name?: string
  task_id?: string | null
  notify_enabled?: boolean
  notify_severity?: string
  notify_title?: string | null
  notify_body?: string | null
  notify_target_scope?: string | null
  notify_target?: string | null
  debounce_seconds?: number
  event_filter?: Record<string, unknown> | null
}

// `audit` (admin-only, honored server-side) → the admin Triggers page's
// full-audit view (every user's items). Omit it for the per-agent settings tab,
// which shows the user-view (own user-scoped + agent-scoped).
export const useTriggers = (params: { agent?: string; scope?: string; audit?: boolean } = {}) =>
  useQuery({
    queryKey: ['triggers', params.agent, params.scope, params.audit ?? false],
    queryFn: async (): Promise<Trigger[]> => {
      const qs = new URLSearchParams()
      if (params.agent) qs.set('agent', params.agent)
      if (params.scope) qs.set('scope', params.scope)
      if (params.audit) qs.set('audit', 'true')
      const url = `/v1/triggers${qs.toString() ? `?${qs}` : ''}`
      const res = await apiFetch(url)
      const data = await res.json()
      return data.triggers ?? []
    },
    refetchInterval: 30_000,
  })

export const useCreateTrigger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (req: CreateTriggerRequest) => {
      const res = await apiFetch('/v1/triggers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      })
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText)
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['triggers'] }),
  })
}

export const useEditTrigger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { id: string; fields: EditTriggerRequest }) => {
      const res = await apiFetch(`/v1/triggers/${args.id}/edit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(args.fields),
      })
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText)
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['triggers'] }),
  })
}

export const useDeleteTrigger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      const res = await apiFetch(`/v1/triggers/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['triggers'] }),
  })
}

export const usePauseTrigger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      const res = await apiFetch(`/v1/triggers/${id}/pause`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['triggers'] }),
  })
}

export const useResumeTrigger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      const res = await apiFetch(`/v1/triggers/${id}/resume`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['triggers'] }),
  })
}

export const useFireTrigger = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { id: string; body?: Record<string, unknown> }) => {
      const res = await apiFetch(`/v1/triggers/${args.id}/fire`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(args.body ?? {}),
      })
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText)
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['triggers'] }),
  })
}
