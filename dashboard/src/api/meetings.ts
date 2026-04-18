import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface Meeting {
  id: string
  topic: string
  participants: string    // JSON array of agent slugs
  active_participants: string
  moderator: string
  strategy: string
  max_turns: number
  current_round: number
  status: string
  parent_chat_id: string
  parent_session_id: string | null
  parent_run_id: string | null
  scope: string
  created_by: string | null
  summary: string
  cost_usd: number
  created_at: string
  concluded_at: string | null
}

export interface MeetingsResponse {
  meetings: Meeting[]
  total: number
  limit: number
  offset: number
}

export const useMeetings = (filters?: {
  agent?: string
  status?: string
  created_by?: string
  limit?: number
  offset?: number
}) =>
  useQuery({
    queryKey: ['meetings', filters],
    queryFn: async (): Promise<MeetingsResponse> => {
      const params = new URLSearchParams()
      if (filters?.agent) params.set('agent', filters.agent)
      if (filters?.status) params.set('status', filters.status)
      if (filters?.created_by) params.set('created_by', filters.created_by)
      if (filters?.limit) params.set('limit', String(filters.limit))
      if (filters?.offset) params.set('offset', String(filters.offset))
      const res = await apiFetch(`/v1/meetings?${params}`)
      return res.json()
    },
    enabled: filters !== undefined,
    refetchInterval: 15_000,
  })
