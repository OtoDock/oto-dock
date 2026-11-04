/**
 * The cards' words, mirrored from the proxy's authority
 * `proxy/services/cards/status.py` (edit both): a button's kind, an
 * outcome's state, and the sentences the card shows for what the server
 * never answers in words.
 */
export const BUTTON_KIND = {
  DO: 'do',
  SAY: 'say',
} as const
export type ButtonKind = (typeof BUTTON_KIND)[keyof typeof BUTTON_KIND]

export const OUTCOME = {
  DONE: 'done',
  RUNNING: 'running',
  FAILED: 'failed',
} as const
export type OutcomeState = (typeof OUTCOME)[keyof typeof OUTCOME]

/** A click's state (`card_clicks.status`), mirrored from
 *  `proxy/storage/cards/db_cards.py`. */
export const CLICK_STATUS = {
  RUNNING: 'running',
  DONE: 'done',
  FAILED: 'failed',
  SAID: 'said',
} as const
export type ClickStatus = (typeof CLICK_STATUS)[keyof typeof CLICK_STATUS]

/** The member states of a department set's install, mirrored from
 *  `proxy/services/community/department_set_installer.py`. */
export const SET_MEMBER = {
  PENDING: 'pending',
  INSTALLING: 'installing',
  INSTALLED: 'installed',
  ADOPTED: 'adopted',
  FAILED: 'failed',
} as const
export type SetMemberState = (typeof SET_MEMBER)[keyof typeof SET_MEMBER]

export const SET_MEMBER_WORDS: Record<SetMemberState, string> = {
  [SET_MEMBER.PENDING]: 'Waiting',
  [SET_MEMBER.INSTALLING]: 'Installing',
  [SET_MEMBER.INSTALLED]: 'Installed',
  [SET_MEMBER.ADOPTED]: 'Used',
  [SET_MEMBER.FAILED]: 'Not installed',
}

/** The kinds of list a list card shows (`status.LIST_KINDS`): the pages'
 *  own lists. */
export const LIST_KIND = {
  TASKS: 'tasks',
  TRIGGERS: 'triggers',
  NOTIFICATIONS: 'notifications',
  MACHINES: 'machines',
  MCPS: 'mcps',
} as const
export type ListKind = (typeof LIST_KIND)[keyof typeof LIST_KIND]

/** A list's `scope` filter (`lists.SCOPES`): the person's own rows, the
 *  agent's, or both. */
export const LIST_SCOPE = {
  MINE: 'mine',
  AGENT: 'agent',
  ALL: 'all',
} as const

/** A list's `status` filter (`lists.STATUSES`). */
export const LIST_STATUS = {
  ACTIVE: 'active',
  PAUSED: 'paused',
} as const

export const CARD_WORDS = {
  /** The caption under a person's message a card sent (`status.FROM_CARD`). */
  fromCard: 'From a card',
  reading: 'Reading the card.',
  notRead: 'The card did not load. Reload the page to see it.',
  working: 'Working on it.',
  /** The list card's own sentences (`lists.MANAGERS_SEE_MCPS` for the second). */
  managersOnly: "Only managers and admins see the agent's MCP servers.",
  notInVersion: 'This list is not in this version.',
  listReading: 'Reading the list.',
  listNotRead: 'The list did not load. Reload the page to see it.',
  listEmpty: 'Nothing to show.',
  openPage: 'Open the page',
  openRun: 'Open the run',
} as const
