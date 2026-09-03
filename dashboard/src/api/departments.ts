/**
 * Departments + agents-map data hooks.
 *
 * Departments are installation-wide org structure that the backend compiles
 * into delegation edges (source='department'); the map is a view over three
 * feeds: the viewer-scoped department list, the activity aggregate (heat +
 * live pulse) and the delegation-edge list. Visibility is membership-scoped
 * SERVER-side — these hooks never filter, they render what they get.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface DepartmentLevel {
  id: string
  rank: number // 0 = top
  name: string
}

export interface DepartmentMember {
  name: string
  display_name: string
  color: string
  description: string
  level_id: string
  /** false = grayed on the map: info popup only, never enterable. */
  accessible: boolean
}

export interface Department {
  id: string
  name: string
  created_by_sub: string
  auto_delegation: boolean
  reach: 'adjacent' | 'subtree'
  position_hint: string
  levels: DepartmentLevel[]
  members: DepartmentMember[]
  /** Computed server-side: admin, or creator who created this department. */
  can_edit: boolean
}

export interface AgentActivity {
  name: string
  messages_7d: number
  usage_records_7d: number
  task_runs_7d: number
  /** A session process exists right now (soft glow). */
  live: boolean
  /** An open turn is running right now (the hard pulse). */
  streaming: boolean
}

export interface DelegationEdge {
  from: string
  to: string
  source: 'manual' | 'department'
}

export const useDepartments = () =>
  useQuery({
    queryKey: ['departments'],
    queryFn: async (): Promise<Department[]> => {
      const res = await apiFetch('/v1/departments')
      if (!res.ok) throw new Error('Failed to fetch departments')
      return (await res.json()).departments ?? []
    },
    refetchInterval: 60_000,
  })

/** Heat + live pulse. Polled faster than the structural feeds — the pulse is
 * the "right now" signal on the map. */
export const useAgentsActivity = (enabled = true) =>
  useQuery({
    queryKey: ['agents-activity'],
    queryFn: async (): Promise<AgentActivity[]> => {
      const res = await apiFetch('/v1/agents/activity')
      if (!res.ok) throw new Error('Failed to fetch agent activity')
      return (await res.json()).agents ?? []
    },
    refetchInterval: 15_000,
    enabled,
  })

export const useDelegationEdges = (enabled = true) =>
  useQuery({
    queryKey: ['delegation-edges-all'],
    queryFn: async (): Promise<DelegationEdge[]> => {
      const res = await apiFetch('/v1/agents/delegation-edges')
      if (!res.ok) throw new Error('Failed to fetch delegation edges')
      return (await res.json()).edges ?? []
    },
    refetchInterval: 60_000,
    enabled,
  })

const invalidateMapFeeds = (qc: ReturnType<typeof useQueryClient>) => {
  qc.invalidateQueries({ queryKey: ['departments'] })
  qc.invalidateQueries({ queryKey: ['agents'] })
  qc.invalidateQueries({ queryKey: ['delegation-edges-all'] })
}

export function useCreateDepartment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: {
      name: string
      auto_delegation?: boolean
      reach?: 'adjacent' | 'subtree'
      levels?: string[]
    }) => {
      const res = await apiFetch('/v1/departments', {
        method: 'POST',
        body: JSON.stringify(data),
      })
      if (!res.ok) {
        const e = await res.json().catch(() => ({}))
        throw new Error(e.detail || 'Failed to create department')
      }
      return res.json() as Promise<Department>
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}

export function useUpdateDepartment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, ...fields }: {
      id: string
      name?: string
      auto_delegation?: boolean
      reach?: 'adjacent' | 'subtree'
      position_hint?: string
    }) => {
      const res = await apiFetch(`/v1/departments/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(fields),
      })
      if (!res.ok) {
        const e = await res.json().catch(() => ({}))
        throw new Error(e.detail || 'Failed to update department')
      }
      return res.json() as Promise<Department>
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}

export function useDeleteDepartment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id }: { id: string }) => {
      const res = await apiFetch(`/v1/departments/${id}`, { method: 'DELETE' })
      if (!res.ok) {
        const e = await res.json().catch(() => ({}))
        throw new Error(e.detail || 'Failed to delete department')
      }
      return res.json() as Promise<{ status: string; unassigned_agents: string[] }>
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}

export function useSetDepartmentLevels() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, levels }: {
      id: string
      /** id: '' for a new level; keeping an id preserves assignments. */
      levels: { id: string; name: string }[]
    }) => {
      const res = await apiFetch(`/v1/departments/${id}/levels`, {
        method: 'PUT',
        body: JSON.stringify({ levels }),
      })
      if (!res.ok) {
        const e = await res.json().catch(() => ({}))
        throw new Error(e.detail || 'Failed to save levels')
      }
      return res.json() as Promise<{
        levels: DepartmentLevel[]
        unassigned_agents: string[]
      }>
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}

/** Additive one-agent assignment — the admin info-popup "add me" CTA.
 * Deliberately NOT the full-set-replace admin PUT (clobber risk). */
export function useAdminAddUserAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ sub, agent, role = 'viewer' }: {
      sub: string
      agent: string
      role?: 'manager' | 'editor' | 'viewer'
    }) => {
      const res = await apiFetch(`/v1/admin/users/${sub}/agents/${agent}`, {
        method: 'POST',
        body: JSON.stringify({ role }),
      })
      if (!res.ok) {
        const e = await res.json().catch(() => ({}))
        throw new Error(e.detail || 'Failed to add agent')
      }
      return res.json() as Promise<{ status: string; agent: string }>
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}
