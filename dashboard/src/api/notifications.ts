import { apiFetch } from './auth'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

export interface NotificationDefinition {
  id: string
  title: string
  body: string
  severity: 'info' | 'success' | 'warning' | 'danger'
  scope: string
  target: string | null
  source: string
  source_id: string | null
  notification_type: string
  schedule: string | null
  run_at: string | null
  // Recurring every N seconds. Mutually exclusive with schedule + run_at.
  interval_seconds: number | null
  created_by: string | null
  created_at: string
  enabled: number
  fired_count: number
  last_fired_at: string | null
  can_delete: boolean
  can_fire: boolean
  can_pause: boolean
  can_resume: boolean
}

// `audit` (admin-only, honored server-side) → the admin Notifications page's
// full-audit view (every user's notifications). Omit it for the per-agent
// settings tab, which shows the user-view (own user-scoped + agent-scoped + global).
export const useNotificationDefinitions = (agent?: string, opts?: { audit?: boolean }) =>
  useQuery({
    queryKey: ['notification-definitions', agent, opts?.audit ?? false],
    queryFn: async (): Promise<NotificationDefinition[]> => {
      const qs = new URLSearchParams({ view: 'definitions' })
      if (agent) qs.set('agent', agent)
      if (opts?.audit) qs.set('audit', 'true')
      const res = await apiFetch(`/v1/notifications?${qs}`)
      const data = await res.json()
      return data.notifications ?? []
    },
    refetchInterval: 30_000,
  })

export const useFireNotification = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (notificationId: string) => {
      const res = await apiFetch(`/v1/notifications/${notificationId}/fire`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notification-definitions'] })
    },
  })
}

export const useDeleteNotification = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (notificationId: string) => {
      const res = await apiFetch(`/v1/notifications/${notificationId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notification-definitions'] })
    },
  })
}

export const usePauseNotification = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (notificationId: string) => {
      const res = await apiFetch(`/v1/notifications/${notificationId}/pause`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notification-definitions'] })
    },
  })
}

export const useResumeNotification = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (notificationId: string) => {
      const res = await apiFetch(`/v1/notifications/${notificationId}/resume`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['notification-definitions'] })
    },
  })
}

export interface NotificationDelivery {
  id: string
  notification_id: string | null
  title: string
  body: string
  severity: 'info' | 'success' | 'warning' | 'danger'
  scope: string
  source: string
  delivered_at: string
  read: number
  dismissed: number
  read_at: string | null
  dismissed_at: string | null
  agent_slug: string | null
  chat_id: string | null
}

export async function fetchDeliveries(): Promise<NotificationDelivery[]> {
  const res = await apiFetch('/v1/notifications')
  const data = await res.json()
  return data.deliveries || []
}

export async function markRead(deliveryId: string): Promise<void> {
  await apiFetch(`/v1/notifications/deliveries/${deliveryId}/read`, { method: 'PATCH' })
}

export async function markAllRead(): Promise<void> {
  await apiFetch('/v1/notifications/mark-all-read', { method: 'POST' })
}

export async function dismissDelivery(deliveryId: string): Promise<void> {
  await apiFetch(`/v1/notifications/deliveries/${deliveryId}/dismiss`, { method: 'PATCH' })
}

export async function dismissAll(): Promise<void> {
  await apiFetch('/v1/notifications/dismiss-all', { method: 'POST' })
}

export async function subscribePush(platform: string, subscriptionData: string): Promise<void> {
  await apiFetch('/v1/push/subscribe', {
    method: 'POST',
    body: JSON.stringify({ platform, subscription_data: subscriptionData }),
  })
}

export async function getVapidPublicKey(): Promise<string> {
  const res = await apiFetch('/v1/push/vapid-public-key')
  const data = await res.json()
  return data.key
}
