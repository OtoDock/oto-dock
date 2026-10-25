import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { apiJson, withMessage } from '../lib/http/apiJson'
import type { ReachRepositoriesResponse } from './serviceAccounts'

export interface CredentialField {
  key: string
  label: string
  input_type: 'text' | 'password' | 'email' | 'number' | 'multiline_secret'
  // false: the field may stay empty (the manifest's "required": false).
  required?: boolean
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
  /** The admin credential kind a connect goes through now (lib/kinds/oauthApp.ts);
   * absent on a provider with one kind. */
  app_kind?: string
  /** A GitHub App whose install page can be offered. */
  app_install_available?: boolean
  /** The kinds a browser connect may pick, the default first: OtoDock's
   * relay offers both of GitHub's, a self-managed install the one its admin
   * registered. Absent on a provider with one kind. */
  connect_kinds?: string[]
  /** The app is OtoDock's own, through the hosted relay. */
  app_hosted?: boolean
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

/** Where a GitHub App is installed, as the account's meta keeps it. */
export interface AccountInstallation {
  id: number
  account: string
  account_id?: number
  target_type: string
  repository_selection: string
  repositories: string[]
  /** The listing stopped at its cap: there are more than it names. */
  repositories_truncated?: boolean
  suspended_at?: string | null
  /** The person owns the installation's target (only then can it be an agent's identity). */
  admin: boolean
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

/** A person's own GitHub reach for one agent they chose this account for:
 * the repositories it may reach ([] = every one) and whether it may only read. */
export interface AgentReach {
  agent: string
  repositories: string[]
  read_only: boolean
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
  /** The reach of each agent of ``agent_overrides`` (GitHub). */
  agent_reaches?: AgentReach[]
  missing_scopes: string[]
  service_bindings?: ServiceBindingSummary[]
  /** A GitHub-App account: the kind and the installations the person can reach. */
  kind?: string
  installations?: AccountInstallation[]
  // The token can no longer serve a session (a grant the vendor ended, an
  // expired token nobody can refresh, a file the MCP's way of issuing
  // tokens no longer covers): the card says reconnect, the resolver
  // leaves the MCP out of new sessions.
  needs_reconnect?: boolean
  reconnect_reason?: string
  // Required fields the saved account lacks (a field the manifest added
  // later): the MCP stays out of its sessions until they are filled.
  missing_required?: string[]
}

// What the card tells the person for each reconnect reason the server
// records.
export const RECONNECT_REASONS: Record<string, string> = {
  expired: 'its access expired and the vendor gave no way to renew it',
  revoked: 'the vendor ended the grant',
  mechanism_changed: "this MCP now signs you in through the vendor's own authorization server",
  registration_unavailable: "the install's client registration at the vendor is gone",
  issuer_changed: "the vendor's authorization server moved",
  app_key_rejected: "GitHub refused this install's app key. An admin must update the GitHub App credentials in Admin, MCP Servers",
  installation_gone: "the app is no longer installed where the agent's GitHub identity pointed",
  installation_suspended: "GitHub suspended the installation the agent's GitHub identity uses",
  installation_expired: "the installation's access expired and was not renewed yet",
  app_changed: "this install's GitHub credentials are no longer the GitHub App the account signed in through",
  installation_not_owned: "the account's owner no longer owns the installation the agent's GitHub identity uses",
  installation_hosted: 'hosted sign-in gives an agent no app identity. A manager binds the account itself in Agent Settings, MCPs',
  installation_reach_outdated: 'a repository the agent may reach is no longer in the installation. A manager picks the repositories again in Agent Settings, MCPs',
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
  /** A choice of account for an agent may narrow its GitHub reach. */
  reach_supported?: boolean
  /** The candidate agents in Shared-only mode: their chats use the agent's
   * own GitHub account, so a person's choice offers no repositories there. */
  shared_only_agents?: string[]
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
      return apiJson<Record<string, CredentialSchemaEntry>>('/v1/mcp-credential-schema')
        .catch(withMessage('Failed to load credential schema'))
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
      return apiJson(`/v1/users/me/integrations/${mcpName}`, {
        method: 'PUT',
        body: JSON.stringify({ credentials, account_label: accountLabel }),
      }, 'Failed to save credentials')
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
      return apiJson(
        `/v1/users/me/integrations/${mcpName}${qs}`,
        { method: 'DELETE' },
      ).catch(withMessage('Failed to delete credentials'))
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
      return apiJson(`/v1/users/me/integrations/${mcpName}/default-account`, {
          method: 'PUT',
          body: JSON.stringify({ account_label: accountLabel }),
        }, 'Failed to set default')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

export interface SetAccountAgentBindingResponse {
  status: string
  repositories?: string[]
  read_only?: boolean
}

export const useSetAccountAgentBinding = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({
      mcpName,
      agentName,
      accountLabel,
      repositories,
      readOnly,
    }: {
      mcpName: string
      agentName: string
      accountLabel: string
      /** The person's own GitHub reach for the agent; absent keeps it. */
      repositories?: string[]
      readOnly?: boolean
    }): Promise<SetAccountAgentBindingResponse> => {
      return apiJson<SetAccountAgentBindingResponse>(`/v1/users/me/integrations/${mcpName}/agent-binding`, {
          method: 'PUT',
          body: JSON.stringify({
            agent_name: agentName,
            account_label: accountLabel,
            ...(repositories !== undefined ? { repositories } : {}),
            ...(readOnly !== undefined ? { read_only: readOnly } : {}),
          }),
        }, 'Failed to bind agent')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-integrations'] }),
  })
}

/** What one of the person's own GitHub-App accounts reaches, for their own
 * choice of repositories per agent (the service picker's answer). */
export const useMyAccountRepositories = (mcpName: string, accountLabel: string, enabled: boolean) =>
  useQuery({
    queryKey: ['my-account-repositories', mcpName, accountLabel],
    queryFn: async (): Promise<ReachRepositoriesResponse> => {
      const q = new URLSearchParams({ account_label: accountLabel })
      return apiJson<ReachRepositoriesResponse>(
        `/v1/users/me/integrations/${encodeURIComponent(mcpName)}/repositories?${q.toString()}`,
        {},
        'Could not load the repositories',
      )
    },
    enabled,
  })

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
      return apiJson(
        `/v1/users/me/integrations/${mcpName}/agent-binding/${encodeURIComponent(agentName)}`,
        { method: 'DELETE' },
      ).catch(withMessage('Failed to remove agent binding'))
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
      return apiJson(`/v1/admin/integrations/infra/${mcpName}`, {
        method: 'PUT',
        body: JSON.stringify({ credentials }),
      }, 'Failed to save')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['admin-integrations'] }),
  })
}
