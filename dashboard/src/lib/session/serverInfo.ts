// What the server reports about itself on the dashboard socket
// (`server_info`, read by useDashboardWs). The platform version
// (`server_info.version`): the OtoDock tab's "This install" card falls back
// to it when the REST answer carries none (a dev tree without VERSIONS.md).
// The update flag (`server_info.updating`): the platform holds new work
// before it stops for an update, and the chat page says so above the
// composer.

import { useSyncExternalStore } from 'react'

let version = ''

export function noteServerVersion(value: unknown): void {
  if (typeof value === 'string') version = value
}

export function serverVersion(): string {
  return version
}

let updating = false
const listeners = new Set<() => void>()

/** The connect frame carries `updating` only while the platform holds, so a
 *  frame without the key clears the flag (a socket that reconnects after
 *  the update gets one). */
export function noteServerUpdating(value: unknown): void {
  const next = value === true
  if (next === updating) return
  updating = next
  listeners.forEach((l) => l())
}

export function serverUpdating(): boolean {
  return updating
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => { listeners.delete(onChange) }
}

export function useServerUpdating(): boolean {
  return useSyncExternalStore(subscribe, serverUpdating, () => false)
}
