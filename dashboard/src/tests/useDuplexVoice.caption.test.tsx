/**
 * R4.6 caption accumulation: the STT provider finalizes phrases DURING
 * continuous speech, so the caption must COMMIT `interim_final` chunks and
 * overlay only the live partial from `interim` frames — the same
 * accumulate/overlay pair dictation uses. Before this, the composer reset
 * to each newest phrase and the full utterance only appeared at dispatch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => ({
    ok: true, json: async () => ({ ws_token: 'tok-1' }),
  })),
}))

import { useDuplexVoice } from '@/hooks/useDuplexVoice'
import { installMicEnv, uninstallMicEnv } from './fakeMicEnv'
import { FakeWebSocket } from './fakeWebSocket'

describe('useDuplexVoice caption accumulation', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    installMicEnv() // the mic opens at start() now — jsdom needs the stubs
  })
  afterEach(() => { uninstallMicEnv(); vi.unstubAllGlobals() })

  async function startSession() {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    const ws = FakeWebSocket.instances[0]
    expect(ws).toBeTruthy()
    return { hook, ws }
  }

  function frame(ws: FakeWebSocket, msg: Record<string, unknown>) {
    act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
  }

  it('commits interim_final chunks and overlays the live partial', async () => {
    const { hook, ws } = await startSession()

    frame(ws, { type: 'interim', text: 'hey do' })
    expect(hook.result.current.caption).toBe('hey do')

    // Provider finalized the phrase mid-utterance — committed, partial restarts.
    frame(ws, { type: 'interim_final', text: 'hey, do you copy?' })
    expect(hook.result.current.caption).toBe('hey, do you copy?')

    frame(ws, { type: 'interim', text: 'over' })
    expect(hook.result.current.caption).toBe('hey, do you copy? over')

    frame(ws, { type: 'interim_final', text: 'over and out' })
    expect(hook.result.current.caption).toBe('hey, do you copy? over and out')
  })

  it('the init frame names the browser language and a replace rewrites the committed pieces', async () => {
    const { hook, ws } = await startSession()
    act(() => { ws.onopen?.() })
    const init = JSON.parse(ws.sent[0]) as { type: string; language: string }
    expect(init.type).toBe('init')
    expect(['en', 'el', 'de', 'es', 'fr', 'it']).toContain(init.language)

    frame(ws, { type: 'interim_final', text: 'yes sir' })
    frame(ws, { type: 'interim', text: 'I am' })
    expect(hook.result.current.caption).toBe('yes sir I am')
    // The language switched: the new stream's words stand, the overlay restarts.
    frame(ws, { type: 'replace', text: 'Ναι κύριε' })
    expect(hook.result.current.caption).toBe('Ναι κύριε')
    frame(ws, { type: 'interim', text: 'είμαι' })
    expect(hook.result.current.caption).toBe('Ναι κύριε είμαι')
    frame(ws, { type: 'replace', text: '' })
    expect(hook.result.current.caption).toBe('')
  })

  it('dispatch clears the committed pieces (final + state frames)', async () => {
    const { hook, ws } = await startSession()

    frame(ws, { type: 'interim_final', text: 'turn on the lights' })
    frame(ws, { type: 'final', text: 'turn on the lights' })
    expect(hook.result.current.caption).toBe('')
    frame(ws, { type: 'state', state: 'thinking' })
    expect(hook.result.current.caption).toBe('')

    // The next utterance starts from scratch — nothing resurrects.
    frame(ws, { type: 'interim', text: 'and the' })
    expect(hook.result.current.caption).toBe('and the')
  })

  it('any state edge consumes the caption (re-dispatched queued speech)', async () => {
    const { hook, ws } = await startSession()

    // Queued-speech re-dispatch sends no fresh final/thinking frames —
    // the reply audio ('speaking') is the dispatch signal.
    frame(ws, { type: 'interim_final', text: 'what about Patras?' })
    expect(hook.result.current.caption).toBe('what about Patras?')
    frame(ws, { type: 'state', state: 'speaking' })
    expect(hook.result.current.caption).toBe('')
  })
})
