import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ApiError, apiJson } from '../lib/http/apiJson'

/** The two kinds of key the admin's list holds. */
export const KEY_KIND = { user: 'user', agent: 'agent' } as const

/** How the server asks who is at the keyboard (`auth/confirm.py`). */
export const KEY_CONFIRM = { password: 'password', passkey: 'passkey', oidc: 'oidc', none: 'none' } as const

export interface UserApiKey {
  id: string
  name: string
  prefix: string
  permissions: string[]
  /** The agents the key reaches; empty: every agent the person holds. */
  agents: string[]
  /** Empty only on a key made before keys had an expiry. */
  expires_at: string | null
  run_without_asking: boolean
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface CreatedUserApiKey extends UserApiKey {
  key: string  // shown ONCE
}

export interface NewUserApiKey {
  name: string
  permissions: string[]
  agents: string[]
  expires_in_days: number
  run_without_asking: boolean
  password?: string
  confirm_token?: string
}

/** What a key made: its revocation lists them. */
export interface KeyCreation {
  kind: string
  ref: string
  label: string
  agent: string
  created_at: string
}

export interface KeyRevoked {
  status: string
  id: string
  created: KeyCreation[]
  created_total: number
}

/** The server asks who is at the keyboard before a key past `triggers`. */
export class KeyConfirmRequired extends ApiError {
  method: (typeof KEY_CONFIRM)[keyof typeof KEY_CONFIRM]
  provider: string

  constructor(e: ApiError, d: { method: KeyConfirmRequired['method']; provider?: string; message?: string }) {
    super(e.status, d.message || e.message, e.detail, e.body)
    this.name = 'KeyConfirmRequired'
    this.method = d.method
    this.provider = d.provider || ''
  }
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
    mutationFn: async (args: NewUserApiKey) => {
      try {
        return await apiJson<CreatedUserApiKey>('/v1/user-api-keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(args),
        })
      } catch (e) {
        const d = e instanceof ApiError ? (e.detail as { code?: string } | null) : null
        if (e instanceof ApiError && e.status === 428 && d?.code === 'confirm_required') {
          throw new KeyConfirmRequired(e, d as unknown as ConstructorParameters<typeof KeyConfirmRequired>[1])
        }
        throw e
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-api-keys'] }),
  })
}

export const useRevokeUserApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (keyId: string) => {
      return apiJson<KeyRevoked>(`/v1/user-api-keys/${keyId}`, { method: 'DELETE' }, 'Could not revoke the key')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['user-api-keys'] }),
  })
}

// ── the admin's list ─────────────────────────────────────────────────

export interface AdminApiKey {
  id: string
  kind: (typeof KEY_KIND)[keyof typeof KEY_KIND]
  name: string
  prefix: string
  permissions: string[]
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
  owner?: string
  owner_email?: string
  agents?: string[]
  expires_at?: string | null
  run_without_asking?: boolean
  agent?: string
}

export const useAdminApiKeys = () =>
  useQuery({
    queryKey: ['admin-api-keys'],
    queryFn: () => apiJson<{ enabled: boolean; keys: AdminApiKey[] }>('/v1/admin/api-keys', {},
      'Could not load the API keys'),
  })

export const useSetApiKeysEnabled = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) => apiJson('/v1/admin/api-keys/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }, 'The setting was not saved'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-api-keys'] }),
  })
}

export const useAdminRevokeApiKey = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (k: { kind: string; id: string }) => apiJson<KeyRevoked>(
      `/v1/admin/api-keys/${k.kind}/${k.id}`, { method: 'DELETE' }, 'Could not revoke the key'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-api-keys'] }),
  })
}
