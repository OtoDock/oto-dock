import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface Run {
  id: string
  task_id: string
  agent: string
  trigger_type: string
  trigger_source: string | null
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'limit_exceeded'
  started_at: string | null
  completed_at: string | null
  duration_ms: number | null
  prompt_preview: string | null
  prompt_text: string | null
  output_text: string | null
  error_message: string | null
  session_id: string | null
  task_type: string | null
  cost_usd: number
  chat_id: string | null
  /** 'user' for tasks created in the user's scope; 'agent' for agent-scoped
   * tasks (the common case for triggers, schedules, internal agents).
   * Drives the workspace overlay's chip filter on task chats. */
  scope?: 'user' | 'agent'
  delegating_agent?: string
  delegating_agent_display_name?: string
  delegating_agent_color?: string
  session_cost_usd?: number
  session_turn_count?: number
  /** The run chat's last response landed after its read marker (server-computed,
   * same rule as the sidebar chat dot). Cleared by viewing the run's chat (the
   * chat page sends chat_read). Runs without a chat are never unread. */
  unread?: boolean
}

export interface RunsResponse {
  runs: Run[]
  total: number
  limit: number
  offset: number
}

// `audit` (admin-only, honored server-side) → the admin Task History page's
// full-audit view (every user's runs). Omit it for the per-agent settings tab,
// which shows the user-view (own user-scoped + agent-scoped).
export const useRuns = (filters?: {
  agent?: string
  status?: string
  task_id?: string
  session_id?: string
  created_by?: string
  audit?: boolean
  /** Include delegate-type (background job) runs — the per-agent Task
      History passes this: task-surface delegations are documented as
      "visible in the agent's History". */
  include_delegates?: boolean
  limit?: number
  offset?: number
} | undefined) =>
  useQuery({
    queryKey: ['runs', filters],
    queryFn: async (): Promise<RunsResponse> => {
      const params = new URLSearchParams()
      if (filters?.agent) params.set('agent', filters.agent)
      if (filters?.status) params.set('status', filters.status)
      if (filters?.task_id) params.set('task_id', filters.task_id)
      if (filters?.session_id) params.set('session_id', filters.session_id)
      if (filters?.created_by) params.set('created_by', filters.created_by)
      if (filters?.audit) params.set('audit', 'true')
      if (filters?.include_delegates) params.set('include_delegates', 'true')
      if (filters?.limit) params.set('limit', String(filters.limit))
      if (filters?.offset) params.set('offset', String(filters.offset))
      const res = await apiFetch(`/v1/tasks/runs?${params}`)
      return res.json()
    },
    enabled: filters !== undefined,
    refetchInterval: 15_000,
  })

export interface AdminUser {
  sub: string
  name: string
  email: string
  role: string
}

export const useAdminUsers = () =>
  useQuery({
    queryKey: ['admin-users'],
    queryFn: async (): Promise<AdminUser[]> => {
      const res = await apiFetch('/v1/admin/users')
      if (!res.ok) return []
      const data = await res.json()
      return data.users ?? []
    },
    staleTime: 60_000,
  })

export class ForbiddenError extends Error {
  constructor(message = 'Not authorized') { super(message); this.name = 'ForbiddenError' }
}

export const useRun = (runId: string) =>
  useQuery({
    queryKey: ['run', runId],
    queryFn: async (): Promise<Run> => {
      const res = await apiFetch(`/v1/tasks/runs/${runId}`)
      if (res.status === 403) throw new ForbiddenError()
      if (!res.ok) throw new Error('Run not found')
      return res.json()
    },
    refetchInterval: (query) => {
      const data = query.state.data as Run | undefined
      return data?.status === 'running' || data?.status === 'pending' ? 3_000 : false
    },
  })

/** The latest run behind a task-run chat — feeds the TaskMetadata popup when
 * the chat page shows a `task-…` chat. Enabled only for task chat ids. */
export const useRunByChat = (chatId: string | null) =>
  useQuery({
    queryKey: ['run-by-chat', chatId],
    queryFn: async (): Promise<Run> => {
      const res = await apiFetch(`/v1/tasks/runs/by-chat/${chatId}`)
      if (res.status === 403) throw new ForbiddenError()
      if (!res.ok) throw new Error('Run not found')
      return res.json()
    },
    enabled: !!chatId && chatId.startsWith('task-'),
    refetchInterval: (query) => {
      const data = query.state.data as Run | undefined
      return data?.status === 'running' || data?.status === 'pending' ? 3_000 : false
    },
  })

