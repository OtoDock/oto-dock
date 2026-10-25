import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'

import { apiJson, apiOk } from '../lib/http/apiJson'
import type { DbMessageRow } from './wireEvents'

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

/** Report one agent reply, named by its first stored row, to the admins of
 *  this OtoDock. A repeat of the same reply answers the same. */
export async function reportReply(
  chatId: string,
  body: { message_id: number; reason: ReplyReportReason; note: string },
): Promise<void> {
  await apiOk(`/v1/chats/${encodeURIComponent(chatId)}/reports`, { method: 'POST', json: body },
    'The report was not sent. Try again.')
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
  chat_id: string
  chat_title: string
  chat_agent: string
  message_id: number
  /** The reply's time, '' when its first row is gone. */
  reply_at: string
  agent: string
  reason: ReplyReportReason
  note: string
  reply_text: string
  /** When an admin dismissed it to the page's history, '' while open. */
  dismissed_at: string
  dismissed_by_name: string
}

/** The admins' two lists: the open reports and the dismissed history. */
export const REPORT_STATE = { OPEN: 'open', DISMISSED: 'dismissed' } as const
export type ReportState = (typeof REPORT_STATE)[keyof typeof REPORT_STATE]

/** A page of reports; the open list also counts the dismissed history. */
interface ReplyReportPage { reports: ReplyReport[]; has_more: boolean; dismissed_count?: number }

/** The open reports or the dismissed ones, newest first, a page of 50 at a
 *  time (admins only). */
export function useReplyReports(state: ReportState = REPORT_STATE.OPEN, enabled = true) {
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

/** Dismiss a report to the history (`dismiss: true`) or restore it; both
 *  lists and the count read again after. Nothing is deleted. */
export function useSetReportDismissed() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, dismiss }: { id: number; dismiss: boolean }) =>
      apiOk(`/v1/admin/reply-reports/${id}/${dismiss ? 'dismiss' : 'restore'}`, { method: 'POST' },
        dismiss ? 'The report was not dismissed.' : 'The report was not restored.'),
    onSettled: () => qc.invalidateQueries({ queryKey: ['reply-reports'] }),
  })
}
