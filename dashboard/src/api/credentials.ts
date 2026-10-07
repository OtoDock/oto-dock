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
  pat_placeholder?: string
  // Provider-specific copy for the PAT option in the flow picker;
  // empty → the generic FLOW_DESCRIPTIONS fallback.
  pat_description?: string
  // The MCP server names its own authorization server: the install
  // registers itself there as a client and people sign in on the vendor's
  // consent page (manifest values only; the install's registrations are
  // an admin route).
  authorization_server?: AuthorizationServerMeta
}

export interface AuthorizationServerMeta {
  registration: 'dynamic'
  issuer: string
  resource_host: string
  confidential: boolean
  // The server also takes the vendor's app tokens (the relay's or an
  // admin's app): the registered client is then the fallback sign-in.
  accepts_app_tokens: boolean
  // On the integrations list only: whether a connect goes through the
  // registered client right now.
  active?: boolean
}

export interface OverridableConfigField {
  key: string
  label: string
  input_type: string
  default_value: string
}

// An agent whose service identity is this account (a manager bound it on the
// agent's MCPs tab). The account card offers "Subscribe as <agent>" from it;
// the server computed the two gates, the client never guesses a role.
export interface ServiceBindingSummary {
  agent_name: string
  display_name: string
  can_manage: boolean
  agent_scope_available: boolean
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
  service_bindings?: ServiceBindingSummary[]
  // The token can no longer serve a session (a grant the vendor ended, an
  // expired token nobody can refresh, a file the MCP's way of issuing
  // tokens no longer covers): the card says reconnect, the resolver
  // leaves the MCP out of new sessions.
  needs_reconnect?: boolean
  reconnect_reason?: string
}

// What the card tells the person for each reconnect reason the server
// records.
export const RECONNECT_REASONS: Record<string, string> = {
  expired: 'its access expired and the vendor issued no refresh token',
  revoked: 'the vendor ended the grant',
  mechanism_changed: "this MCP now signs you in through the vendor's own authorization server",
  registration_unavailable: "the install's client registration at the vendor is gone",
  issuer_changed: "the vendor's authorization server moved",
}

export function reconnectReasonText(reason?: string): string {
  return (reason && RECONNECT_REASONS[reason]) || 'the connection can no longer be refreshed'
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

// The credential schema of every installed MCP (the manifest's credential
// block as the proxy serializes it: ``oauth_meta`` carries the
// ``authorization_server`` declaration the admin card reads).
export interface CredentialSchemaEntry {
  type: string
  label?: string
  oauth?: boolean
  oauth_meta?: OAuthMeta
  app_credential?: string
}

export const useCredentialSchema = (enabled = true) =>
  useQuery({
    queryKey: ['mcp-credential-schema'],
    enabled,
    queryFn: async (): Promise<Record<string, CredentialSchemaEntry>> => {
      const res = await apiFetch('/v1/mcp-credential-schema')
      if (!res.ok) throw new Error('Failed to load credential schema')
      return res.json()
    },
  })

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
