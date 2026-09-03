/**
 * Early-mic capture (2026-08-15, the first-words fix): start() opens the mic
 * immediately and buffers frames in a bounded ring until the server 'ready',
 * then flushes frame-by-frame and streams live. 'listening' (the blue halo)
 * requires BOTH mic-wired and ready+flushed. These tests encode the root
 * cause (words spoken during 'connecting' were lost / mangled) and the
 * teardown/race guards around the now-earlier getUserMedia.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => ({
    ok: true, json: async () => ({ ws_token: 'tok-1' }),
  })),
}))

import { useDuplexVoice } from '@/hooks/useDuplexVoice'
import {
  FakeAudioContext, emitFrame, installMicEnv, uninstallMicEnv, type MicEnv,
} from './fakeMicEnv'

class FakeWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static instances: FakeWebSocket[] = []
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  sent: (string | ArrayBuffer)[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: string | ArrayBuffer) { this.sent.push(data) }
  close() { /* keep teardown quiet */ }
}

const binaryFrames = (ws: FakeWebSocket) =>
  ws.sent.filter((s): s is ArrayBuffer => s instanceof ArrayBuffer)

/** First PCM16 sample of a frame — recovers emitFrame's constant value. */
const firstSample = (buf: ArrayBuffer) => new Int16Array(buf)[0]

describe('useDuplexVoice early mic capture', () => {
  let env: MicEnv
  beforeEach(() => {
    FakeWebSocket.instances = []
    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
    env = installMicEnv()
  })
  afterEach(() => { uninstallMicEnv(); vi.unstubAllGlobals() })

  it('opens the mic at start(), before any ready frame', async () => {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    act(() => { hook.result.current.start() })
    expect(env.getUserMedia).toHaveBeenCalledTimes(1)
    await act(async () => {})
    // Session live-ness pending: still connecting, nothing binary sent.
    expect(hook.result.current.phase).toBe('connecting')
    expect(binaryFrames(FakeWebSocket.instances[0])).toHaveLength(0)
  })

  it('buffers frames while connecting, flushes them in order on ready, then streams live', async () => {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    const ws = FakeWebSocket.instances[0]
    const ctx = FakeAudioContext.instances[0]

    emitFrame(ctx, 0.25)
    emitFrame(ctx, 0.5)
    // Still nothing on the wire before ready (the proxy handshake reads
    // text first — binary would break it), and still not 'listening'.
    expect(binaryFrames(ws)).toHaveLength(0)
    expect(hook.result.current.phase).toBe('connecting')

    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }) })
    const flushed = binaryFrames(ws)
    expect(flushed).toHaveLength(2)
    // Order recovered from the constant-valued frames (0.25 then 0.5): the
    // exact PCM16 scaling belongs to the downsampler — only relative order
    // and distinctness are this test's contract.
    expect(firstSample(flushed[0])).toBeGreaterThan(0)
    expect(firstSample(flushed[1])).toBe(firstSample(flushed[0]) * 2 + 1)
    expect(hook.result.current.phase).toBe('listening')

    emitFrame(ctx, 0.75)
    expect(binaryFrames(ws)).toHaveLength(3)     // live streaming now
  })

  it('goes listening when ready arrives BEFORE the mic wires (both orders gate)', async () => {
    const gate = env.defer()
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    const ws = FakeWebSocket.instances[0]

    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }) })
    // Session is live but the mic is not wired yet — blue would be a lie.
    expect(hook.result.current.phase).toBe('connecting')

    await act(async () => { gate.resolve() })
    expect(hook.result.current.phase).toBe('listening')
  })

  it('unmount during connecting stops the mic tracks (no leak, no wake double-open)', async () => {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    expect(env.tracks).toHaveLength(1)
    hook.unmount()
    expect(env.tracks[0].stop).toHaveBeenCalled()
  })

  it('getUserMedia resolving AFTER teardown stops its own tracks', async () => {
    const gate = env.defer()
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    act(() => { hook.result.current.stop() })
    await act(async () => { gate.resolve() })
    expect(env.tracks).toHaveLength(1)
    expect(env.tracks[0].stop).toHaveBeenCalled()
    expect(hook.result.current.phase).toBe('off')
  })

  it('mute during connecting zeroes the flushed burst and re-asserts mute on ready', async () => {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    const ws = FakeWebSocket.instances[0]
    const ctx = FakeAudioContext.instances[0]

    emitFrame(ctx, 0.25)
    act(() => { hook.result.current.toggleMute() })
    emitFrame(ctx, 0.5)
    act(() => { ws.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }) })

    const flushed = binaryFrames(ws)
    expect(flushed).toHaveLength(2)
    // Muted at flush → the WHOLE burst is zeroed (no pre-mute speech leaks).
    expect(flushed.every(f => new Int16Array(f).every(s => s === 0))).toBe(true)
    // The engine had no socket during the mute tap that mattered — the ready
    // path re-asserts it after the flush.
    const jsonAfterFlush = ws.sent
      .slice(ws.sent.indexOf(flushed[1]) + 1)
      .filter((s): s is string => typeof s === 'string')
      .map(s => JSON.parse(s).type)
    expect(jsonAfterFlush).toContain('mute')
  })

  it('a stale ring never flushes into the NEXT session', async () => {
    const hook = renderHook(() => useDuplexVoice('chat-1'))
    await act(async () => { hook.result.current.start() })
    emitFrame(FakeAudioContext.instances[0], 0.25)
    act(() => { hook.result.current.stop() })

    await act(async () => { hook.result.current.start() })
    const ws2 = FakeWebSocket.instances[1]
    act(() => { ws2.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }) })
    expect(binaryFrames(ws2)).toHaveLength(0)
  })
})
