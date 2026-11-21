import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// A platform call the platform paces is told how long to wait. The host waits and sends once more, so no page
// writes a retry of its own; a second refusal goes back with the wait.
const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('@/api/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/auth')>()),
  apiFetch,
}))

import { callPlatformMethod, fireAppActions, type AppActionResult } from '@/api/apps'

const refused = (ms: number | null, header = '1') => new Response(
  JSON.stringify({ detail: 'Too fast — try again in a moment', ...(ms === null ? {} : { retry_after_ms: ms }) }),
  { status: 429, headers: { 'Retry-After': header } },
)
const answered = () => new Response(JSON.stringify({ ok: true, result: { n: 1 } }), { status: 200 })

describe('callPlatformMethod', () => {
  beforeEach(() => { vi.useFakeTimers(); apiFetch.mockReset() })
  afterEach(() => vi.useRealTimers())

  it('waits as long as it is told and sends once more', async () => {
    apiFetch.mockResolvedValueOnce(refused(250)).mockResolvedValueOnce(answered())
    const call = callPlatformMethod('app-1', 'files.read', { path: 'workspace/a.json' })
    await vi.advanceTimersByTimeAsync(200)
    expect(apiFetch).toHaveBeenCalledTimes(1)                 // still waiting
    await vi.advanceTimersByTimeAsync(100)
    expect(await call).toEqual({ ok: true, result: { n: 1 } })
    expect(apiFetch).toHaveBeenCalledTimes(2)
    expect(apiFetch.mock.calls[1][0]).toBe('/v1/apps/app-1/catalog/files.read')
  })

  it('sends twice at most, and hands the second refusal back with its wait', async () => {
    apiFetch.mockResolvedValueOnce(refused(100)).mockResolvedValueOnce(refused(4000))
    const call = callPlatformMethod('app-1', 'viewer.me', null)
    await vi.advanceTimersByTimeAsync(200)
    expect(await call).toEqual({ ok: false, reason: 'Too fast — try again in a moment', retry_after_ms: 4000 })
    expect(apiFetch).toHaveBeenCalledTimes(2)
  })

  it('reads the header where the body says nothing, and never waits longer than five seconds', async () => {
    apiFetch.mockResolvedValueOnce(refused(null, '2')).mockResolvedValueOnce(answered())
    const call = callPlatformMethod('app-1', 'viewer.me', null)
    await vi.advanceTimersByTimeAsync(1900)
    expect(apiFetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(200)
    expect((await call).ok).toBe(true)
    apiFetch.mockResolvedValueOnce(refused(600_000)).mockResolvedValueOnce(answered())
    const long = callPlatformMethod('app-1', 'viewer.me', null)
    await vi.advanceTimersByTimeAsync(5100)
    expect((await long).ok).toBe(true)
  })

  it('answers every other refusal at once, as before', async () => {
    apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'not declared by this app' }), { status: 404 }))
    expect(await callPlatformMethod('app-1', 'nope', null)).toEqual({ ok: false, reason: 'not declared by this app' })
    expect(apiFetch).toHaveBeenCalledTimes(1)
  })
})

// A button's batch: one batch a second for a person on an app, whatever its buttons. A batch refused whole
// ran nothing, and each of its calls settles with the wait; a call refused inside its own gap brings its wait.
describe('fireAppActions', () => {
  beforeEach(() => { apiFetch.mockReset() })

  it('settles every call of a batch refused whole with the wait, and answers the wait', async () => {
    apiFetch.mockResolvedValueOnce(refused(140))
    const got: Record<string, AppActionResult> = {}
    const paced = await fireAppActions('app-1', [
      { call_id: 'a', action_id: 'tool' }, { call_id: 'b', action_id: 'go' },
    ], (id, r) => { got[id] = r })
    expect(paced).toBe(140)
    for (const id of ['a', 'b']) {
      expect(got[id]).toEqual({
        status: 'denied', reason: 'Too fast — try again in a moment', code: 429, retry_after_ms: 140,
      })
    }
  })

  it('passes on the wait a line names, and answers no wait for a batch that ran', async () => {
    const lines = [
      { call_id: 'a', action_id: 'tool', status: 'denied', reason: 'Too fast — try again in a moment', code: 429, retry_after_ms: 610 },
      { call_id: 'b', action_id: 'tool', status: 'done', result: 'ok' },
    ].map((l) => JSON.stringify(l)).join('\n')
    apiFetch.mockResolvedValueOnce(new Response(lines, { status: 200 }))
    const got: Record<string, AppActionResult> = {}
    const paced = await fireAppActions('app-1', [
      { call_id: 'a', action_id: 'tool' }, { call_id: 'b', action_id: 'tool' },
    ], (id, r) => { got[id] = r })
    expect(paced).toBe(0)
    expect(got.a).toEqual(expect.objectContaining({ status: 'denied', code: 429, retry_after_ms: 610 }))
    expect(got.b).toEqual(expect.objectContaining({ status: 'done', result: 'ok' }))
  })
})
