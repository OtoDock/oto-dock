import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export interface CredentialField {
  key: string
  label: string
  input_type: 'text' | 'password' | 'email' | 'number'
}

export interface OAuthService {
  key: string
  label: string
  description: string
  scopes?: string[]
  capabilities?: string[]
  requires_admin_consent?: boolean
  requires_user_oauth?: boolean
}

export interface OAuthMeta {
  provider_id: string
  supports_multi_account: boolean
  registered_app_required: boolean
  bearer_required: boolean
  proposed_hosts: string[]
  // When an MCP exposes more than one OAuth flow (e.g. github-mcp's
  // ["authorization_code", "personal_access_token"]), the dashboard renders a
  // picker. Single-flow MCPs send a one-element list.
  flows?: string[]
  pat_instructions_url?: string
}

export interface OverridableConfigField {
  key: string
  label: string
  input_type: string
  default_value: string
}

// Multi-account: one entry per labeled account a user has connected for a
// given per-user MCP.
export interface AccountSummary {
  account_label: string
  display_email: string
  is_default: boolean
  created_at: string
  configured_keys: string[]
  connected_services: string[]
  agent_overrides: string[]
  missing_scopes: string[]
}

export interface Integration {
  mcp_name: string
  display_name: string
  description: string
  configured: boolean
  required_keys: string[]
  fields: CredentialField[]
  oauth?: boolean
  oauth_services?: OAuthService[]
  oauth_meta?: OAuthMeta
  supports_multi_account: boolean
  overridable_config?: OverridableConfigField[]
  accounts: AccountSummary[]
  candidate_agents: string[]
}

// ───────────────────────────────────────────────────────────────────
// User integrations (multi-account aware)
// ───────────────────────────────────────────────────────────────────

export const useMyIntegrations = () =>
  useQuery({
    queryKey: ['my-integrations'],
    queryFn: async (): Promise<Integration[]> => {
      const res = await apiFetch('/v1/users/me/integrations')
      return res.json()
    },
  })

export const useSetIntegration = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      credentials,
      accountLabel = 'default',
    }: {
      mcpName: string
      credentials: Record<string, string>
      accountLabel?: string
    }) => {
      const res = await apiFetch(`/v1/users/me/integrations/${mcpName}`, {
        method: 'PUT',
        body: JSON.stringify({ credentials, account_label: accountLabel }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to save credentials')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

export const useDeleteIntegration = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      accountLabel,
    }: {
      mcpName: string
      accountLabel: string
    }) => {
      const qs = `?account_label=${encodeURIComponent(accountLabel)}`
      const res = await apiFetch(
        `/v1/users/me/integrations/${mcpName}${qs}`,
        { method: 'DELETE' },
      )
      if (!res.ok) throw new Error('Failed to delete credentials')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

// ───────────────────────────────────────────────────────────────────
// Multi-account: default + per-agent binding
// ───────────────────────────────────────────────────────────────────

export const useSetDefaultAccount = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      accountLabel,
    }: {
      mcpName: string
      accountLabel: string
    }) => {
      const res = await apiFetch(
        `/v1/users/me/integrations/${mcpName}/default-account`,
        {
          method: 'PUT',
          body: JSON.stringify({ account_label: accountLabel }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to set default')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

export const useSetAccountAgentBinding = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      agentName,
      accountLabel,
    }: {
      mcpName: string
      agentName: string
      accountLabel: string
    }) => {
      const res = await apiFetch(
        `/v1/users/me/integrations/${mcpName}/agent-binding`,
        {
          method: 'PUT',
          body: JSON.stringify({
            agent_name: agentName,
            account_label: accountLabel,
          }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to bind agent')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

export const useRemoveAccountAgentBinding = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      agentName,
    }: {
      mcpName: string
      agentName: string
    }) => {
      const res = await apiFetch(
        `/v1/users/me/integrations/${mcpName}/agent-binding/${encodeURIComponent(agentName)}`,
        { method: 'DELETE' },
      )
      if (!res.ok) throw new Error('Failed to remove agent binding')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

// ───────────────────────────────────────────────────────────────────
// Admin integrations
// ───────────────────────────────────────────────────────────────────

export const useSetInfraCredentials = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      credentials,
    }: {
      mcpName: string
      credentials: Record<string, string>
    }) => {
      const res = await apiFetch(`/v1/admin/integrations/infra/${mcpName}`, {
        method: 'PUT',
        body: JSON.stringify({ credentials }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to save')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-integrations'] }),
  })
}
