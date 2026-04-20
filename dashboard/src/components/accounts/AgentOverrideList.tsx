/**
 * Per-agent binding checklist — pin specific agents to a specific account
 * (overrides the ⭐ default). Used by both user-scope and service-scope
 * account cards.
 */

import { useCallback } from 'react'
import type { AccountSummary } from '../../api/credentials'
import type { AccountsOps } from './types'
import type { Integration } from '../../api/credentials'

interface Props {
  integration: Integration
  account: AccountSummary
  ops: AccountsOps
}

export function AgentOverrideList({ integration, account, ops }: Props) {
  // Build the set of agents currently bound to OTHER accounts so the UI
  // can grey them out + show the owner.
  const ownedByOtherAccount: Record<string, string> = {}
  for (const acc of integration.accounts) {
    if (acc.account_label === account.account_label) continue
    for (const agent of acc.agent_overrides) {
      ownedByOtherAccount[agent] = acc.display_email || acc.account_label
    }
  }

  const handleToggle = useCallback(
    (agent: string) => {
      if (!ops.setBinding || !ops.removeBinding) return
      if (account.agent_overrides.includes(agent)) {
        ops.removeBinding.mutate({
          mcpName: integration.mcp_name,
          agentName: agent,
        })
      } else {
        ops.setBinding.mutate({
          mcpName: integration.mcp_name,
          agentName: agent,
          accountLabel: account.account_label,
        })
      }
    },
    [account, integration.mcp_name, ops],
  )

  if (integration.candidate_agents.length === 0) {
    return null
  }
  // Service-side ops omit setBinding/removeBinding — bindings for service
  // accounts live in Agent Settings → MCPs (ServiceAccountBindingDropdown).
  if (!ops.setBinding || !ops.removeBinding) {
    return null
  }

  const defaultsHint = 'Agents not checked use the ⭐ default account.'

  return (
    <div className="space-y-1">
      <div className="text-xs text-p-text-secondary uppercase tracking-wide font-medium">
        Use this account for specific agents
      </div>
      <div className="text-xs text-p-text-light">
        {defaultsHint} Other accounts' bindings show greyed out.
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-1 pt-2">
        {integration.candidate_agents.map((agent) => {
          const bound = account.agent_overrides.includes(agent)
          const otherOwner = ownedByOtherAccount[agent]
          return (
            <label
              key={agent}
              className={`flex items-center gap-2 px-2 py-1 rounded-sm text-sm cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800 ${
                otherOwner ? 'opacity-60' : ''
              }`}
            >
              <input
                type="checkbox"
                checked={bound}
                onChange={() => handleToggle(agent)}
                className="rounded-sm border-p-border-light text-brand focus:ring-brand"
              />
              <span className="text-p-text">{agent}</span>
              {otherOwner && (
                <span className="text-xs text-p-text-light ml-auto">
                  → {otherOwner}
                </span>
              )}
            </label>
          )
        })}
      </div>
    </div>
  )
}
