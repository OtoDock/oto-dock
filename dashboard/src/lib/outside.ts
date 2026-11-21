/** What an outside person (a caller on the agent's phone line, a visitor
 *  who is not a member) gets of a tool, or of their own files. The words
 *  mirror `proxy/services/mcp/outside_picks.py`, which names them once. */
export const OUTSIDE_ACCESS = {
  READ_ONLY: 'read_only',
  READ_WRITE: 'read_write',
  CUSTOM: 'custom',
} as const

export type OutsideAccess = (typeof OUTSIDE_ACCESS)[keyof typeof OUTSIDE_ACCESS]
/** The files choice has no custom list. */
export type FilesAccess = typeof OUTSIDE_ACCESS.READ_ONLY | typeof OUTSIDE_ACCESS.READ_WRITE

export const ACCESS_LABEL: Record<OutsideAccess, string> = {
  [OUTSIDE_ACCESS.READ_ONLY]: 'Read only',
  [OUTSIDE_ACCESS.READ_WRITE]: 'Read and write',
  [OUTSIDE_ACCESS.CUSTOM]: 'Custom',
}

/** The pick words in the order a picker lists them; '' is "Not picked". */
export const ACCESS_ORDER: OutsideAccess[] = [
  OUTSIDE_ACCESS.READ_ONLY, OUTSIDE_ACCESS.READ_WRITE, OUTSIDE_ACCESS.CUSTOM,
]

export function isOutsideAccess(v: string): v is OutsideAccess {
  return (ACCESS_ORDER as string[]).includes(v)
}

/** A limit on a tool the agent itself uses (`proxy/services/mcp/tool_limits.py`):
 *  Read only or some of its functions; all tools is the absence of a limit. */
export type LimitAccess = typeof OUTSIDE_ACCESS.READ_ONLY | typeof OUTSIDE_ACCESS.CUSTOM

/** The two columns a manager sets on the What the agent may use card. The
 *  outside column is the External identities card's. */
export const LIMIT_COLUMN = { RUNS: 'runs', CHATS: 'chats' } as const
export type LimitColumn = (typeof LIMIT_COLUMN)[keyof typeof LIMIT_COLUMN]

export const LIMIT_COLUMN_LABEL: Record<LimitColumn, string> = {
  [LIMIT_COLUMN.RUNS]: "The agent's own runs",
  [LIMIT_COLUMN.CHATS]: "People's chats",
}

/** The word a Select shows for no limit. */
export const ALL_TOOLS = 'all'
