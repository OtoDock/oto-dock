/**
 * An agent's own accounts of one tool, on Agent Settings → MCPs, in every
 * mode with a space of the agent's (a Personal-only agent has none, and the
 * page renders no list there): each account with whose it is, the default
 * (the account runs with no person use), Make default and Remove, and Add an
 * account from the manager's own (on GitHub the account itself or one of its
 * installations). Under each account, on GitHub, the agent's reach on it
 * (changed by the person who added it), and its own event subscriptions. On
 * an agent that runs every chat in its own space
 * (Shared-only), editors and managers can switch a chat to any of these (the
 * agent's switch_account tool); elsewhere people's chats use their own
 * accounts.
 *
 * Self-hides for an MCP that takes no account of its own (the options route
 * answers 400) and while it loads. Any manager may change the default or
 * remove an account, whoever added it; adding names the caller's own
 * account only, one that shares a name with another person's included: the
 * server's refusal says what to do. An account whose owner left cannot be
 * the default. A refusal shows under the list in the server's words.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  type AgentServiceAccountOption,
  type AgentServiceSetAccount,
  type ServiceSetResponse,
  useAddAgentServiceAccount,
  useAgentServiceAccountOptions,
  useRemoveAgentServiceAccount,
  useSetAgentServiceDefault,
} from '../../api/serviceAccounts'
import { useSubscriptions } from '../../api/subscriptions'
import { useSaveState } from '../../hooks/useSaveState'
import { isGitHubApp } from '../../lib/kinds/oauthApp'
import InfoTip from '../ui/InfoTip'
import { Select } from '../ui/Field'
import { SaveState } from '../ui/SettingsControls'
import { useDialogs } from '../ui/dialogs'
import { AgentLeftoverSubscriptions, AgentServiceSubscriptions } from './AgentServiceSubscriptions'
import { GitHubReachEditor } from './GitHubReachEditor'

interface Props {
  agentName: string
  mcpName: string
  /** The agent runs every person's chat in its own space (Shared-only): its
   * chats use these accounts and may switch among them. */
  chatsUseThem: boolean
}

const ADD_NONE = ''
const ADD_CONNECT = '__connect__'

interface AddChoice {
  label: string
  installationId: string
  text: string
}

const nameOf = (a: { label: string; display_email: string }) => a.display_email || a.label

/** What the Add select offers: each of the caller's accounts the agent does
 * not hold, and on a GitHub App account each installation the caller owns. */
function addChoices(myAccounts: AgentServiceAccountOption[], held: Set<string>): AddChoice[] {
  return myAccounts
    .filter((a) => !held.has(a.label))
    .flatMap((a) => [
      { label: a.label, installationId: '', text: a.label || a.display_email },
      ...(a.hosted ? [] : a.installations ?? [])
        .filter((i) => i.admin && !i.suspended)
        .map((i) => ({
          label: a.label,
          installationId: String(i.id),
          text: `Installation: ${i.account} via ${a.label || a.display_email}`,
        })),
    ])
}

/** What removing an account does: the account its chats, or on an agent
 * whose chats are people's own its runs with no person, move to (the server
 * promotes the oldest whose owner stands, and `accounts` lists the default
 * first, then oldest first), or that they then have none. */
function removeBody(account: AgentServiceSetAccount, accounts: AgentServiceSetAccount[],
                    subscriptions: number, chatsUseThem: boolean): string {
  const next = account.is_default
    ? accounts.find((a) => a.label !== account.label && a.stands)
    : accounts.find((a) => a.is_default && a.stands)
  const subs = subscriptions > 0 ? ' Its event subscriptions are removed.' : ''
  if (!chatsUseThem) {
    if (!next) return `Runs with no person then have no account for this tool.${subs}`
    return `Runs with no person move to ${nameOf(next)}.${subs}`
  }
  if (!next) return `Chats on it stop what they are doing. This agent then has no account for this tool.${subs}`
  return `Chats on it stop what they are doing and move to ${nameOf(next)}. Each chat is told.${subs}`
}

export function AgentAccounts({ agentName, mcpName, chatsUseThem }: Props) {
  const navigate = useNavigate()
  const dialogs = useDialogs()
  const opts = useAgentServiceAccountOptions(agentName, mcpName)
  const subscriptions = useSubscriptions(
    { mcp_name: mcpName, scope: 'service', agent: agentName })
  const add = useAddAgentServiceAccount()
  const makeDefault = useSetAgentServiceDefault()
  const remove = useRemoveAgentServiceAccount()
  const { stateOf, run } = useSaveState()
  const [notice, setNotice] = useState<{ text: string; refused: boolean } | null>(null)

  if (opts.isLoading || opts.isError || !opts.data) return null

  const accounts = opts.data.accounts ?? []
  const held = new Set(accounts.filter((a) => a.mine).map((a) => a.label))
  const choices = addChoices(opts.data.my_accounts, held)
  const busy = add.isPending || makeDefault.isPending || remove.isPending
  const subscriptionsOf = (label: string) =>
    (subscriptions.data ?? []).filter((s) => s.account_label === label).length

  const write = (go: () => Promise<ServiceSetResponse>) => {
    setNotice(null)
    void run('accounts', () => go().then((answer) => {
      if (answer.warning) setNotice({ text: answer.warning, refused: false })
    }), {
      onFail: (e) => setNotice({ text: e instanceof Error ? e.message : 'The change was refused.', refused: true }),
    })
  }

  const onAdd = (value: string) => {
    if (value === ADD_CONNECT) {
      navigate('/user-settings?tab=integrations')
      return
    }
    const choice = choices[Number(value)]
    if (!value || !choice) return
    write(() => add.mutateAsync({
      agentName, mcpName, accountLabel: choice.label, installationId: choice.installationId,
    }))
  }

  const onRemove = async (account: AgentServiceSetAccount) => {
    if (!(await dialogs.confirm({
      title: `Remove ${nameOf(account)} from this agent?`,
      body: removeBody(account, accounts, subscriptionsOf(account.label), chatsUseThem),
      confirmLabel: 'Remove',
      tone: 'danger',
    }))) {
      return
    }
    write(() => remove.mutateAsync({ agentName, mcpName, accountLabel: account.label }))
  }

  return (
    // Clicks stop here, so the card that holds the list never takes them as its own.
    <div className="mt-2" data-testid="agent-accounts" onClick={(e) => e.stopPropagation()}>
      <div className="text-3xs uppercase tracking-wide text-tertiary">
        Accounts
        <span className="normal-case tracking-normal">
          <InfoTip label="Accounts">
            Tasks, triggers, calls and delegated work use the default. Each account has its own
            event subscriptions. Removing an account removes them. An account stays while the
            person who added it manages this agent.
            {chatsUseThem && (
              <>
                {' '}A switch is for everyone in that chat. When the default changes, the chats on
                it are told. They move at their next start. Every editor of this agent sees these
                addresses.
              </>
            )}
          </InfoTip>
          <SaveState state={stateOf('accounts')} />
        </span>
      </div>
      <div className="text-xs text-tertiary">
        {chatsUseThem
          ? 'Chats start on the default. Editors and managers can switch a chat to any of these.'
          : "Runs with no person use the default. People's own chats use their own accounts."}
      </div>
      <ul className="mt-1 space-y-2">
        {accounts.map((a) => (
          <li key={a.label} data-testid="service-account-row">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
              <span className="min-w-0 truncate text-primary">{nameOf(a)}</span>
              {a.installation_id && (
                <span className="text-xs text-secondary">
                  Installation: {a.installation_account || a.installation_id}
                </span>
              )}
              <span className="text-xs text-tertiary">{a.mine ? 'Yours' : a.owner_name}</span>
              {!a.stands && (
                <span className="text-xs text-status-warning-fg">Not in use. Its owner no longer manages this agent.</span>
              )}
              <span className="ml-auto flex items-center gap-3">
                {a.is_default ? (
                  <span className="text-xs text-tertiary">Default</span>
                ) : a.stands && (
                  <button
                    type="button"
                    onClick={() => write(() => makeDefault.mutateAsync({ agentName, mcpName, accountLabel: a.label }))}
                    disabled={busy}
                    aria-label={`Make ${nameOf(a)} the default`}
                    className="text-xs text-link hover:text-link-hover disabled:opacity-50"
                  >
                    Make default
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void onRemove(a)}
                  disabled={busy}
                  aria-label={`Remove ${nameOf(a)}`}
                  className="text-xs text-link hover:text-link-hover disabled:opacity-50"
                >
                  Remove
                </button>
              </span>
            </div>
            {a.problem && <div className="text-xs mt-1 text-status-danger-fg">{a.problem}</div>}
            {opts.data.reach_supported && (
              <GitHubReachEditor
                key={`${a.owner_sub}:${a.label}:${a.installation_id}`}
                agentName={agentName}
                mcpName={mcpName}
                accountLabel={a.label}
                installationId={a.installation_id}
                repositories={a.repositories ?? []}
                readOnly={!!a.read_only}
                pickable={isGitHubApp(a.kind) || (a.repositories ?? []).length > 0}
                viewOnly={!a.mine}
                accountName={nameOf(a)}
                onRefused={(text) => setNotice(text === null ? null : { text, refused: true })}
              />
            )}
            <AgentServiceSubscriptions
              agentName={agentName}
              mcpName={mcpName}
              accountLabel={a.label}
              paused={!a.stands}
              isDefault={a.is_default}
            />
          </li>
        ))}
      </ul>
      <AgentLeftoverSubscriptions
        agentName={agentName}
        mcpName={mcpName}
        heldLabels={accounts.map((a) => a.label)}
      />
      <div className="mt-2 flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
        <Select
          compact
          value={ADD_NONE}
          disabled={busy}
          onChange={(e) => onAdd(e.target.value)}
          aria-label="Add an account"
          className="min-w-0 sm:w-auto max-w-full"
        >
          <option value={ADD_NONE}>Add an account</option>
          {choices.map((c, i) => (
            <option key={`${c.label}:${c.installationId}`} value={String(i)}>{c.text}</option>
          ))}
          <option value={ADD_CONNECT}>Connect another account</option>
        </Select>
      </div>
      {notice && (
        <div className={`text-xs mt-1 ${notice.refused ? 'text-status-danger-fg' : 'text-status-warning-fg'}`}>
          {notice.text}
        </div>
      )}
    </div>
  )
}
