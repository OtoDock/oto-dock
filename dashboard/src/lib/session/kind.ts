/**
 * The session kinds — the dashboard mirror of the proxy's
 * `core/session/session_kind.py`. The lock-step test
 * `proxy/tests/session/test_session_kind.py` reads this file and asserts the
 * spellings, the id prefix and the external-driven set equal the Python
 * table (keep each constant on one line, single-quoted values).
 *
 * A chat row's kind is read here and nowhere else: the column
 * (`source_type`) first, the `task-` id prefix second — rows the scheduler
 * minted before the proxy wrote the column carry `chat` with a task id,
 * and the live `chat_status` frames carry no column at all, so a live task
 * classifies before its seed row arrives. The dashboard never compares a
 * bare `'task'`, `'phone'` or `'task-'`: it asks this module.
 */

/** The `chats.source_type` spellings of the kinds that mint a chat row. */
export const SOURCE_TYPE = {
  CHAT: 'chat',
  PHONE: 'phone',
  TASK: 'task',
} as const

export type SourceType = (typeof SOURCE_TYPE)[keyof typeof SOURCE_TYPE]

/** The chat ids the scheduler mints: `task-{run_id}`. */
export const TASK_CHAT_ID_PREFIX = 'task-'

/** The kinds an out-of-band driver owns (a phone call): the dashboard views
 *  their chats read-only and the agent Conversations tab lists exactly them. */
export const EXTERNAL_DRIVEN_SOURCE_TYPES: ReadonlySet<string> = new Set(['phone'])

const ROW_SOURCE_TYPES: ReadonlySet<string> = new Set(Object.values(SOURCE_TYPE))

/** The chat id of a task run — the one mint (mirrors `task_chat_id`). */
export function taskChatId(runId: string): string {
  return `${TASK_CHAT_ID_PREFIX}${runId}`
}

/** Whether a chat id has the scheduler's shape (mirrors `is_task_chat_id`). */
export function isTaskChatId(chatId: string | null | undefined): boolean {
  return !!chatId && chatId.startsWith(TASK_CHAT_ID_PREFIX)
}

/** The run id a task chat id carries, `''` for any other id (mirrors `run_id_of_chat`). */
export function runIdOfChat(chatId: string | null | undefined): string {
  return isTaskChatId(chatId) ? (chatId as string).slice(TASK_CHAT_ID_PREFIX.length) : ''
}

/** The kind of a chat row (mirrors `of_chat`): the column first, then the
 *  id for a pre-write task row; an unknown spelling reads as a chat. */
export function chatKind(
  row: { id?: string | null; source_type?: string | null } | null | undefined,
): SourceType {
  const value = row?.source_type || ''
  if (value && value !== SOURCE_TYPE.CHAT) {
    return ROW_SOURCE_TYPES.has(value) ? (value as SourceType) : SOURCE_TYPE.CHAT
  }
  return isTaskChatId(row?.id) ? SOURCE_TYPE.TASK : SOURCE_TYPE.CHAT
}

/** Whether a row's or a pump's kind is externally driven (mirrors the set). */
export function isExternalDriven(sourceType: string | null | undefined): boolean {
  return !!sourceType && EXTERNAL_DRIVEN_SOURCE_TYPES.has(sourceType)
}
