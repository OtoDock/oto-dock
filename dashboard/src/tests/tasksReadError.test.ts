import { describe, it, expect } from 'vitest'

import { readError } from '@/api/tasks'

// ─── A refused task action shows the server's sentence, not its JSON. The
//     caps answer 429 with a full sentence in `detail` ("You already have 25
//     active scheduled tasks…"); the Schedules pages print the message raw. ──

describe('readError', () => {
  it('returns the detail sentence of a JSON refusal', async () => {
    const res = new Response(JSON.stringify({ detail: 'You already have 25 active scheduled tasks across your agents (the limit is 25). Delete or pause one first.' }), { status: 429 })
    expect(await readError(res)).toBe('You already have 25 active scheduled tasks across your agents (the limit is 25). Delete or pause one first.')
  })

  it('returns a plain body as sent', async () => {
    const res = new Response('Service Unavailable', { status: 503 })
    expect(await readError(res)).toBe('Service Unavailable')
  })

  it('falls back to the status text on an empty body', async () => {
    const res = new Response('', { status: 404, statusText: 'Not Found' })
    expect(await readError(res)).toBe('Not Found')
  })

  it('shows a JSON body without a detail sentence as text', async () => {
    const res = new Response('{"error":"x"}', { status: 400 })
    expect(await readError(res)).toBe('{"error":"x"}')
  })
})
