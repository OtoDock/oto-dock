/**
 * Per-MCP service-account binding dropdown.
 *
 * Rendered next to each row in Agent Settings → MCPs tab. Lets the manager
 * pin the agent to one of their OWN connected accounts as the agent's service
 * identity for agent-scope sessions. Self-hides for MCPs that don't support
 * service accounts (the backing options endpoint returns 400).
 *
 * Options layout:
 *   • (no service account)            — clears the binding
 *   • Personal: <email>               — caller's own connected accounts
 *   • Personal (<owner>): <email>     — READ-ONLY when a co-manager bound theirs
 *   • + Connect new…                  — deep-link User Settings
 *
 * With a binding in place (and the agent's mode offering agent scope) the
 * agent's vendor event subscriptions follow beneath: the bound account is
 * the one they register with. On GitHub the agent's reach (the
 * repositories it may reach, read only) follows the select.
 */

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useSaveState } from '../hooks/useSaveState'
import {
  useAgentServiceAccountOptions,
  useClearAgentServiceBinding,
  useSetAgentServiceBinding,
} from '../api/serviceAccounts'
import { AgentServiceSubscriptions } from './accounts/AgentServiceSubscriptions'
import { GitHubReachEditor } from './accounts/GitHubReachEditor'
import { isGitHubApp } from '../lib/kinds/oauthApp'
import { Select } from './ui/Field'
import { SaveState } from './ui/SettingsControls'

interface Props {
  agentName: string
  mcpName: string
  /** Caller's own user_sub. Drives "is this binding mine?" detection so we
   * can render the co-manager read-only annotation correctly. */
  callerSub: string
  /** Whether the agent's visibility mode offers agent scope: a service-scope
   * subscription needs it, so the subscriptions section hides otherwise. */
  agentScopeAvailable?: boolean
}

/** Sentinel values for the <select>. ``encodeOption`` packs (label, owner_sub)
 * into a single string so the native dropdown stays simple. */
const SENTINEL_NONE = '__none__'
const SENTINEL_CONNECT_NEW = '__connect_new__'
const SEP = '::'
const ACCOUNT_PREFIX = 'bind'
const INSTALLATION_PREFIX = 'inst'

/** An account itself, or one of its GitHub App installations (the id first,
 * since a label may hold the separator). */
function encodeOption(label: string, ownerSub: string, installationId = ''): string {
  if (installationId) return `${INSTALLATION_PREFIX}${SEP}${installationId}${SEP}${ownerSub}${SEP}${label}`
  return `${ACCOUNT_PREFIX}${SEP}${ownerSub}${SEP}${label}`
}
function decodeOption(value: string): { label: string; ownerSub: string; installationId: string } | null {
  const parts = value.split(SEP)
  if (parts[0] === INSTALLATION_PREFIX && parts.length >= 4) {
    return { installationId: parts[1], ownerSub: parts[2], label: parts.slice(3).join(SEP) }
  }
  if (parts[0] === ACCOUNT_PREFIX && parts.length >= 3) {
    return { installationId: '', ownerSub: parts[1], label: parts.slice(2).join(SEP) }
  }
  return null
}

export function ServiceAccountBindingDropdown({
  agentName,
  mcpName,
  callerSub,
  agentScopeAvailable = false,
}: Props) {
  const navigate = useNavigate()
  const opts = useAgentServiceAccountOptions(agentName, mcpName)
  const setBinding = useSetAgentServiceBinding()
  const clearBinding = useClearAgentServiceBinding()
  // The server's word on the last change: a refusal, or a bind that stands
  // with a warning (the installation's first token was not minted).
  const [notice, setNotice] = useState<{ text: string; warning: boolean } | null>(null)
  const { stateOf, run } = useSaveState()

  // Self-hide for MCPs that don't support service accounts (backend 400) or
  // while loading initial data.
  if (opts.isLoading) return null
  if (opts.isError) return null
  if (!opts.data) return null

  const { my_accounts, current_binding, reach_supported } = opts.data

  // Compute the currently-selected <select> value.
  const selectedValue = current_binding
    ? encodeOption(current_binding.label, current_binding.owner_sub, current_binding.installation_id || '')
    : SENTINEL_NONE

  // A binding owned by a co-manager (a different user) renders read-only; the
  // manager can still switch it to one of their own accounts.
  const isForeignBinding =
    !!current_binding &&
    !!current_binding.owner_sub &&
    current_binding.owner_sub !== callerSub

  // The bound installation may have left the account's list since (a
  // Refresh after a removal, a suspension, a lost ownership): the binding
  // still stands, so it is shown as it is, unavailable, and "(no service
  // account)" is then a real change that clears it.
  const bound = current_binding?.installation_id
    ? my_accounts
        .find((a) => a.label === current_binding.label)
        ?.installations?.find((i) => String(i.id) === current_binding.installation_id)
    : undefined
  const boundUnavailable =
    !!current_binding?.installation_id && !isForeignBinding && (!bound || !bound.admin || bound.suspended)

  // A refusal shows under the select, where the answer's warning shows.
  const onChange = (value: string) => {
    if (value === SENTINEL_CONNECT_NEW) {
      navigate('/user-settings?tab=integrations')
      return
    }
    const decoded = value === SENTINEL_NONE ? null : decodeOption(value)
    if (value !== SENTINEL_NONE && !decoded) return
    setNotice(null)
    void run('account', () => {
      if (!decoded) return clearBinding.mutateAsync({ agentName, mcpName })
      return setBinding.mutateAsync({
        agentName,
        mcpName,
        accountLabel: decoded.label,
        // The account itself is an explicit '' (an older proxy ignores the key).
        installationId: decoded.installationId,
      }).then((answer) => {
        if (answer.warning) setNotice({ text: answer.warning, warning: true })
      })
    }, {
      onFail: (e) => setNotice({ text: e instanceof Error ? e.message : 'The change was refused', warning: false }),
    })
  }

  const count = (i: { repositories_count: number; repositories_truncated?: boolean }) =>
    i.repositories_truncated
      ? `${i.repositories_count}+ repositories`
      : `${i.repositories_count} ${i.repositories_count === 1 ? 'repository' : 'repositories'}`

  const isPending = setBinding.isPending || clearBinding.isPending

  return (
    // Clicks stop here, so the card that holds the select never takes them
    // as its own.
    <div onClick={(e) => e.stopPropagation()}>
    <div
      // Stack on mobile (label above a full-width select) so the select never
      // overflows the card / makes the page horizontally scrollable.
      className="flex flex-col gap-1 mt-2 sm:flex-row sm:items-center sm:gap-2"
    >
      <span className="text-3xs uppercase tracking-wide text-tertiary shrink-0">
        Service account
        <span className="normal-case tracking-normal"><SaveState state={stateOf('account')} /></span>
      </span>
      <Select compact
        value={selectedValue}
        disabled={isPending}
        onChange={(e) => onChange(e.target.value)}
        className="min-w-0 sm:w-auto max-w-full"
      >
        <option value={SENTINEL_NONE}>No service account</option>

        {my_accounts.map((a) => (
          <option
            key={`m:${a.label}`}
            value={encodeOption(a.label, callerSub)}
          >
            {/* Label first: two same-identity accounts (a labeled PAT next
                to the browser grant) are twins by display_email; the label
                is the distinguishing name and equals the identity when the
                user never set one. */}
            Personal: {a.label || a.display_email}
          </option>
        ))}

        {/* A GitHub App installation the person owns: the agent then acts
            with the installation's own token, which reaches its repositories
            only. */}
        {my_accounts.flatMap((a) =>
          (a.hosted ? [] : a.installations ?? [])
            .filter((i) => i.admin && !i.suspended)
            .map((i) => (
              <option
                key={`i:${a.label}:${i.id}`}
                value={encodeOption(a.label, callerSub, String(i.id))}
              >
                Installation: {i.account} (
                {i.repository_selection === 'all' ? 'all repositories' : count(i)}
                ) via {a.label || a.display_email}
              </option>
            )),
        )}

        {boundUnavailable && (
          <option value={selectedValue} disabled>
            Installation: {current_binding!.installation_account || current_binding!.installation_id} via{' '}
            {current_binding!.label}, unavailable
          </option>
        )}

        {/* A co-manager's binding shows as a disabled, read-only row so the
            manager can SEE who owns the agent's current service identity. */}
        {isForeignBinding && (
          <option value={selectedValue} disabled>
            {current_binding!.installation_id
              ? `Installation: ${current_binding!.installation_account || current_binding!.installation_id} via `
              : ''}
            Personal ({current_binding!.owner_name}):{' '}
            {current_binding!.label || current_binding!.owner_email}
          </option>
        )}

        <option value={SENTINEL_CONNECT_NEW}>+ Connect new…</option>
      </Select>
    </div>
    {notice && (
      <div className={`text-xs mt-1 ${notice.warning ? 'text-status-warning-fg' : 'text-status-danger-fg'}`}>
        {notice.text}
      </div>
    )}
    {current_binding?.problem && (
      <div className="text-xs mt-1 text-status-danger-fg">{current_binding.problem}</div>
    )}
    {current_binding && reach_supported && (
      <GitHubReachEditor
        key={`${current_binding.owner_sub}:${current_binding.label}:${current_binding.installation_id || ''}`}
        agentName={agentName}
        mcpName={mcpName}
        accountLabel={current_binding.label}
        installationId={current_binding.installation_id || ''}
        repositories={current_binding.repositories ?? []}
        readOnly={!!current_binding.read_only}
        pickable={
          isGitHubApp(my_accounts.find((a) => a.label === current_binding.label)?.kind) ||
          (current_binding.repositories ?? []).length > 0
        }
        viewOnly={isForeignBinding}
      />
    )}
    {current_binding && agentScopeAvailable && (
      <AgentServiceSubscriptions
        agentName={agentName}
        mcpName={mcpName}
        accountLabel={current_binding.label}
      />
    )}
    </div>
  )
}
