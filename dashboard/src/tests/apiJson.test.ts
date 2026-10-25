// The one JSON fetch the hooks share: the body on success, the server's
// sentence as the message of a refusal (with its status and detail kept for a
// caller that reads them), and a toast for a failure whose page moved on.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))

import {
  ApiError, apiJson, apiOk, apiResponse, errorName, errorText, raiseApiError, refusalProblems, toastError, withFallback,
  withMessage,
} from '@/lib/http/apiJson'
import { ConfirmRequired } from '@/api/shares'
import { onAppToast } from '@/lib/appToasts'

/** The error a call is refused with (a call that answers fails the test). */
const refusal = (p: Promise<unknown>): Promise<ApiError> =>
  p.then(() => { throw new Error('the call was not refused') }, (x: unknown) => x as ApiError)

const answer = (status: number, body: string, statusText = '') =>
  new Response(status === 204 ? null : body, { status, statusText })

beforeEach(() => h.apiFetch.mockReset())
afterEach(() => vi.restoreAllMocks())

describe('apiJson', () => {
  it('answers the body, sends `json` as the body, and a 204 as undefined', async () => {
    h.apiFetch.mockResolvedValueOnce(answer(200, '{"ok":true}'))
    expect(await apiJson('/v1/x', { method: 'POST', json: { a: 1 } })).toEqual({ ok: true })
    expect(h.apiFetch).toHaveBeenCalledWith('/v1/x', { method: 'POST', body: '{"a":1}' })
    h.apiFetch.mockResolvedValueOnce(answer(204, ''))
    expect(await apiJson('/v1/x', { method: 'DELETE' })).toBeUndefined()
  })

  it('raises the server sentence with its status and detail', async () => {
    h.apiFetch.mockResolvedValueOnce(answer(400, '{"detail":"an admin capped share expiry at 30 days"}'))
    const e = await refusal(apiJson('/v1/shares'))
    expect(e).toBeInstanceOf(ApiError)
    expect(e.message).toBe('an admin capped share expiry at 30 days')
    expect(e.status).toBe(400)
  })

  it('keeps an object detail for the caller and words it by its message', async () => {
    h.apiFetch.mockResolvedValueOnce(answer(409, '{"detail":{"code":"shared_only_removals","message":"Confirm the removals"}}'))
    const e = await refusal(apiJson('/v1/agents/a', {}, 'Could not save'))
    expect(e.message).toBe('Confirm the removals')
    expect((e.detail as { code: string }).code).toBe('shared_only_removals')
  })

  it('falls back on a body that is not JSON (a proxy error page)', async () => {
    h.apiFetch.mockResolvedValueOnce(answer(502, '<html>Bad gateway</html>', 'Bad Gateway'))
    expect((await refusal(apiJson('/v1/x', {}, 'Could not load'))).message).toBe('Could not load')
    h.apiFetch.mockResolvedValueOnce(answer(502, '<html>Bad gateway</html>', 'Bad Gateway'))
    expect((await refusal(apiJson('/v1/x'))).message).toBe('Bad Gateway')
  })

  it("shows the caller's sentence where the refusal carries none for a person", async () => {
    // A validation error's list, an empty detail, an empty body, and the
    // proxy's own words for an error no route handled.
    for (const [status, body] of [
      [422, '{"detail":[{"loc":["body","name"],"msg":"field required","type":"missing"}]}'],
      [400, '{"detail":""}'],
      [404, ''],
      [500, '{"detail":"Internal Server Error"}'],
      [409, '{"detail":{"code":"slug_taken"}}'],
    ] as const) {
      h.apiFetch.mockResolvedValueOnce(answer(status, body, 'Whatever'))
      const e = await refusal(apiJson('/v1/x', {}, 'Could not save the file'))
      expect(e.message).toBe('Could not save the file')
      expect(e.status).toBe(status)
    }
  })

  it("shows the caller's sentence, else a plain one, when no answer came back", async () => {
    for (const offline of ['Failed to fetch', 'NetworkError when attempting to fetch resource.', 'Load failed']) {
      h.apiFetch.mockRejectedValueOnce(new TypeError(offline))
      const e = await refusal(apiJson('/v1/x', {}, 'Could not save the file'))
      expect(e).toBeInstanceOf(ApiError)
      expect(e.message).toBe('Could not save the file')
      expect(e.status).toBe(0)
      h.apiFetch.mockRejectedValueOnce(new TypeError(offline))
      expect((await refusal(apiOk('/v1/x'))).message).toBe('The server did not answer.')
      h.apiFetch.mockRejectedValueOnce(new TypeError(offline))
      expect((await refusal(apiResponse('/v1/x', {}, 'Could not load the app'))).message).toBe('Could not load the app')
    }
  })

  it('lets an aborted call through as it is', async () => {
    const aborted = new DOMException('The operation was aborted.', 'AbortError')
    h.apiFetch.mockRejectedValueOnce(aborted)
    await expect(apiJson('/v1/x', {}, 'Could not load')).rejects.toBe(aborted)
  })

  it('raiseApiError leaves the caller its own status branch before the refusal', async () => {
    const read = async (res: Response) => {
      if (res.status === 404) return null
      if (!res.ok) await raiseApiError(res, 'Could not load the app')
      return res.json()
    }
    expect(await read(answer(404, '{"detail":"App not found"}'))).toBeNull()
    const e = await refusal(read(answer(403, '{"detail":"Access denied"}')))
    expect(e.message).toBe('Access denied')
    expect(e.status).toBe(403)
    expect(e.body).toEqual({ detail: 'Access denied' })
  })

  it('a share write asking for the confirm is still a ConfirmRequired', async () => {
    const { useCreateShare } = await import('@/api/shares')
    expect(typeof useCreateShare).toBe('function')
    const c = new ConfirmRequired('passkey', 'Confirm it is you')
    expect(c).toBeInstanceOf(ApiError)
    expect(c.status).toBe(428)
  })
})

describe('toastError', () => {
  it('raises a warning toast with the failure in it', () => {
    const seen: unknown[] = []
    const off = onAppToast((t) => seen.push(t))
    toastError('The app was not removed')(new Error('Share not found'))
    toastError('The app was not removed')('weird')
    off()
    expect(seen).toEqual([
      { title: 'The app was not removed', body: 'Share not found', severity: 'warning' },
      { title: 'The app was not removed', body: 'Try again in a moment.', severity: 'warning' },
    ])
  })
})

describe('apiOk', () => {
  it('leaves the success body unread and raises a refusal as apiJson does', async () => {
    h.apiFetch.mockResolvedValueOnce(answer(200, ''))
    await expect(apiOk('/v1/x', { method: 'POST', json: { a: 1 } })).resolves.toBeUndefined()
    expect(h.apiFetch).toHaveBeenCalledWith('/v1/x', { method: 'POST', body: '{"a":1}' })
    h.apiFetch.mockResolvedValueOnce(answer(403, '{"detail":"Admin required"}'))
    const e = await refusal(apiOk('/v1/x', {}, 'Could not save'))
    expect(e.message).toBe('Admin required')
    expect(e.status).toBe(403)
  })
})

describe('withMessage', () => {
  it("keeps the hook's sentence, the status and the detail", async () => {
    h.apiFetch.mockResolvedValueOnce(answer(404, '{"detail":"Not Found"}'))
    const e = await refusal(apiJson('/v1/x').catch(withMessage('Failed to fetch MCPs')))
    expect(e).toBeInstanceOf(ApiError)
    expect(e.message).toBe('Failed to fetch MCPs')
    expect(e.status).toBe(404)
    expect(e.detail).toBe('Not Found')
  })

  it('builds the sentence from the status when given a function', async () => {
    h.apiFetch.mockResolvedValueOnce(answer(502, 'gateway', 'Bad Gateway'))
    const e = await refusal(apiJson('/v1/x').catch(withMessage((s) => `chat documents: ${s}`)))
    expect(e.message).toBe('chat documents: 502')
  })

  it("words a call with no answer by the hook's sentence, never by its status 0", async () => {
    h.apiFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const e = await refusal(apiJson('/v1/x').catch(withMessage('Failed to fetch MCPs')))
    expect(e.message).toBe('Failed to fetch MCPs')
    expect(e.status).toBe(0)
    h.apiFetch.mockRejectedValueOnce(new TypeError('NetworkError when attempting to fetch resource.'))
    expect((await refusal(apiJson('/v1/x').catch(withMessage((s) => `chat documents: ${s}`)))).message)
      .toBe('The server did not answer.')
  })
})

describe('errorText and errorName', () => {
  it('read the message and the name of any thrown shape, else empty', () => {
    expect(errorText(new Error('boom'))).toBe('boom')
    expect(errorText({ message: 'from a plugin' })).toBe('from a plugin')
    expect(errorText('a string')).toBe('')
    expect(errorText(null)).toBe('')
    expect(errorName(new DOMException('no', 'NotAllowedError'))).toBe('NotAllowedError')
    expect(errorName({ name: 'AbortError' })).toBe('AbortError')
    expect(errorName(42)).toBe('')
  })
})

describe('refusalProblems', () => {
  it("lists a refused template's problems, from an ApiError or the install's own error", async () => {
    const body = { detail: { error: 'invalid_template', message: 'a; b', problems: ['a', '', 'b', 3] } }
    h.apiFetch.mockResolvedValueOnce(answer(400, JSON.stringify(body)))
    const e = await refusal(apiJson('/v1/x'))
    expect(e.message).toBe('a; b')
    expect(refusalProblems(e)).toEqual(['a', 'b'])
    expect(refusalProblems(Object.assign(new Error('x'), { body }))).toEqual(['a', 'b'])
    for (const none of [new Error('x'), null, 'text', { body: { detail: 'Invalid catalog template: a; b' } }]) {
      expect(refusalProblems(none)).toEqual([])
    }
  })
})

describe('withFallback', () => {
  it("keeps the server's sentence, else words the refusal by its status", async () => {
    const fallback = withFallback((s) => `Failed to rename (HTTP ${s})`)
    h.apiFetch.mockResolvedValueOnce(answer(409, '{"detail":"A chat with that title exists"}'))
    expect((await refusal(apiOk('/v1/x').catch(fallback))).message).toBe('A chat with that title exists')
    h.apiFetch.mockResolvedValueOnce(answer(500, 'Internal Server Error', 'Internal Server Error'))
    expect((await refusal(apiOk('/v1/x').catch(fallback))).message).toBe('Failed to rename (HTTP 500)')
    h.apiFetch.mockResolvedValueOnce(answer(500, '{"detail":"Internal Server Error"}', 'Internal Server Error'))
    expect((await refusal(apiOk('/v1/x').catch(fallback))).message).toBe('Failed to rename (HTTP 500)')
    h.apiFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    expect((await refusal(apiOk('/v1/x').catch(fallback))).message).toBe('The server did not answer.')
  })
})
