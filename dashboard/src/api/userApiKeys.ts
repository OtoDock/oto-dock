import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiJson } from '../lib/http/apiJson'

export interface UserApiKey {
  id: string
  name: string
  prefix: string
  permissions: string[]
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface CreatedUserApiKey extends UserApiKey {
  key: string  // shown ONCE
}

export const useUserApiKeys = (includeRevoked = false) =>
  useQuery({
    queryKey: ['user-api-keys', includeRevoked],
    queryFn: async (): Promise<UserApiKey[]> => {
      const qs = includeRevoked ? '?include_revoked=true' : ''
      const data = await apiJson<{ keys?: UserApiKey[] }>(`/v1/user-api-keys${qs}`, {},
        'Could not load the API keys')
      return data.keys ?? []
    },
    refetchInterval: 30_000,
  })

export const useCreateUserApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { name: string; permissions: string[] }) => {
      return apiJson<CreatedUserApiKey>('/v1/user-api-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(args),
      })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-api-keys'] }),
  })
}

export const useRevokeUserApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (keyId: string) => {
      return apiJson(`/v1/user-api-keys/${keyId}`, { method: 'DELETE' }, 'Could not revoke the key')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-api-keys'] }),
  })
}
