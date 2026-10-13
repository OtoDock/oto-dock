// The workspace overlay lives in the URL (`?ws=1`, `?ws_preview=<path>`,
// hooks/useWorkspaceState.ts). A navigate that rebuilds the chat path (a new
// chat taking its id at warmup, a slug normalised at history time) must carry
// those two params or the open overlay closes under the user's hands.

const WORKSPACE_PARAMS = ['ws', 'ws_preview'] as const

/** `path` with the current location's workspace params appended; other
 *  params are dropped (the chat path owns the rest of its query). */
export function withWorkspaceParams(
  path: string,
  search: string = typeof window === 'undefined' ? '' : window.location.search,
): string {
  const current = new URLSearchParams(search)
  const kept = new URLSearchParams()
  for (const key of WORKSPACE_PARAMS) {
    const value = current.get(key)
    if (value) kept.set(key, value)
  }
  const qs = kept.toString()
  return qs ? `${path}?${qs}` : path
}
