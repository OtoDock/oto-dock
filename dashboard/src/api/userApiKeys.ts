import { apiFetch } from './auth'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'

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
      const res = await apiFetch(`/v1/user-api-keys${qs}`)
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      return data.keys ?? []
    },
    refetchInterval: 30_000,
  })

export const useCreateUserApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { name: string; permissions: string[] }) => {
      const res = await apiFetch('/v1/user-api-keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(args),
      })
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText)
      return (await res.json()) as CreatedUserApiKey
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-api-keys'] }),
  })
}

export const useRevokeUserApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (keyId: string) => {
      const res = await apiFetch(`/v1/user-api-keys/${keyId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-api-keys'] }),
  })
}
