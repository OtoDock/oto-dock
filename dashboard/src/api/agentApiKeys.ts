import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiJson } from '../lib/http/apiJson'

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
      const data = await apiJson<{ keys?: AgentApiKey[] }>(`/v1/agents/${agent}/api-keys${qs}`, {},
        'Could not load the API keys')
      return data.keys ?? []
    },
    enabled: !!agent,
    refetchInterval: 30_000,
  })

export const useCreateAgentApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { agent: string; name: string; permissions: string[] }) => {
      return apiJson<CreatedAgentApiKey>(`/v1/agents/${args.agent}/api-keys`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: args.name, permissions: args.permissions }),
      })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agent-api-keys'] }),
  })
}

export const useRevokeAgentApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { agent: string; keyId: string }) => {
      return apiJson(`/v1/agents/${args.agent}/api-keys/${args.keyId}`, {
        method: 'DELETE',
      }, 'Could not revoke the key')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['agent-api-keys'] }),
  })
}
