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
import { apiJson, withMessage } from '../lib/http/apiJson'
import type { AgentRole } from '../lib/permissions'
import type { DepartmentMode, DepartmentReach } from '../lib/kinds/department'

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
  /** Which links the department wires (lib/departments MODE_*); new
   * departments default to 'down'. */
  mode: DepartmentMode
  /** How many levels a wired link spans; independent of the mode. */
  reach: DepartmentReach
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
      const data = await apiJson<{ departments?: Department[] }>('/v1/departments')
        .catch(withMessage('Failed to fetch departments'))
      return data.departments ?? []
    },
    refetchInterval: 60_000,
  })

/** Heat + live pulse, as this viewer may see them (the server's Active-now
 * gates: never another person's personal chat). Polled faster than the
 * structural feeds — the pulse is the "right now" signal on the map; the
 * agents page holds no dashboard socket to keep it live otherwise. */
export const useAgentsActivity = (enabled = true) =>
  useQuery({
    queryKey: ['agents-activity'],
    queryFn: async (): Promise<AgentActivity[]> =>
      (await apiJson<{ agents?: AgentActivity[] }>('/v1/agents/activity', {}, 'Failed to fetch agent activity')).agents ?? [],
    refetchInterval: 15_000,
    enabled,
  })

export const useDelegationEdges = (enabled = true) =>
  useQuery({
    queryKey: ['delegation-edges-all'],
    queryFn: async (): Promise<DelegationEdge[]> => {
      const data = await apiJson<{ edges?: DelegationEdge[] }>('/v1/agents/delegation-edges')
        .catch(withMessage('Failed to fetch delegation edges'))
      return data.edges ?? []
    },
    refetchInterval: 60_000,
    enabled,
  })

const invalidateMapFeeds = (qc: ReturnType<typeof useQueryClient>) => Promise.all([
  qc.invalidateQueries({ queryKey: ['departments'] }),
  qc.invalidateQueries({ queryKey: ['agents'] }),
  qc.invalidateQueries({ queryKey: ['delegation-edges-all'] }),
])

export function useCreateDepartment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: {
      name: string
      mode?: DepartmentMode
      reach?: DepartmentReach
      levels?: string[]
    }) => {
      return apiJson<Department>('/v1/departments', {
        method: 'POST',
        body: JSON.stringify(data),
      }, 'Failed to create department')
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
      mode?: DepartmentMode
      reach?: DepartmentReach
      position_hint?: string
    }) => {
      return apiJson<Department>(`/v1/departments/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(fields),
      }, 'Failed to update department')
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}

export function useDeleteDepartment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id }: { id: string }) => {
      return apiJson<{ status: string; unassigned_agents: string[] }>(`/v1/departments/${id}`, { method: 'DELETE' }, 'Failed to delete department')
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
      return apiJson<{
        levels: DepartmentLevel[]
        unassigned_agents: string[]
      }>(`/v1/departments/${id}/levels`, {
        method: 'PUT',
        body: JSON.stringify({ levels }),
      }, 'Failed to save levels')
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
      role?: AgentRole
    }) => {
      return apiJson<{ status: string; agent: string }>(`/v1/admin/users/${sub}/agents/${agent}`, {
        method: 'POST',
        body: JSON.stringify({ role }),
      }, 'Failed to add agent')
    },
    onSuccess: () => invalidateMapFeeds(qc),
  })
}
