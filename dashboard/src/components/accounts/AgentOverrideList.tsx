/**
 * Per-agent binding checklist — pin specific agents to a specific account
 * (overrides the ⭐ default). Used by both user-scope and service-scope
 * account cards.
 *
 * On GitHub (`reach_supported`) each agent this account may take also
 * shows the repositories it may reach with it and Read only: the person's
 * own narrowing of their connection for that agent. Choosing them for an
 * unchecked agent checks it (the choice of account is where they live), and
 * unchecking clears them. A Shared-only agent gets none: its chats use the
 * agent's own GitHub account (`shared_only_agents`).
 */

import { useCallback, useState } from 'react'
import { useMyAccountRepositories } from '../../api/credentials'
import type { AccountSummary } from '../../api/credentials'
import type { AccountsOps } from './types'
import type { Integration } from '../../api/credentials'
import { useSaveState } from '../../hooks/useSaveState'
import { isGitHubApp } from '../../lib/kinds/oauthApp'
import { GitHubReachView, type ReachChange } from './GitHubReachEditor'
import InfoTip from '../ui/InfoTip'
import { SaveState } from '../ui/SettingsControls'

interface Props {
  integration: Integration
  account: AccountSummary
  ops: AccountsOps
}

const SHARED_ONLY_LINE =
  "Its chats use the agent's own GitHub account. Limit it under Agent Settings, MCPs."

export function AgentOverrideList({ integration, account, ops }: Props) {
  // Build the set of agents currently bound to OTHER accounts so the UI
  // can grey them out + show the owner.
  const ownedByOtherAccount: Record<string, string> = {}
  for (const acc of integration.accounts) {
    if (acc.account_label === account.account_label) continue
    for (const agent of acc.agent_overrides) {
      // Label first — same-identity accounts are twins by display_email.
      ownedByOtherAccount[agent] = acc.account_label || acc.display_email
    }
  }

  const { stateOf, run } = useSaveState()
  const handleToggle = useCallback(
    (agent: string) => {
      const { setBinding, removeBinding } = ops
      if (!setBinding || !removeBinding) return
      if (account.agent_overrides.includes(agent)) {
        void run(`agents:${agent}`, () => removeBinding.mutateAsync({
          mcpName: integration.mcp_name,
          agentName: agent,
        }), { failTitle: `The account choice for ${agent} was not removed` })
      } else {
        void run(`agents:${agent}`, () => setBinding.mutateAsync({
          mcpName: integration.mcp_name,
          agentName: agent,
          accountLabel: account.account_label,
        }), { failTitle: `The account choice for ${agent} was not saved` })
      }
    },
    [account, integration.mcp_name, ops, run],
  )

  if (integration.candidate_agents.length === 0) {
    return null
  }
  // Service-side ops omit setBinding/removeBinding — bindings for service
  // accounts live in Agent Settings → MCPs (ServiceAccountBindingDropdown).
  if (!ops.setBinding || !ops.removeBinding) {
    return null
  }

  const narrowing = !!integration.reach_supported
  const sharedOnly = new Set(integration.shared_only_agents ?? [])
  const defaultsHint = 'Agents not checked use the ⭐ default account.'

  return (
    <div className="space-y-1">
      <div className="text-xs text-secondary uppercase tracking-wide font-medium">
        Use this account for specific agents
        {narrowing && (
          <span className="normal-case tracking-normal">
            <InfoTip label="Use this account for specific agents">
              You can limit the repositories an agent reaches, or make it read only.
            </InfoTip>
          </span>
        )}
        <span className="normal-case tracking-normal"><SaveState state={stateOf('agents')} /></span>
      </div>
      <div className="text-xs text-tertiary">
        {defaultsHint}
      </div>
      <div className={`grid grid-cols-1 gap-1 pt-2 ${narrowing ? '' : 'sm:grid-cols-2'}`}>
        {integration.candidate_agents.map((agent) => {
          const bound = account.agent_overrides.includes(agent)
          const otherOwner = ownedByOtherAccount[agent]
          // A Shared-only agent's chats run as the agent, on its service
          // account: a choice here is never read, so a GitHub card offers no
          // checkbox (one ticked before still shows, so it can be cleared).
          if (narrowing && sharedOnly.has(agent) && !bound) {
            return (
              <div key={agent} className="px-2 py-1 text-sm">
                <span className="text-primary">{agent}</span>
                <div className="text-xs text-tertiary">{SHARED_ONLY_LINE}</div>
              </div>
            )
          }
          return (
            <div key={agent}>
              <label
                className={`flex items-center gap-2 px-2 py-1 rounded-control text-sm cursor-pointer hover:bg-surface-hover ${
                  otherOwner ? 'opacity-60' : ''
                }`}
              >
                <input
                  type="checkbox"
                  checked={bound}
                  onChange={() => handleToggle(agent)}
                  className="accent-action"
                />
                <span className="text-primary">{agent}</span>
                {otherOwner && (
                  <span className="text-xs text-tertiary ml-auto">
                    uses {otherOwner}
                  </span>
                )}
              </label>
              {narrowing && !otherOwner && sharedOnly.has(agent) && (
                <div className="pl-7 pb-1 text-xs text-tertiary">{SHARED_ONLY_LINE}</div>
              )}
              {narrowing && !otherOwner && !sharedOnly.has(agent) && (
                <OwnAgentReach
                  key={`${agent}:${bound}`}
                  integration={integration}
                  account={account}
                  agent={agent}
                  ops={ops}
                />
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** The person's own reach for one agent with this account. */
function OwnAgentReach({
  integration,
  account,
  agent,
  ops,
}: {
  integration: Integration
  account: AccountSummary
  agent: string
  ops: AccountsOps
}) {
  const [picking, setPicking] = useState(false)
  const listing = useMyAccountRepositories(integration.mcp_name, account.account_label, picking)
  const reach = account.agent_reaches?.find((r) => r.agent === agent)
  const repositories = reach?.repositories ?? []
  const setBinding = ops.setBinding!

  // The view shows a refusal under the controls.
  const save = (change: ReachChange) =>
    setBinding.mutateAsync({
      mcpName: integration.mcp_name,
      agentName: agent,
      accountLabel: account.account_label,
      ...change,
    }).then(() => ({}))

  return (
    <div className="pl-7 pb-1">
      <GitHubReachView
        repositories={repositories}
        readOnly={!!reach?.read_only}
        pickable={isGitHubApp(account.kind) || repositories.length > 0}
        viewOnly={false}
        listing={listing}
        onPickerChange={setPicking}
        save={save}
        pending={setBinding.isPending}
        label={agent}
      />
    </div>
  )
}
