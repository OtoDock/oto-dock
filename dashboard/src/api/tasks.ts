import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface Task {
  /** Set on a row the offboarding transfer moved: the first creator's sub,
   *  when, and the name to show (live display name, else retired username). */
  transferred_from?: string
  transferred_at?: string
  transferred_from_name?: string
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
  // Per-task execution pins, set by an agent through the schedules MCP.
  // Empty/null = this task inherits the agent's default. The effective_*
  // fields are server-computed for display: the pin when there is one, else
  // the agent's currently-resolved default ('' when the install has no
  // enabled model for that agent's layer).
  override_model: string | null
  override_execution_path: string | null
  effective_model: string
  effective_execution_path: string
  // Where the effective model came from ('pinned' | 'agent default' |
  // 'layer default'), its capability tier (1 = frontier … 4 = fast, null =
  // untiered) and the warnings for a pin that no longer resolves. Optional:
  // absent on an older proxy.
  effective_model_source?: string
  effective_model_tier?: number | null
  tier_label?: string
  pin_warnings?: string[]
  // A definition's kind — the words of TASK_KIND in lib/kinds/task.ts
  // (scheduled, one_time, trigger, continuation, app, delegate); absent on
  // an older proxy.
  task_type?: string
  // The zone the cron and a naive run_at are read in: the row's own
  // (user_tz, null = the platform's) and the resolved one (effective_tz,
  // server-computed); the schedule in words with that zone named when it
  // carries a clock time (schedule_text). Optional: absent on an older proxy.
  user_tz?: string | null
  effective_tz?: string
  schedule_text?: string
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

// A refusal's `detail` is a sentence for the person (the task and
// continuation caps answer 429 with one); anything else is shown as sent.
export async function readError(res: Response): Promise<string> {
  const text = await res.text()
  try {
    const d = JSON.parse(text)
    if (typeof d?.detail === 'string') return d.detail
  } catch {
    // not JSON
  }
  return text || res.statusText
}

export const useRunTaskNow = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (taskId: string): Promise<{ run_id: string }> => {
      const res = await apiFetch(`/v1/tasks/${taskId}/run`, { method: 'POST' })
      if (!res.ok) throw new Error(await readError(res))
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
      if (!res.ok) throw new Error(await readError(res))
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
      if (!res.ok) throw new Error(await readError(res))
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
      if (!res.ok) throw new Error(await readError(res))
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['schedules'] })
    },
  })
}
