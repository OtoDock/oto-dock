/** Where a pinned row's mark sits in the chat history: in front of the
 * title, after the status and unread dots, or on the time line right after
 * the time. Grey like the time line, with no colour of its own, so it is
 * never read as a delegation edge or a live or unread state. */
export type PinMarkAt = 'title' | 'time'

export const PIN_MARK_AT = 'title' as PinMarkAt
