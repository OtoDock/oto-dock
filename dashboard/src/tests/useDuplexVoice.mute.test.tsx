/**
 * Sticky mute (operator decision 2026-08-14): the mic mute is USER-OWNED —
 * release() (composer hold ends) and sendTyped() must re-assert the user's
 * intent instead of force-unmuting. Before this, every hold/typed-send cycle
 * silently un-muted a deliberately muted mic.
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

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  sent: string[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { /* keep teardown quiet */ }
}

function typedFrames(ws: FakeWebSocket): string[] {
  return ws.sent
    .filter(s => typeof s === 'string')
    .map(s => { try { return JSON.parse(s).type as string } catch { return '' } })
    .filter(Boolean)
}

describe('useDuplexVoice sticky mute', () => {
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

  it('mute survives release() — the hold/edit cycle re-asserts intent', async () => {
    const { hook, ws } = await startSession()

    act(() => { hook.result.current.toggleMute() })
    expect(hook.result.current.muted).toBe(true)
    expect(typedFrames(ws)).toContain('mute')

    ws.sent = []
    act(() => { hook.result.current.hold() })
    act(() => { hook.result.current.release() })
    // Still muted: release re-sent 'mute', never 'unmute'.
    expect(hook.result.current.muted).toBe(true)
    expect(typedFrames(ws)).toContain('mute')
    expect(typedFrames(ws)).not.toContain('unmute')
  })

  it('mute survives sendTyped() — the reply still plays, the mic stays off', async () => {
    const { hook, ws } = await startSession()
    act(() => { hook.result.current.toggleMute() })

    ws.sent = []
    let ok = false
    act(() => { ok = hook.result.current.sendTyped('turn on the lights') })
    expect(ok).toBe(true)
    expect(hook.result.current.muted).toBe(true)
    expect(typedFrames(ws)).toEqual(['typed_utterance', 'mute'])
  })

  it('an unmuted user gets the classic unmute on release/sendTyped', async () => {
    const { hook, ws } = await startSession()
    ws.sent = []
    act(() => { hook.result.current.release() })
    act(() => { hook.result.current.sendTyped('hello') })
    expect(typedFrames(ws)).toEqual(['unmute', 'typed_utterance', 'unmute'])
    expect(hook.result.current.muted).toBe(false)
  })

  it('a new session resets the mute (per-call intent, not a preference)', async () => {
    const { hook } = await startSession()
    act(() => { hook.result.current.toggleMute() })
    expect(hook.result.current.muted).toBe(true)

    act(() => { hook.result.current.stop() })
    await act(async () => { hook.result.current.start() })
    expect(hook.result.current.muted).toBe(false)
  })

  it('only the user tap unmutes a muted mic', async () => {
    const { hook, ws } = await startSession()
    act(() => { hook.result.current.toggleMute() })

    ws.sent = []
    act(() => { hook.result.current.toggleMute() })
    expect(hook.result.current.muted).toBe(false)
    expect(typedFrames(ws)).toEqual(['unmute'])
  })
})

describe('useDuplexVoice sticky mute — composer hold (operator revision 2026-08-24)', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    installMicEnv()
  })
  afterEach(() => { uninstallMicEnv(); vi.unstubAllGlobals() })

  async function startSession() {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    const ws = FakeWebSocket.instances[0]
    expect(ws).toBeTruthy()
    return { hook, ws }
  }

  it('hold() itself mutes — composer focus is a sticky mute the send keeps', async () => {
    const { hook, ws } = await startSession()
    expect(hook.result.current.muted).toBe(false)
    act(() => { hook.result.current.hold() })
    expect(hook.result.current.muted).toBe(true)
    expect(typedFrames(ws)).toContain('hold')
    // Manual send continues phone mode but does NOT unmute.
    act(() => { hook.result.current.sendTyped('edited text') })
    expect(hook.result.current.muted).toBe(true)
    expect(typedFrames(ws)).not.toContain('unmute')
  })

  it('releaseWithDraft: the unmute tap hands the draft back and unmutes', async () => {
    const { hook, ws } = await startSession()
    act(() => { hook.result.current.hold() })
    expect(hook.result.current.muted).toBe(true)
    ws.sent = []
    let ok = false
    act(() => { ok = hook.result.current.releaseWithDraft('held words') })
    expect(ok).toBe(true)
    expect(hook.result.current.muted).toBe(false)
    const rel = ws.sent
      .map((s) => { try { return JSON.parse(s) } catch { return {} } })
      .find((f) => f.type === 'release')
    expect(rel?.text).toBe('held words')
  })

  it('pause frame flips the halo to listening; resume flips it back', async () => {
    const { hook, ws } = await startSession()
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'state', state: 'speaking' }) }) })
    expect(hook.result.current.phase).toBe('speaking')
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'pause' }) }) })
    expect(hook.result.current.phase).toBe('listening')
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'resume' }) }) })
    expect(hook.result.current.phase).toBe('speaking')
  })
})
