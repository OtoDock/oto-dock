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
import { apiFetch } from './auth'

export interface AgentServiceAccountOption {
  label: string
  display_email: string
  is_default: boolean
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
  } | null
}

export const useAgentServiceAccountOptions = (
  agentName: string | undefined,
  mcpName: string | undefined,
) =>
  useQuery({
    queryKey: ['agent-service-account-options', agentName, mcpName],
    queryFn: async (): Promise<AgentServiceAccountOptionsResponse> => {
      const res = await apiFetch(
        `/v1/agents/${encodeURIComponent(agentName!)}` +
          `/mcps/${encodeURIComponent(mcpName!)}/service-account-options`,
      )
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    enabled: !!agentName && !!mcpName,
  })

interface SetBindingVars {
  agentName: string
  mcpName: string
  accountLabel: string
}

export const useSetAgentServiceBinding = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (vars: SetBindingVars) => {
      const res = await apiFetch(
        `/v1/agents/${encodeURIComponent(vars.agentName)}` +
          `/mcps/${encodeURIComponent(vars.mcpName)}/service-binding`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            account_label: vars.accountLabel,
          }),
        },
      )
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({
        queryKey: ['agent-service-account-options', vars.agentName, vars.mcpName],
      })
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
      const res = await apiFetch(
        `/v1/agents/${encodeURIComponent(vars.agentName)}` +
          `/mcps/${encodeURIComponent(vars.mcpName)}/service-binding`,
        { method: 'DELETE' },
      )
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({
        queryKey: ['agent-service-account-options', vars.agentName, vars.mcpName],
      })
    },
  })
}
