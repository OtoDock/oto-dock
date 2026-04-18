import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface Task {
  id: string
  name: string
  agent: string
  schedule: string
  run_at: string | null
  delay_seconds: number | null
  // Recurring every N seconds. Mutually exclusive with schedule + run_at.
  interval_seconds: number | null
  llm_mode: string
  prompt: string
  enabled: boolean
  timeout_seconds: number
  next_run_time: string | null
  scope: 'user' | 'agent'
  created_by: string | null
  notification_mode: 'auto' | 'manual' | 'none'
  notify_severity: 'info' | 'success' | 'warning'
  can_run: boolean
  can_delete: boolean
  can_pause: boolean
  can_resume: boolean
}

export interface Stats {
  total_today: number
  running: number
  failed_today: number
  scheduled_tasks: number
  running_tasks: number
}

export interface ScheduledJob {
  id: string
  task_id: string
  name: string
  agent: string
  next_run_time: string | null
}

// `audit` (admin-only, honored server-side) → the admin Scheduled Tasks page's
// full-audit view (every user's items). Omit it for the per-agent settings tab,
// which shows the user-view (own user-scoped + agent-scoped).
export const useTasks = (agent?: string, opts?: { audit?: boolean }) =>
  useQuery({
    queryKey: ['tasks', agent, opts?.audit ?? false],
    queryFn: async (): Promise<Task[]> => {
      const qs = new URLSearchParams()
      if (agent) qs.set('agent', agent)
      if (opts?.audit) qs.set('audit', 'true')
      const res = await apiFetch(`/v1/tasks${qs.toString() ? `?${qs}` : ''}`)
      const data = await res.json()
      return data.tasks ?? []
    },
    refetchInterval: 30_000,
  })

export const useStats = () =>
  useQuery({
    queryKey: ['stats'],
    queryFn: async (): Promise<Stats> => {
      const res = await apiFetch('/v1/tasks/stats')
      return res.json()
    },
    refetchInterval: 30_000,
  })

export const useSchedules = () =>
  useQuery({
    queryKey: ['schedules'],
    queryFn: async (): Promise<ScheduledJob[]> => {
      const res = await apiFetch('/v1/schedules')
      const data = await res.json()
      return data.schedules ?? []
    },
    refetchInterval: 60_000,
  })

export const useRunTaskNow = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (taskId: string): Promise<{ run_id: string }> => {
      const res = await apiFetch(`/v1/tasks/${taskId}/run`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['stats'] })
    },
  })
}

export const useDeleteTask = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (taskId: string): Promise<void> => {
      const res = await apiFetch(`/v1/tasks/${taskId}/delete`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['schedules'] })
    },
  })
}

export const usePauseTask = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (taskId: string): Promise<void> => {
      const res = await apiFetch(`/v1/tasks/${taskId}/pause`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['schedules'] })
    },
  })
}

export const useResumeTask = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (taskId: string): Promise<void> => {
      const res = await apiFetch(`/v1/tasks/${taskId}/resume`, { method: 'POST' })
      if (!res.ok) throw new Error(await res.text())
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['schedules'] })
    },
  })
}
