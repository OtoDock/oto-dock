import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface MemorySettings {
  id: number
  user_memory_enabled: boolean
  agent_memory_enabled: boolean
  // Per scope: topic files inject in FULL into the system prompt while
  // their total stays under this budget; past it only the generated
  // MEMORY.md index is injected (agents read topics via the memory tool).
  inline_budget_bytes: number
  // User turns without a memory tool call before a one-line capture
  // reminder rides the next message (0 = off).
  nudge_turns: number
}

export interface AgentMemorySettings {
  agent: string
  user_memory_enabled: boolean
  agent_memory_enabled: boolean
}

// ---------------------------------------------------------------------------
// Platform settings (admin only)
// ---------------------------------------------------------------------------

export const useMemorySettings = () =>
  useQuery<MemorySettings>({
    queryKey: ['memory', 'settings'],
    queryFn: async () => {
      const r = await apiFetch('/v1/internal/memory/settings')
      if (!r.ok) throw new Error(await r.text())
      return r.json()
    },
  })

export function useUpdateMemorySettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (patch: Partial<MemorySettings>) => {
      const r = await apiFetch('/v1/internal/memory/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      if (!r.ok) throw new Error(await r.text())
      return r.json() as Promise<MemorySettings>
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['memory', 'settings'] }),
  })
}

// ---------------------------------------------------------------------------
// Per-agent settings (managers + admin)
// ---------------------------------------------------------------------------

export const useAgentMemorySettings = (agent: string | undefined) =>
  useQuery<AgentMemorySettings>({
    queryKey: ['memory', 'agent-settings', agent],
    enabled: !!agent,
    queryFn: async () => {
      const r = await apiFetch(`/v1/internal/memory/agent-settings/${agent}`)
      if (!r.ok) throw new Error(await r.text())
      return r.json()
    },
  })

export function useSetAgentMemoryToggle(agent: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { key: string; value: boolean | string | null }) => {
      const r = await apiFetch(`/v1/internal/memory/agent-settings/${agent}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(vars),
      })
      if (!r.ok) throw new Error(await r.text())
      return r.json() as Promise<AgentMemorySettings>
    },
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ['memory', 'agent-settings', agent] }),
  })
}

// ---------------------------------------------------------------------------
// Clear memory (per-agent: manager/admin · per-user: self)
// ---------------------------------------------------------------------------

export function useClearAgentMemory(agent: string) {
  return useMutation({
    mutationFn: async () => {
      const r = await apiFetch(`/v1/internal/memory/clear-agent-memory/${agent}`, {
        method: 'POST',
      })
      if (!r.ok) throw new Error(await r.text())
      return r.json() as Promise<{ files_unlinked: number; agent: string }>
    },
  })
}

export function useClearMyMemory() {
  return useMutation({
    mutationFn: async () => {
      const r = await apiFetch('/v1/internal/memory/clear-my-memory', {
        method: 'POST',
      })
      if (!r.ok) throw new Error(await r.text())
      return r.json() as Promise<{
        files_unlinked: number
        agents_affected: number
      }>
    },
  })
}
