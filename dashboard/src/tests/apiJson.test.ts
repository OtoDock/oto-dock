// The one JSON fetch the hooks share: the body on success, the server's
// sentence as the message of a refusal (with its status and detail kept for a
// caller that reads them), and a toast for a failure whose page moved on.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/api/auth', () => ({ apiFetch: h.apiFetch }))

import { ApiError, apiJson, toastError } from '@/lib/http/apiJson'
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
