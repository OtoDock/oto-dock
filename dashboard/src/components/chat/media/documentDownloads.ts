// The document pane's downloads: the live file as it is through its own
// link, everything else (a version as it is, a conversion of the live file
// or of a version) prepared by the proxy (`POST /v1/documents/download`) and
// started from the media link it answers. Both start through
// `clickDownload`, so the phone apps take them as every other download. A
// file card asks its link first (`checkedDownload`) and says on the card why
// it did not start.

import { apiJson, apiResponse, errorText, raiseApiError } from '@/lib/http/apiJson'
import { clickDownload } from '@/lib/http/download'

/** A download of the pane's menu: "" is the file as it is. */
export type DownloadFormat = '' | 'pdf' | 'csv'

/** The word a person reads for a format. */
export const FORMAT_WORD: Record<'pdf' | 'csv', string> = { pdf: 'PDF', csv: 'CSV' }

/** The conversions the listing offers for a document, the two the menu
 * knows, in its order. */
export function offeredConversions(downloads: readonly string[] | undefined): Array<'pdf' | 'csv'> {
  const offered = new Set(downloads ?? [])
  return (['pdf', 'csv'] as const).filter((f) => offered.has(f))
}

/** A link with the name the phone apps save it under (`fn`). */
export function withName(url: string, filename: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}fn=${encodeURIComponent(filename)}`
}

/** The sentence a failed preparation shows. */
export function failureSentence(e: unknown, format: DownloadFormat): string {
  const said = errorText(e)
  if (said) return said
  return format ? `The ${FORMAT_WORD[format]} could not be made. Try again in a moment.` : 'The download could not be prepared. Try again in a moment.'
}

/** Ask the proxy for a prepared download and start it. Throws an ApiError
 * whose message is the person's sentence. */
export async function prepareAndDownload(req: {
  chatId: string
  fileId: string
  snapshotId: string | null
  format: DownloadFormat
}): Promise<void> {
  const answer = await apiJson<{ download_url: string; filename: string }>('/v1/documents/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    json: { chat_id: req.chatId, file_id: req.fileId, snapshot_id: req.snapshotId ?? '', format: req.format },
  })
  clickDownload(withName(answer.download_url, answer.filename), answer.filename)
}

/** What a file card says when its file is no longer behind its link. */
export const FILE_GONE = 'This file is no longer there. Ask the agent to send it again.'

/** What a file card says when its link gave no usable answer. */
export const DOWNLOAD_NOT_STARTED = 'The download did not start. Try again in a moment.'

/** Start a file card's download once its media link (`/v1/media/<token>`)
 * answers its first byte, with the person's own sign-in. Answers '' when the
 * download started, else the sentence the card shows. The Android app's web
 * view hands the app only a download whose answer succeeded: a file no
 * longer behind its link ends inside the web view with no word to anyone.
 * The players and the document pane say so themselves (a playback error,
 * the pane's band). */
export async function checkedDownload(href: string, filename: string): Promise<string> {
  let res: Response
  try {
    res = await apiResponse(href, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' })
  } catch {
    return DOWNLOAD_NOT_STARTED
  }
  if (res.ok) {
    void res.body?.cancel().catch(() => {})
    clickDownload(href, filename)
    return ''
  }
  if (res.status === 404) return FILE_GONE
  if (res.status === 503) return (await raiseApiError(res).catch(errorText)) || DOWNLOAD_NOT_STARTED
  return DOWNLOAD_NOT_STARTED
}
