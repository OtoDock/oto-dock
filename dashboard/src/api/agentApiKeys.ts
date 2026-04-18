import { apiFetch } from './auth'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

export interface AgentApiKey {
  id: string
  agent: string
  name: string
  prefix: string
  permissions: string[]
  created_by: string
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface CreatedAgentApiKey extends AgentApiKey {
  key: string  // shown ONCE
}

export const useAgentApiKeys = (agent: string | undefined, includeRevoked = false) =>
  useQuery({
    queryKey: ['agent-api-keys', agent, includeRevoked],
    queryFn: async (): Promise<AgentApiKey[]> => {
      const qs = includeRevoked ? '?include_revoked=true' : ''
      const res = await apiFetch(`/v1/agents/${agent}/api-keys${qs}`)
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      return data.keys ?? []
    },
    enabled: !!agent,
    refetchInterval: 30_000,
  })

export const useCreateAgentApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { agent: string; name: string; permissions: string[] }) => {
      const res = await apiFetch(`/v1/agents/${args.agent}/api-keys`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: args.name, permissions: args.permissions }),
      })
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText)
      return (await res.json()) as CreatedAgentApiKey
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agent-api-keys'] }),
  })
}

export const useRevokeAgentApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { agent: string; keyId: string }) => {
      const res = await apiFetch(`/v1/agents/${args.agent}/api-keys/${args.keyId}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agent-api-keys'] }),
  })
}
