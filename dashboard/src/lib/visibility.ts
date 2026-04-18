/**
 * Agent visibility modes — the dashboard mirror of `proxy/core/session/visibility.py`.
 *
 * Four declarative modes over two independent columns
 * (`collaborative` × `default_scope`). The backend owns the behavior; the
 * dashboard owns the user-facing labels and the per-mode UI gating (which
 * memory rows / workspace chips / scope toggles to show).
 *
 *   collaborative=true,  scope=user   → Personal + shared
 *   collaborative=true,  scope=agent  → Shared + personal
 *   collaborative=false, scope=user   → Personal only   (no shared dirs at all)
 *   collaborative=false, scope=agent  → Shared only     (no user dirs; one shared history)
 */

export type DefaultScope = 'user' | 'agent'

export type VisibilityMode =
  | 'personal_shared' // collaborative + user  default
  | 'shared_personal' // collaborative + agent default
  | 'personal_only' //   non-collab    + user  → user dirs only
  | 'shared_only' //     non-collab    + agent → shared dirs only, one history

/** Map the two columns to a stable mode key (mirrors `mode_for`). */
export function modeOf(collaborative: boolean, defaultScope: DefaultScope): VisibilityMode {
  if (collaborative) return defaultScope === 'user' ? 'personal_shared' : 'shared_personal'
  return defaultScope === 'agent' ? 'shared_only' : 'personal_only'
}

/** Inverse of {@link modeOf} — the two columns to persist for a mode. */
export function columnsOf(mode: VisibilityMode): { collaborative: boolean; default_scope: DefaultScope } {
  switch (mode) {
    case 'personal_shared':
      return { collaborative: true, default_scope: 'user' }
    case 'shared_personal':
      return { collaborative: true, default_scope: 'agent' }
    case 'personal_only':
      return { collaborative: false, default_scope: 'user' }
    case 'shared_only':
      return { collaborative: false, default_scope: 'agent' }
  }
}

/**
 * Resolve an agent's mode from its summary/info row. `collaborative` may be
 * absent on a stale cache entry — soft-fall to the widest (collaborative,
 * user) mode, matching the backend resolver's best-effort default.
 */
export function modeOfAgent(
  agent: { collaborative?: boolean; default_scope?: DefaultScope } | null | undefined,
): VisibilityMode {
  return modeOf(agent?.collaborative ?? true, agent?.default_scope ?? 'user')
}

/** Agent-level scopes a mode offers (mirrors `available_scopes_for`). */
export function availableScopes(mode: VisibilityMode): DefaultScope[] {
  switch (mode) {
    case 'personal_shared':
    case 'shared_personal':
      return ['user', 'agent']
    case 'personal_only':
      return ['user']
    case 'shared_only':
      return ['agent']
  }
}

export const hasUserScope = (mode: VisibilityMode): boolean => availableScopes(mode).includes('user')
export const hasAgentScope = (mode: VisibilityMode): boolean => availableScopes(mode).includes('agent')
export const isCollaborative = (mode: VisibilityMode): boolean =>
  mode === 'personal_shared' || mode === 'shared_personal'
export const isSharedOnly = (mode: VisibilityMode): boolean => mode === 'shared_only'
export const isPersonalOnly = (mode: VisibilityMode): boolean => mode === 'personal_only'

/**
 * Which memory rows the agent's Memory card should show. A mode that doesn't
 * offer a scope can't store memory in it (mirrors `memory_user_enabled` /
 * `memory_agent_enabled` zeroing the unavailable scope).
 */
export const showsUserMemory = (mode: VisibilityMode): boolean => hasUserScope(mode)
export const showsAgentMemory = (mode: VisibilityMode): boolean => hasAgentScope(mode)

/** Short UI label for a mode — the four locked names. */
export const MODE_LABEL: Record<VisibilityMode, string> = {
  personal_shared: 'Personal + shared',
  shared_personal: 'Shared + personal',
  personal_only: 'Personal only',
  shared_only: 'Shared only',
}

/** One-line plain-English summary shown live under the mode selector. */
export const MODE_SUMMARY: Record<VisibilityMode, string> = {
  personal_shared:
    'Each person gets a private space; a shared team space is also available. Separate chats and memory per person.',
  shared_personal:
    'Work lives in one shared team space; each person also keeps personal files. Separate chats and memory per person.',
  personal_only:
    'Fully private to each person — no shared space at all. Separate chats and memory per person.',
  shared_only:
    'One shared workspace and one shared chat history for everyone. No personal space; everyone sees the same conversations.',
}

/** Per-option helper text inside the grouped radio. */
export const MODE_OPTION_HINT: Record<VisibilityMode, string> = {
  personal_shared: 'each person has a private space; a shared team space is also available',
  shared_personal: 'work lives in one shared space; each person also keeps personal files',
  personal_only: 'fully private per person; no shared space; per user chats & memory',
  shared_only: 'one shared workspace & one shared chat history for everyone; no personal space',
}

/**
 * The two radio groups, in display order. Group 1 = collaborative (both
 * spaces available); group 2 = private (a single space only).
 */
export const MODE_GROUPS: { label: string; modes: VisibilityMode[] }[] = [
  { label: 'Collaborative — a team shares this agent', modes: ['personal_shared', 'shared_personal'] },
  { label: 'Private — one space only', modes: ['personal_only', 'shared_only'] },
]
