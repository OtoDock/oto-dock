import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiJson } from '../lib/http/apiJson'

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
  // Platform master switches, GET-only rider under a distinct key (the
  // per-agent toggles reuse the same field names). Lets agent managers grey
  // the UI without the admin-only /settings endpoint.
  master?: {
    user_memory_enabled: boolean
    agent_memory_enabled: boolean
  }
}

// ---------------------------------------------------------------------------
// Platform settings (admin only)
// ---------------------------------------------------------------------------

export const useMemorySettings = () =>
  useQuery<MemorySettings>({
    queryKey: ['memory', 'settings'],
    queryFn: () => apiJson<MemorySettings>('/v1/internal/memory/settings', {},
      'Could not load the memory settings'),
  })

export function useUpdateMemorySettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (patch: Partial<MemorySettings>) => {
      return apiJson<MemorySettings>('/v1/internal/memory/settings', {
        method: 'PATCH',
        json: patch,
      }, 'Could not save the memory settings')
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
    queryFn: () => apiJson<AgentMemorySettings>(`/v1/internal/memory/agent-settings/${agent}`, {},
      "Could not load the agent's memory settings"),
  })

export function useSetAgentMemoryToggle(agent: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: { key: string; value: boolean | string | null }) => {
      return apiJson<AgentMemorySettings>(`/v1/internal/memory/agent-settings/${agent}`, {
        method: 'PATCH',
        json: vars,
      }, "Could not save the agent's memory setting")
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
      return apiJson<{ files_unlinked: number; agent: string }>(
        `/v1/internal/memory/clear-agent-memory/${agent}`,
        { method: 'POST' },
        "Could not clear the agent's memory",
      )
    },
  })
}

export function useClearMyMemory() {
  return useMutation({
    mutationFn: async () => {
      return apiJson<{
        files_unlinked: number
        agents_affected: number
      }>('/v1/internal/memory/clear-my-memory', { method: 'POST' }, 'Could not clear your memory')
    },
  })
}
