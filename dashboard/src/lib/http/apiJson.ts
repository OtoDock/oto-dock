// One JSON fetch for the dashboard's API hooks: `apiFetch` (the session's
// cookie, the 401 and gate redirects) plus the body read and the refusal
// turned into an Error whose message is the server's sentence. A hook that
// reads a refusal on purpose (a 428 confirm, a 409 with a code) catches the
// ApiError and reads `status` and `detail`.

import { apiFetch } from '../../api/auth'
import { emitAppToast } from '../appToasts'

export class ApiError extends Error {
  status: number
  /** The body's `detail`: a sentence, or the object a route answers with. */
  detail: unknown
  body: unknown
  constructor(status: number, message: string, detail: unknown, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.body = body
  }
}

/** The message a refusal shows: its `detail` sentence, an object detail's
 * `message`, else the caller's fallback, else the status text. */
function messageOf(detail: unknown, fallback: string, statusText: string): string {
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object') {
    const m = (detail as { message?: unknown }).message
    if (typeof m === 'string' && m) return m
  }
  return fallback || statusText || 'The request failed'
}

/** Read a refusal once and raise it. */
export async function raiseApiError(res: Response, fallback = ''): Promise<never> {
  const text = await res.text().catch(() => '')
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    // A proxy's HTML error page or plain text: the status says it.
  }
  const detail = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined
  throw new ApiError(res.status, messageOf(detail, fallback, res.statusText), detail, body)
}

/** `path` as JSON, or an ApiError. `json` is sent as the body; a 204 answers
 * undefined. */
export async function apiJson<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
  fallback = '',
): Promise<T> {
  const { json, ...rest } = init
  const res = await apiFetch(path, json === undefined ? rest : { ...rest, body: JSON.stringify(json) })
  if (!res.ok) await raiseApiError(res, fallback)
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

/** An error handler that shows the failure as a toast on any page, so a
 * mutation whose page moved on (or closed its form) still says it failed.
 * Pair it with `mutateAsync(...).catch(...)`: a `mutate` callback does not
 * run once its component unmounted. */
export function toastError(title: string): (e: unknown) => void {
  return (e: unknown) => {
    emitAppToast({
      title,
      body: e instanceof Error && e.message ? e.message : 'Try again in a moment.',
      severity: 'warning',
    })
  }
}
