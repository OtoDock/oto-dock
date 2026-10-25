/**
 * A remote machine's live state — mirrored from the proxy's authority
 * `proxy/services/remote/remote_status.py` (`tests/storage/test_status_vocabularies.py`
 * binds this file to it; edit both). Every machine route overlays this
 * state on the row; the persisted column's `offline` never arrives.
 */

export const MACHINE_STATE = {
  ONLINE: 'online',
  STALE: 'stale',
  PAUSED: 'paused',
  DISCONNECTED: 'disconnected',
  NEVER_CONNECTED: 'never_connected',
} as const
export type MachineState = (typeof MACHINE_STATE)[keyof typeof MACHINE_STATE]

/** The machine can accept commands right now. */
export function isReachableMachine(state: string | null | undefined): boolean {
  return state === MACHINE_STATE.ONLINE || state === MACHINE_STATE.STALE
}

const STATE_WORDS: Record<MachineState, string> = {
  [MACHINE_STATE.ONLINE]: 'online',
  [MACHINE_STATE.STALE]: 'slow connection',
  [MACHINE_STATE.PAUSED]: 'paused',
  [MACHINE_STATE.DISCONNECTED]: 'offline',
  [MACHINE_STATE.NEVER_CONNECTED]: 'never connected',
}

/** A machine's state in a person's words ("never connected", never the
 *  stored word): the Execution Target select and the badge beside it. A
 *  word this table does not know reads as offline. */
export function machineStateWord(state: string | null | undefined): string {
  return STATE_WORDS[state as MachineState] ?? STATE_WORDS[MACHINE_STATE.DISCONNECTED]
}
