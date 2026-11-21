/**
 * The wrap-up after a conversation: its state on the conversation's record
 * (`conversation_records.wrapup`, '' before the call ends) and the kinds of
 * thing it wrote. The words mirror `proxy/storage/chat/conversation_store.py`
 * (`WRAPUP_*`) and the wrap-up's `wrote` list (`services/phone/wrapup.py`).
 */

export const WRAPUP_STATE = {
  PENDING: 'pending',
  RUNNING: 'running',
  DONE: 'done',
  SKIPPED: 'skipped',
  FAILED: 'failed',
  /** The agent's managers turned it off: nothing ran. */
  OFF: 'off',
} as const
export type WrapupState = (typeof WRAPUP_STATE)[keyof typeof WRAPUP_STATE]

/** What a wrap-up wrote: a memory topic of the person's, or one of their files. */
export const WROTE_KIND = {
  MEMORY: 'memory',
  FILE: 'file',
} as const
