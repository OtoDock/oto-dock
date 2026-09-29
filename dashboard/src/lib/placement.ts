/**
 * Placement — where a session runs — mirrored from the proxy's authority
 * `proxy/core/placement.py` (`tests/remote/test_placement.py` binds this
 * file to it; edit both).
 *
 * A stored execution target (`agents.execution_target`, a chat's pin, the
 * `warmup_ready` frame's `execution_target`, a check verdict's `ran_on`) is
 * `'local'` or a machine id. An app's step target says where in two words
 * (`SITE`; the proxy spells a check input document's site with the same
 * pair, which the dashboard never reads). A machine row was paired by an
 * admin as platform infrastructure or by a person as their own
 * (`PAIRING_SCOPE`, `remote_machines.pairing_scope`).
 */

/** The stored value of the platform sandbox. */
export const TARGET_LOCAL = 'local'

/** The sandbox: `'local'`, or an unstamped (empty) target. */
export function isLocalTarget(target: string | null | undefined): boolean {
  return !target || target === TARGET_LOCAL
}

/** The machine id a stored target names; `''` for the sandbox. */
export function machineOf(target: string | null | undefined): string {
  return isLocalTarget(target) ? '' : (target as string)
}

/** The site a check input document and an app's step target carry. */
export const SITE = { LOCAL: 'local', MACHINE: 'machine' } as const
export type SiteKind = (typeof SITE)[keyof typeof SITE]

/** How a machine was paired (`remote_machines.pairing_scope`). */
export const PAIRING_SCOPE = { ADMIN: 'admin', USER: 'user' } as const
export type PairingScope = (typeof PAIRING_SCOPE)[keyof typeof PAIRING_SCOPE]
