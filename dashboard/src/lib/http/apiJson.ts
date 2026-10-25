// One JSON fetch for the dashboard's API hooks: `apiFetch` (the session's
// cookie, the 401 and gate redirects) plus the body read and the refusal
// turned into an Error whose message is the server's sentence (a call with
// no answer into one of status 0 with the caller's sentence). A hook that
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

/** The proxy's answer to an error no route handled: words for nobody, so a
 * refusal carrying only these shows the caller's sentence instead. */
const UNHANDLED_ERROR_DETAIL = 'Internal Server Error'

/** A call that got no answer (offline, the server down, a dropped
 * connection) when the caller gave no sentence of its own. */
const NO_ANSWER = 'The server did not answer.'

/** The status of the ApiError a call with no answer raises. */
const NO_RESPONSE_STATUS = 0

/** The sentence a refusal's `detail` carries: the string, or an object
 * detail's `message`; '' when it carries none. */
function detailSentence(detail: unknown): string {
  if (typeof detail === 'string' && detail && detail !== UNHANDLED_ERROR_DETAIL) return detail
  if (detail && typeof detail === 'object') {
    const m = (detail as { message?: unknown }).message
    if (typeof m === 'string' && m) return m
  }
  return ''
}

/** The message a refusal shows: its `detail` sentence, else the caller's
 * fallback, else the status text. */
function messageOf(detail: unknown, fallback: string, statusText: string): string {
  return detailSentence(detail) || fallback || statusText || 'The request failed'
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

/** `apiFetch`, with a call that got no answer raised as an ApiError of
 * status 0 worded by the caller's sentence (else "The server did not
 * answer."): the browser's own words ("Failed to fetch", "NetworkError…",
 * "Load failed") never reach a person. An aborted call passes through. For a
 * caller that reads the Response itself; `apiJson` and `apiOk` use it. */
export async function apiResponse(path: string, init: RequestInit = {}, fallback = ''): Promise<Response> {
  try {
    return await apiFetch(path, init)
  } catch (e) {
    // fetch rejects with a TypeError when no response came back.
    if (e instanceof TypeError) throw new ApiError(NO_RESPONSE_STATUS, fallback || NO_ANSWER, undefined, null)
    throw e
  }
}

/** `path` as JSON, or an ApiError. `json` is sent as the body; a 204 answers
 * undefined. */
export async function apiJson<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
  fallback = '',
): Promise<T> {
  const { json, ...rest } = init
  const res = await apiResponse(path, json === undefined ? rest : { ...rest, body: JSON.stringify(json) }, fallback)
  if (!res.ok) await raiseApiError(res, fallback)
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

/** `path` for its side effect: a refusal raised as apiJson raises it, the
 * success body left unread (a route that answers no JSON, or a caller that
 * ignores it). */
export async function apiOk(
  path: string,
  init: RequestInit & { json?: unknown } = {},
  fallback = '',
): Promise<void> {
  const { json, ...rest } = init
  const res = await apiResponse(path, json === undefined ? rest : { ...rest, body: JSON.stringify(json) }, fallback)
  if (!res.ok) await raiseApiError(res, fallback)
}

/** A refusal that keeps the hook's own sentence whatever the server said:
 * `apiJson(...).catch(withMessage('Failed to load'))`, or one built from the
 * status. The ApiError keeps its status and detail. A call with no answer
 * takes the sentence too, never one built from its status 0; an aborted
 * call passes through unchanged. */
export function withMessage(message: string | ((status: number) => string)): (e: unknown) => never {
  return (e: unknown) => {
    if (e instanceof ApiError && e.status === NO_RESPONSE_STATUS && typeof message !== 'string') throw e
    if (e instanceof ApiError) {
      throw new ApiError(e.status, typeof message === 'string' ? message : message(e.status), e.detail, e.body)
    }
    throw e
  }
}

/** A refusal worded by the server's sentence when it gave one, else by the
 * status: `apiJson(...).catch(withFallback((s) => `Failed to rename (HTTP ${s})`))`. */
export function withFallback(message: (status: number) => string): (e: unknown) => never {
  return (e: unknown) => {
    if (e instanceof ApiError && e.status === NO_RESPONSE_STATUS) throw e
    if (e instanceof ApiError) {
      throw new ApiError(e.status, detailSentence(e.detail) || message(e.status), e.detail, e.body)
    }
    throw e
  }
}

/** A thrown value's `message` (an Error, a DOMException, a plugin's error
 * object), else ''. What a `catch (e)` shows of the failure. */
export function errorText(e: unknown): string {
  if (e && typeof e === 'object' && 'message' in e && typeof e.message === 'string') return e.message
  return ''
}

/** Every problem a refusal lists apart (`detail.problems`, a refused
 * template's findings), else []: read off the parsed body, which an ApiError
 * and the agent install's own error both carry. */
export function refusalProblems(e: unknown): string[] {
  const body = e && typeof e === 'object' ? (e as { body?: unknown }).body : undefined
  const detail = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined
  const problems = detail && typeof detail === 'object' ? (detail as { problems?: unknown }).problems : undefined
  return Array.isArray(problems) ? problems.filter((p): p is string => typeof p === 'string' && p !== '') : []
}

/** A thrown value's `name` ('AbortError', 'NotAllowedError'), else ''. */
export function errorName(e: unknown): string {
  if (e && typeof e === 'object' && 'name' in e && typeof e.name === 'string') return e.name
  return ''
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
