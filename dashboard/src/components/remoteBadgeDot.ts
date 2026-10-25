/**
 * The colour of a machine's state dot: RemoteBadge's, and the dot of each
 * place in the chat's "Runs on" menu (RunsOnChip). One table. The menu asks
 * it with no pairing scope, so a person's own offline computer is red there
 * and amber on the top bar's badge (which says the chat fell back to the
 * platform): the menu's dot says only whether the place is up.
 */
import { PAIRING_SCOPE, type PairingScope } from '../lib/placement'
import { MACHINE_STATE, type MachineState } from '../lib/status/machine'

// The machine's live state (lib/status/machine.ts) plus the badge's own
// word for a chat that fell back to the sandbox while its machine is away.
// Exported for the callers that derive a badge state (TopBar).
export const FELLBACK_LOCAL = 'fellback_local'
export type BadgeState = MachineState | typeof FELLBACK_LOCAL | null

// Which kind of target the dot represents. Drives severity colour: a user's
// own machine going offline is a soft fallback (amber), an admin/platform
// target going offline blocks everyone on the agent (red).
export type BadgeScope = PairingScope

export function dotColorClass(state: Exclude<BadgeState, null>, scope?: BadgeScope): string {
  if (state === MACHINE_STATE.ONLINE) return 'bg-status-success-solid'
  if (state === MACHINE_STATE.STALE || state === MACHINE_STATE.PAUSED || state === FELLBACK_LOCAL) return 'bg-status-warning-solid'
  // disconnected | never_connected
  if (scope === PAIRING_SCOPE.USER) return 'bg-status-warning-solid' // own machine → soft fallback
  if (state === MACHINE_STATE.NEVER_CONNECTED) return 'bg-status-info-solid'
  return 'bg-status-danger-solid' // admin/platform target offline → blocks everyone
}
