/**
 * Per-agent checklist: the agents that may use this account. An agent may
 * use several of the person's accounts of one tool (its set), and starts on
 * the set's default. An agent ticked on no account uses the ⭐ default.
 * Used by both user-scope and service-scope account cards.
 *
 * A ticked agent shows "Default" when this account is the one it starts on,
 * else a "Make default" button. The first account ticked for an agent is its
 * default, and when the default is unticked the server promotes the oldest
 * one left (the refetch shows it).
 *
 * On GitHub (`reach_supported`) each agent this account may take also
 * shows the repositories it may reach with it and Read only: the person's
 * own narrowing of this account for that agent. Choosing them for an
 * unchecked agent checks it (the add route carries the reach), and
 * unchecking clears them. An agent on its own accounts gets no checkbox on any
 * tool: its chats use the agent's own accounts (`agents_on_own_accounts`), and
 * the row names the one they start on (`agent_accounts`). A row ticked
 * before still shows, so it can be cleared.
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

/** What the row of an agent whose chats run on its own accounts says. */
function ownAccountsLine(account: string | undefined, belowFloor: boolean): string {
  if (belowFloor) {
    return "Your chats with it get no account for this tool. Your role is below its team account's starting role."
  }
  return account
    ? `Its chats use the agent's own account, ${account}. A manager sets it in Agent Settings, MCPs.`
    : "Its chats use the agent's own account. A manager sets it in Agent Settings, MCPs."
}

export function AgentOverrideList({ integration, account, ops }: Props) {
  const { stateOf, run } = useSaveState()
  const handleToggle = useCallback(
    (agent: string) => {
      const { addAgentAccount, removeAgentAccount } = ops
      if (!addAgentAccount || !removeAgentAccount) return
      const vars = { mcpName: integration.mcp_name, agentName: agent, accountLabel: account.account_label }
      if (account.agent_overrides.includes(agent)) {
        void run(`agents:${agent}`, () => removeAgentAccount.mutateAsync(vars),
          { failTitle: `This account was not removed from ${agent}` })
      } else {
        void run(`agents:${agent}`, () => addAgentAccount.mutateAsync(vars),
          { failTitle: `This account was not added to ${agent}` })
      }
    },
    [account, integration.mcp_name, ops, run],
  )
  const handleMakeDefault = useCallback(
    (agent: string) => {
      const { setAgentDefault } = ops
      if (!setAgentDefault) return
      void run(`agents:${agent}:default`, () => setAgentDefault.mutateAsync({
        mcpName: integration.mcp_name,
        agentName: agent,
        accountLabel: account.account_label,
      }), { failTitle: `This account was not made the default for ${agent}` })
    },
    [account.account_label, integration.mcp_name, ops, run],
  )

  if (integration.candidate_agents.length === 0) {
    return null
  }
  // Service-side ops omit the agent account hooks: the agent's own accounts
  // live in Agent Settings → MCPs (AgentAccounts).
  const { addAgentAccount, removeAgentAccount, setAgentDefault } = ops
  if (!addAgentAccount || !removeAgentAccount || !setAgentDefault) {
    return null
  }

  const narrowing = !!integration.reach_supported
  const ownAccounts = new Set(integration.agents_on_own_accounts ?? [])
  const belowFloor = new Set(integration.agents_below_floor ?? [])
  const defaults = new Set(account.agent_defaults ?? [])

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
        Agents that may use this account. An agent starts on its default account. An agent with no
        account ticked uses the ⭐ default account.
      </div>
      <div className={`grid grid-cols-1 gap-1 pt-2 ${narrowing ? '' : 'sm:grid-cols-2'}`}>
        {integration.candidate_agents.map((agent) => {
          const bound = account.agent_overrides.includes(agent)
          // An agent whose chats run on its own accounts (Shared only, the
          // team account on): a choice here is never read, so the card
          // offers no checkbox (one ticked before still shows, so it can be
          // cleared) and no default.
          const sharedHere = ownAccounts.has(agent)
          const line = ownAccountsLine(integration.agent_accounts?.[agent], belowFloor.has(agent))
          if (sharedHere && !bound) {
            return (
              <div key={agent} className="px-2 py-1 text-sm">
                <span className="text-primary">{agent}</span>
                <div className="text-xs text-tertiary">{line}</div>
              </div>
            )
          }
          return (
            <div key={agent} data-testid="agent-account-row">
              <div className="flex items-center gap-2 px-2 py-1 rounded-control text-sm hover:bg-surface-hover">
                <label className="flex min-w-0 flex-1 items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={bound}
                    onChange={() => handleToggle(agent)}
                    className="accent-action"
                  />
                  <span className="text-primary truncate">{agent}</span>
                </label>
                {bound && !sharedHere && (defaults.has(agent) ? (
                  <span className="text-xs text-tertiary">Default</span>
                ) : (
                  <button
                    type="button"
                    onClick={() => handleMakeDefault(agent)}
                    disabled={setAgentDefault.isPending}
                    aria-label={`Make default for ${agent}`}
                    className="text-xs text-link hover:text-link-hover disabled:opacity-50"
                  >
                    Make default
                  </button>
                ))}
              </div>
              {sharedHere && (
                <div className="pl-7 pb-1 text-xs text-tertiary">{line}</div>
              )}
              {narrowing && !sharedHere && (
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
  const addAgentAccount = ops.addAgentAccount!

  // The view shows a refusal under the controls. The add route keeps the
  // account in the agent's set and carries this account's reach for it.
  const save = (change: ReachChange) =>
    addAgentAccount.mutateAsync({
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
        pending={addAgentAccount.isPending}
        label={agent}
      />
    </div>
  )
}
