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
 */

import { useNavigate } from 'react-router-dom'
import {
  useAgentServiceAccountOptions,
  useClearAgentServiceBinding,
  useSetAgentServiceBinding,
} from '../api/serviceAccounts'

interface Props {
  agentName: string
  mcpName: string
  /** Caller's own user_sub. Drives "is this binding mine?" detection so we
   * can render the co-manager read-only annotation correctly. */
  callerSub: string
}

/** Sentinel values for the <select>. ``encodeOption`` packs (label, owner_sub)
 * into a single string so the native dropdown stays simple. */
const SENTINEL_NONE = '__none__'
const SENTINEL_CONNECT_NEW = '__connect_new__'
const SEP = '::'

function encodeOption(label: string, ownerSub: string): string {
  return `bind${SEP}${ownerSub}${SEP}${label}`
}
function decodeOption(value: string): { label: string; ownerSub: string } | null {
  if (!value.startsWith(`bind${SEP}`)) return null
  const parts = value.split(SEP)
  if (parts.length < 3) return null
  const ownerSub = parts[1]
  const label = parts.slice(2).join(SEP)
  return { label, ownerSub }
}

export function ServiceAccountBindingDropdown({
  agentName,
  mcpName,
  callerSub,
}: Props) {
  const navigate = useNavigate()
  const opts = useAgentServiceAccountOptions(agentName, mcpName)
  const setBinding = useSetAgentServiceBinding()
  const clearBinding = useClearAgentServiceBinding()

  // Self-hide for MCPs that don't support service accounts (backend 400) or
  // while loading initial data.
  if (opts.isLoading) return null
  if (opts.isError) return null
  if (!opts.data) return null

  const { my_accounts, current_binding } = opts.data

  // Compute the currently-selected <select> value.
  const selectedValue = current_binding
    ? encodeOption(current_binding.label, current_binding.owner_sub)
    : SENTINEL_NONE

  // A binding owned by a co-manager (a different user) renders read-only; the
  // manager can still switch it to one of their own accounts.
  const isForeignBinding =
    !!current_binding &&
    !!current_binding.owner_sub &&
    current_binding.owner_sub !== callerSub

  const onChange = async (value: string) => {
    if (value === SENTINEL_CONNECT_NEW) {
      navigate('/user-settings?tab=integrations')
      return
    }
    if (value === SENTINEL_NONE) {
      await clearBinding.mutateAsync({ agentName, mcpName })
      return
    }
    const decoded = decodeOption(value)
    if (!decoded) return
    await setBinding.mutateAsync({
      agentName,
      mcpName,
      accountLabel: decoded.label,
    })
  }

  const isPending = setBinding.isPending || clearBinding.isPending

  return (
    <div
      // Stack on mobile (label above a full-width select) so the select never
      // overflows the card / makes the page horizontally scrollable.
      className="flex flex-col gap-1 mt-2 sm:flex-row sm:items-center sm:gap-2"
      // Don't let clicks on the dropdown toggle the MCP enable checkbox in
      // the wrapping <label>.
      onClick={(e) => e.stopPropagation()}
    >
      <span className="text-[10px] uppercase tracking-wide text-p-text-light shrink-0">
        Service account
      </span>
      <select
        value={selectedValue}
        disabled={isPending}
        onChange={(e) => void onChange(e.target.value)}
        className="text-xs px-2 py-1 rounded-sm border border-p-border-light bg-white dark:bg-gray-800 w-full min-w-0 sm:w-auto max-w-full"
      >
        <option value={SENTINEL_NONE}>(no service account)</option>

        {my_accounts.map((a) => (
          <option
            key={`m:${a.label}`}
            value={encodeOption(a.label, callerSub)}
          >
            Personal: {a.display_email || a.label}
          </option>
        ))}

        {/* A co-manager's binding shows as a disabled, read-only row so the
            manager can SEE who owns the agent's current service identity. */}
        {isForeignBinding && (
          <option value={selectedValue} disabled>
            Personal ({current_binding!.owner_name}):{' '}
            {current_binding!.owner_email || current_binding!.label}
          </option>
        )}

        <option value={SENTINEL_CONNECT_NEW}>+ Connect new…</option>
      </select>
    </div>
  )
}
