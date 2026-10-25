/**
 * Per-agent service-account binding hooks.
 *
 * Backs the ``ServiceAccountBindingDropdown`` rendered next to each MCP row
 * in Agent Settings. A binding targets the caller's own
 * ``user_credential_accounts`` row — bind your personal account as the
 * agent's service identity for agent-scope sessions. The owner is always the
 * caller (derived server-side); there is no platform service-account tier.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiJson } from '../lib/http/apiJson'
import { eventCatalogKey } from './subscriptions'

export interface ServiceAccountInstallation {
  id: number
  account: string
  target_type: string
  repository_selection: string
  repositories_count: number
  /** The listing stopped at its cap: the count is a floor. */
  repositories_truncated?: boolean
  suspended: boolean
  admin: boolean
}

/** The bind's answer: a warning when the installation's first token could
 * not be minted (the binding stands; a session tries again). */
export interface SetBindingResponse {
  status: string
  installation_id?: string
  repositories?: string[]
  read_only?: boolean
  warning?: string
  /** A new binding of the caller's own account started from the reach the
   * caller chose for their own chats with the agent (a copy, once). */
  reach_copied?: boolean
}

export interface AgentServiceAccountOption {
  label: string
  display_email: string
  is_default: boolean
  /** A GitHub-App account: its kind (lib/kinds/oauthApp.ts) and installations. */
  kind?: string
  installations?: ServiceAccountInstallation[]
  /** Signed in through OtoDock's relay: bound as the account alone. */
  hosted?: boolean
}

export interface AgentServiceAccountOptionsResponse {
  my_accounts: AgentServiceAccountOption[]
  current_binding: {
    label: string
    owner_sub: string
    owner_name: string
    owner_email: string
    set_by: string
    set_at: string
    /** A GitHub App installation as the identity ('' = the account itself). */
    installation_id?: string
    installation_account?: string
    /** The agent's GitHub reach: the repositories it may reach ([] = every one)
     * and whether it may only read. */
    repositories?: string[]
    read_only?: boolean
    /** Why the binding cannot serve as it is, in the server's words. */
    problem?: string
  } | null
  /** The binding may carry a GitHub reach (the MCP is GitHub). */
  reach_supported?: boolean
}

/** One installation's repositories, as the reach picker offers them. */
export interface ReachInstallation {
  id: number
  account: string
  repositories: string[]
  /** The listing stopped at its cap: more repositories exist. */
  truncated: boolean
}

export interface ReachRepositoriesResponse {
  /** False for a token or OAuth account: no list to pick from. */
  pickable: boolean
  installations: ReachInstallation[]
  /** GitHub could not be reached: the lists OtoDock saw last. */
  from_meta: boolean
}

export const useAgentServiceRepositories = (
  agentName: string,
  mcpName: string,
  accountLabel: string,
  installationId: string,
  enabled: boolean,
) =>
  useQuery({
    queryKey: ['agent-service-repositories', agentName, mcpName, accountLabel, installationId],
    queryFn: async (): Promise<ReachRepositoriesResponse> => {
      const q = new URLSearchParams({ account_label: accountLabel, installation_id: installationId })
      return apiJson<ReachRepositoriesResponse>(
        `/v1/agents/${encodeURIComponent(agentName)}` +
          `/mcps/${encodeURIComponent(mcpName)}/service-account-repositories?${q.toString()}`,
        {},
        'Could not load the repositories',
      )
    },
    enabled,
  })

/** Whether the agent's binding last started from the binder's own reach:
 * set by each bind's answer, read by the reach editor's notice. */
const reachCopiedKey = (agentName: string, mcpName: string) =>
  ['agent-service-reach-copied', agentName, mcpName]

export const useReachCopied = (agentName: string, mcpName: string) =>
  useQuery({
    queryKey: reachCopiedKey(agentName, mcpName),
    queryFn: async () => false,
    enabled: false,
  }).data === true

export const useAgentServiceAccountOptions = (
  agentName: string | undefined,
  mcpName: string | undefined,
) =>
  useQuery({
    queryKey: ['agent-service-account-options', agentName, mcpName],
    queryFn: async (): Promise<AgentServiceAccountOptionsResponse> => {
      return apiJson<AgentServiceAccountOptionsResponse>(
        `/v1/agents/${encodeURIComponent(agentName!)}` +
          `/mcps/${encodeURIComponent(mcpName!)}/service-account-options`,
        {},
        'Could not load the accounts',
      )
    },
    enabled: !!agentName && !!mcpName,
  })

interface SetBindingVars {
  agentName: string
  mcpName: string
  accountLabel: string
  /** An installation of the account as the identity; '' the account itself;
   * absent keeps what the binding holds. */
  installationId?: string
  /** The agent's GitHub reach; absent keeps what the binding holds. */
  repositories?: string[]
  readOnly?: boolean
}

export const useSetAgentServiceBinding = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: SetBindingVars): Promise<SetBindingResponse> => {
      return apiJson<SetBindingResponse>(
        `/v1/agents/${encodeURIComponent(vars.agentName)}` +
          `/mcps/${encodeURIComponent(vars.mcpName)}/service-binding`,
        {
          method: 'PUT',
          json: {
            account_label: vars.accountLabel,
            ...(vars.installationId !== undefined ? { installation_id: vars.installationId } : {}),
            ...(vars.repositories !== undefined ? { repositories: vars.repositories } : {}),
            ...(vars.readOnly !== undefined ? { read_only: vars.readOnly } : {}),
          },
        },
        'Could not set the account',
      )
    },
    onSuccess: (data, vars) => {
      qc.setQueryData(reachCopiedKey(vars.agentName, vars.mcpName), data.reach_copied === true)
      return Promise.all([
        qc.invalidateQueries({
          queryKey: ['agent-service-account-options', vars.agentName, vars.mcpName],
        }),
        // The agent's picker lists what the binding covers.
        qc.invalidateQueries({ queryKey: eventCatalogKey(vars.mcpName) }),
      ])
    },
  })
}

interface ClearBindingVars {
  agentName: string
  mcpName: string
}

export const useClearAgentServiceBinding = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: ClearBindingVars) => {
      return apiJson(
        `/v1/agents/${encodeURIComponent(vars.agentName)}` +
          `/mcps/${encodeURIComponent(vars.mcpName)}/service-binding`,
        { method: 'DELETE' },
        'Could not remove the account',
      )
    },
    onSuccess: (_data, vars) => {
      qc.setQueryData(reachCopiedKey(vars.agentName, vars.mcpName), false)
      return Promise.all([
        qc.invalidateQueries({
          queryKey: ['agent-service-account-options', vars.agentName, vars.mcpName],
        }),
        qc.invalidateQueries({ queryKey: eventCatalogKey(vars.mcpName) }),
      ])
    },
  })
}
