/**
 * Path helpers shared by the workspace and chat surfaces.
 *
 * Two representations exist for agent files:
 * - Agent-relative paths (returned by the file API): `workspace/foo.md`,
 *   `users/alice/workspace/foo.md`. Used in REST URLs.
 * - Sandbox-virtual paths (consumed by agent prompts): `/workspace/foo.md`,
 *   `/users/alice/workspace/foo.md`. What CLI/Codex/Direct LLM agents see
 *   inside their sandbox. We inject these into the chat textarea when the
 *   user drags a tile or hits Copy Path.
 *
 * Backend (proxy/auth/hooks) is the source of truth for the mapping; this
 * client-side helper exists purely so the chat sees what the agent will see.
 */

/**
 * Convert an agent-relative path (no leading slash) to its sandbox-virtual
 * form (leading slash). Idempotent on already-virtual paths.
 */
export function toSandboxVirtualPath(agentRelPath: string): string {
  if (!agentRelPath) return ''
  return agentRelPath.startsWith('/') ? agentRelPath : `/${agentRelPath}`
}

/**
 * URL-encode each segment of a path while preserving `/` separators.
 *
 * Filenames may contain `#`, `?`, `%`, spaces, and other reserved chars.
 * `encodeURIComponent` on the whole path would mangle slashes; this splits,
 * encodes each segment, and rejoins.
 */
export function encodePathSegments(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/')
}

/** Return the parent directory of an agent-relative path. */
export function parentDir(path: string): string {
  const i = path.lastIndexOf('/')
  return i > 0 ? path.slice(0, i) : ''
}

/**
 * Decide whether a context-menu action should target a multi-selection or
 * just the clicked node. Rule: if the clicked node is part of a multi-item
 * selection, act on the whole selection; otherwise act on just that node.
 *
 * Per-item actions (Rename, Preview, Copy Path) MUST NOT use this helper —
 * they always operate on the single clicked node.
 */
export function resolveActionTargets(clickedPath: string, selected: string[]): string[] {
  if (selected.length > 1 && selected.includes(clickedPath)) return selected
  return [clickedPath]
}

/**
 * Compute the slice of `visibleOrder` from `anchor` to `target` (inclusive,
 * order-independent). Used to implement Shift+click range selection in both
 * grid and tree views. Returns an empty list if either endpoint is missing.
 */
export function computeRangeSelection(
  visibleOrder: string[],
  anchor: string,
  target: string,
): string[] {
  const a = visibleOrder.indexOf(anchor)
  const b = visibleOrder.indexOf(target)
  if (a < 0 || b < 0) return [target] // anchor lost — fall back to single-select
  const [lo, hi] = a <= b ? [a, b] : [b, a]
  return visibleOrder.slice(lo, hi + 1)
}
