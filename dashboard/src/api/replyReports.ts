import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { DisplayMessage, MessageBlock } from '../components/chat/types'
import { ApiError, apiJson, apiOk } from '../lib/http/apiJson'
import { storedRowId } from '../lib/messageBlocks'
import { WIRE, type DbMessageRow } from './wireEvents'

/** What a person picks when reporting an agent's reply, in the dialog's
 *  order: the words the proxy takes (`api/agents/reply_reports.py`). */
export const REPLY_REPORT_REASONS = [
  { value: 'harmful', label: 'Harmful or offensive' },
  { value: 'misleading', label: 'Wrong or misleading' },
  { value: 'private', label: 'Private information' },
  { value: 'other', label: 'Something else' },
] as const

export type ReplyReportReason = (typeof REPLY_REPORT_REASONS)[number]['value']

/** The note's cap, the proxy's `NOTE_MAX`. */
export const REPLY_REPORT_NOTE_MAX = 1000

/** The parts of a reply with no text that a report names, one line each
 *  (the proxy's `reply_reports.describe`): a reply made of them alone has a
 *  flag too. Thinking and the platform's own blocks are not among them. */
const DESCRIBED: ReadonlySet<string> = new Set([
  'tool', 'setting', 'card', 'list', 'images', 'ui', 'file', 'document_preview', 'video', 'audio', 'url',
])

/** Whether a reply has something to report: its text, or a part the
 *  proxy describes. */
export function reportableReply(blocks: MessageBlock[]): boolean {
  return blocks.some((b) => b.type === WIRE.TEXT || DESCRIBED.has(b.type))
}

/** Whether this person reported the bubble `messages[index]`: a reported
 *  first row from its own first stored row up to the next stored bubble's
 *  (the server may name a reply by a row inside the bubble the history
 *  builder drew). The oldest loaded bubble, when a page boundary cuts its
 *  reply, starts after the reported row and reads unreported until the
 *  older page lands. */
export function reportedBubble(reported: readonly number[] | undefined, messages: DisplayMessage[], index: number): boolean {
  const first = storedRowId(messages[index].id)
  if (first === null || !reported?.length) return false
  let next = Infinity
  for (let i = index + 1; i < messages.length; i++) {
    const id = storedRowId(messages[i].id)
    if (id !== null) {
      next = id
      break
    }
  }
  return reported.some((id) => id >= first && id < next)
}

/** What a report's sending answers: the first stored row of the reply it
 *  names, the id "Reported" keeps. */
interface ReportAnswer { status: string; message_id: number }

/** Report one agent reply, named by any of its stored rows, to the admins of
 *  this OtoDock. A repeat of the same reply answers the same. */
export async function reportReply(
  chatId: string,
  body: { message_id: number; reason: ReplyReportReason; note: string },
): Promise<number> {
  const answer = await apiJson<ReportAnswer>(`/v1/chats/${encodeURIComponent(chatId)}/reports`,
    { method: 'POST', json: body }, 'The report was not sent. Try again.')
  return answer.message_id
}

/** Report an agent's reply from a shared copy of a chat: the share decides
 *  who may, the block is named by its row in the copy. */
export async function reportSharedReply(
  shareId: string,
  body: { message_id: number; reason: ReplyReportReason; note: string },
): Promise<number> {
  const answer = await apiJson<ReportAnswer>(`/v1/shares/${encodeURIComponent(shareId)}/reports`,
    { method: 'POST', json: body }, 'The report was not sent. Try again.')
  return answer.message_id
}

/** The first rows of the replies this person reported in a chat, so the
 *  flag stays "Reported" across a reload: one read per chat, shared by
 *  every flag, kept until a report of theirs adds to it. */
export function useMyReportedReplies(chatId: string | null | undefined) {
  return useQuery({
    queryKey: ['reply-reports', 'mine', chatId],
    queryFn: async () => (await apiJson<{ message_ids: number[] }>(
      `/v1/chats/${encodeURIComponent(chatId || '')}/reports/mine`, {}, '')).message_ids,
    enabled: !!chatId,
    staleTime: Infinity,
    retry: false,
  })
}

/** Add a reply just reported to the chat's "Reported" set. */
export function useAddMyReportedReply() {
  const qc = useQueryClient()
  return (chatId: string, messageId: number) => {
    qc.setQueryData<number[]>(['reply-reports', 'mine', chatId],
      (ids) => (ids?.includes(messageId) ? ids : [...(ids ?? []), messageId]))
  }
}

/** One page of the chat's stored rows, oldest first: the newest `limit`
 *  (older than `beforeId` when given), and whether older ones remain. */
export async function fetchRowsPage(
  chatId: string, beforeId?: number, limit = 200,
): Promise<{ messages: DbMessageRow[]; has_more: boolean }> {
  const before = beforeId ? `&before_id=${beforeId}` : ''
  return apiJson<{ messages: DbMessageRow[]; has_more: boolean }>(
    `/v1/chats/${encodeURIComponent(chatId)}?limit=${limit}${before}`, {}, 'The report was not sent. Try again.')
}

/** One report as the admins' page reads it (`GET /v1/admin/reply-reports`). */
export interface ReplyReport {
  id: number
  created_at: string
  reporter_sub: string
  reporter_name: string
  /** '' once the chat was deleted (the report outlives it). */
  chat_id: string
  /** The chat's title, or the one it had when it was reported. */
  chat_title: string
  /** '' once the chat was deleted. */
  chat_agent: string
  message_id: number
  /** The reply's time, '' when its first row is gone. */
  reply_at: string
  agent: string
  reason: ReplyReportReason
  note: string
  reply_text: string
  state: ReplyReportState
  /** When an admin marked it handled or dismissed it, '' while open. */
  closed_at: string
  closed_by_name: string
}

/** Where a report stands: open, or closed one of two ways. */
export const REPORT_STATE = { OPEN: 'open', HANDLED: 'handled', DISMISSED: 'dismissed' } as const
export type ReplyReportState = (typeof REPORT_STATE)[keyof typeof REPORT_STATE]

/** The admins' two lists: the open reports and the closed ones. */
export const REPORT_LIST = { OPEN: 'open', CLOSED: 'closed' } as const
export type ReportList = (typeof REPORT_LIST)[keyof typeof REPORT_LIST]

/** A page of reports; the open list also counts the closed ones. */
interface ReplyReportPage { reports: ReplyReport[]; has_more: boolean; closed_count?: number }

/** The open reports or the closed ones, newest filed first, a page of 50 at
 *  a time (admins only). */
export function useReplyReports(state: ReportList = REPORT_LIST.OPEN, enabled = true) {
  return useInfiniteQuery({
    queryKey: ['reply-reports', state],
    queryFn: ({ pageParam }) => apiJson<ReplyReportPage>(
      `/v1/admin/reply-reports?state=${state}&limit=50${pageParam ? `&before_id=${pageParam}` : ''}`, {},
      'Could not load the reports'),
    initialPageParam: 0,
    getNextPageParam: (last: ReplyReportPage) =>
      last.has_more && last.reports.length ? last.reports[last.reports.length - 1].id : undefined,
    enabled,
    staleTime: 15_000,
  })
}

const STATE_ROUTE: Record<ReplyReportState, string> = {
  [REPORT_STATE.OPEN]: 'restore', [REPORT_STATE.HANDLED]: 'handled', [REPORT_STATE.DISMISSED]: 'dismiss',
}

/** Mark a report handled, dismiss it, or open it again; both lists and the
 *  count read again after. */
export function useSetReportState() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, state }: { id: number; state: ReplyReportState }) =>
      apiOk(`/v1/admin/reply-reports/${id}/${STATE_ROUTE[state]}`, { method: 'POST' },
        'The report was not changed.'),
    onSettled: () => qc.invalidateQueries({ queryKey: ['reply-reports'] }),
  })
}

/** Delete a report and its copy of the reply; one another admin deleted
 *  first counts as deleted. Both lists read again after. */
export function useDeleteReport() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id }: { id: number }) => {
      try {
        await apiOk(`/v1/admin/reply-reports/${id}`, { method: 'DELETE' }, 'The report was not deleted.')
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 404)) throw e
      }
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['reply-reports'] }),
  })
}
