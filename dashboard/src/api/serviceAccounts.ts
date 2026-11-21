/**
 * An agent's own accounts of a tool, the hooks of ``AgentAccounts`` on each
 * MCP row in Agent Settings, MCPs. Each account is one of a manager's own
 * connected accounts, added by them (the owner is always the caller, derived
 * server-side); runs with no person use the default. There is no platform
 * service-account tier.
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
  /** Each account may carry a GitHub reach (the MCP is GitHub). */
  reach_supported?: boolean
  /** The agent's own accounts of the tool, the default first. */
  accounts?: AgentServiceSetAccount[]
}

/** One of the agent's own accounts of a tool. */
export interface AgentServiceSetAccount {
  label: string
  display_email: string
  owner_sub: string
  owner_name: string
  is_default: boolean
  /** Its owner still manages the agent: the account is in use. */
  stands: boolean
  /** The caller's own account. */
  mine: boolean
  /** A GitHub App installation the account acts as ('' = the account itself). */
  installation_id: string
  installation_account?: string
  /** A GitHub-App account (lib/kinds/oauthApp.ts): it has repositories to pick from. */
  kind?: string
  /** The agent's GitHub reach on this account: the repositories it may reach
   * ([] = every one) and whether it may only read. */
  repositories?: string[]
  read_only?: boolean
  /** Why the account cannot serve as it is, in the server's words. */
  problem?: string
}

/** The answer of a write to the agent's own accounts. */
export interface ServiceSetResponse {
  status: string
  accounts: AgentServiceSetAccount[]
  /** A new GitHub account started from the reach the caller chose for their
   * own chats with the agent (a copy, once). */
  reach_copied?: boolean
  /** The installation's first token could not be minted (the account stands). */
  warning?: string
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

/** Whether an account of the agent started from the adder's own reach: set
 * by the add's answer, read by that account's reach editor's notice. */
const reachCopiedKey = (agentName: string, mcpName: string, accountLabel: string) =>
  ['agent-service-reach-copied', agentName, mcpName, accountLabel]

export const useReachCopied = (agentName: string, mcpName: string, accountLabel: string) =>
  useQuery({
    queryKey: reachCopiedKey(agentName, mcpName, accountLabel),
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

interface ServiceSetVars {
  agentName: string
  mcpName: string
  accountLabel: string
}

interface AddServiceVars extends ServiceSetVars {
  /** One of the account's GitHub App installations as the way it acts; '' or
   * absent the account itself. */
  installationId?: string
}

interface ServiceReachVars extends ServiceSetVars {
  /** The way the account acts, as the page shows it: the reach is written
   * only while it still acts so. */
  installationId: string
  /** Absent keeps what the account holds. */
  repositories?: string[]
  readOnly?: boolean
}

const serviceSetPath = (vars: ServiceSetVars) =>
  `/v1/agents/${encodeURIComponent(vars.agentName)}` +
  `/mcps/${encodeURIComponent(vars.mcpName)}/service-accounts`

function useServiceSetWrite<V extends ServiceSetVars>(
  request: (vars: V) => Promise<ServiceSetResponse>,
) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: request,
    onSuccess: (_data, vars: V) => Promise.all([
      qc.invalidateQueries({
        queryKey: ['agent-service-account-options', vars.agentName, vars.mcpName],
      }),
      qc.invalidateQueries({ queryKey: eventCatalogKey(vars.mcpName) }),
      // A removal takes the account's event subscriptions with it.
      qc.invalidateQueries({ queryKey: ['subscriptions'] }),
    ]),
    // A refusal often means the list changed under the page (an account
    // removed or changed meanwhile): show the list as it is now.
    onError: (_e, vars: V) =>
      qc.invalidateQueries({ queryKey: ['agent-service-account-options', vars.agentName, vars.mcpName] }),
  })
}

/** Add one of the caller's own accounts to the agent's own set. */
export const useAddAgentServiceAccount = () => {
  const qc = useQueryClient()
  return useServiceSetWrite((vars: AddServiceVars) => apiJson<ServiceSetResponse>(
    serviceSetPath(vars),
    {
      method: 'PUT',
      json: {
        account_label: vars.accountLabel,
        ...(vars.installationId ? { installation_id: vars.installationId } : {}),
      },
    },
    'Could not add the account',
  ).then((answer) => {
    qc.setQueryData(reachCopiedKey(vars.agentName, vars.mcpName, vars.accountLabel), answer.reach_copied === true)
    return answer
  }))
}

/** Change the agent's GitHub reach on one of the caller's own accounts. */
export const useSetAgentServiceReach = () => {
  const qc = useQueryClient()
  return useServiceSetWrite((vars: ServiceReachVars) => apiJson<ServiceSetResponse>(
    `${serviceSetPath(vars)}/reach`,
    {
      method: 'PUT',
      json: {
        account_label: vars.accountLabel,
        installation_id: vars.installationId,
        ...(vars.repositories !== undefined ? { repositories: vars.repositories } : {}),
        ...(vars.readOnly !== undefined ? { read_only: vars.readOnly } : {}),
      },
    },
    'Could not change the repositories',
  ).then((answer) => {
    // The reach is the manager's own now: the copy's notice goes.
    qc.setQueryData(reachCopiedKey(vars.agentName, vars.mcpName, vars.accountLabel), false)
    return answer
  }))
}

/** Make one of the agent's own accounts its default. */
export const useSetAgentServiceDefault = () =>
  useServiceSetWrite((vars: ServiceSetVars) => apiJson<ServiceSetResponse>(
    `${serviceSetPath(vars)}/default`,
    { method: 'PUT', json: { account_label: vars.accountLabel } },
    'Could not change the default account',
  ))

/** Take one account out of the agent's own set. */
export const useRemoveAgentServiceAccount = () => {
  const qc = useQueryClient()
  return useServiceSetWrite((vars: ServiceSetVars) => apiJson<ServiceSetResponse>(
    `${serviceSetPath(vars)}?${new URLSearchParams({ account_label: vars.accountLabel }).toString()}`,
    { method: 'DELETE' },
    'Could not remove the account',
  ).then((answer) => {
    qc.setQueryData(reachCopiedKey(vars.agentName, vars.mcpName, vars.accountLabel), false)
    return answer
  }))
}
