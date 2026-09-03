/**
 * Barge-in flush + perceived TTFA stat.
 *
 * `flush` (daemon barge-in cut) must stop AND null the player so the
 * scheduled 250ms–1s lead goes silent — the next chunk lazily opens a fresh
 * player at base lead. The `ttfa` client_stat arms at the `final` frame
 * (turn dispatched) and is consumed by the FIRST binary chunk after it —
 * one per turn, outside the 40-stat underrun/longtask cap.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

vi.mock('@/api/auth', () => ({
  apiFetch: vi.fn(async () => ({
    ok: true, json: async () => ({ ws_token: 'tok-1' }),
  })),
}))

const players: Array<{ enqueue: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; getLevel: () => number }> = []
vi.mock('@/audio/webaudioPlayer', () => ({
  createPcmPlayer: vi.fn(() => {
    const p = { enqueue: vi.fn(), stop: vi.fn(), getLevel: () => 0 }
    players.push(p)
    return p
  }),
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
  onmessage: ((e: { data: string | ArrayBuffer }) => void) | null = null
  constructor(public url: string) { FakeWebSocket.instances.push(this) }
  send(data: string) { this.sent.push(data) }
  close() { /* keep teardown quiet */ }
}

describe('useDuplexVoice flush + ttfa', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    players.length = 0
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

  function frame(ws: FakeWebSocket, msg: Record<string, unknown>) {
    act(() => { ws.onmessage?.({ data: JSON.stringify(msg) }) })
  }

  function audio(ws: FakeWebSocket) {
    act(() => { ws.onmessage?.({ data: new ArrayBuffer(960) }) })
  }

  function ttfaFrames(ws: FakeWebSocket) {
    return ws.sent
      .map(s => { try { return JSON.parse(s) } catch { return null } })
      .filter((f): f is Record<string, unknown> =>
        !!f && f.type === 'client_stat' && f.kind === 'ttfa')
  }

  it('flush stops and nulls the player; next chunk opens a fresh one', async () => {
    const { ws } = await startSession()

    audio(ws)
    expect(players.length).toBe(1)
    audio(ws)
    expect(players.length).toBe(1)          // mid-reply chunks reuse it

    frame(ws, { type: 'flush' })
    expect(players[0].stop).toHaveBeenCalledTimes(1)

    audio(ws)                               // next reply: fresh player
    expect(players.length).toBe(2)
    // Idempotent after a manual tap already nulled the player.
    frame(ws, { type: 'flush' })
    frame(ws, { type: 'flush' })
    expect(players[1].stop).toHaveBeenCalledTimes(1)
  })

  it('ttfa arms on final and fires once on the first following chunk', async () => {
    const { ws } = await startSession()

    audio(ws)                               // no final yet → no stat
    expect(ttfaFrames(ws).length).toBe(0)

    frame(ws, { type: 'final', text: 'what time is it' })
    audio(ws)
    const stats = ttfaFrames(ws)
    expect(stats.length).toBe(1)
    expect(typeof stats[0].ms).toBe('number')

    audio(ws)                               // mid-turn chunks don't re-fire
    expect(ttfaFrames(ws).length).toBe(1)

    frame(ws, { type: 'final', text: 'and tomorrow?' })
    audio(ws)                               // next turn re-arms
    expect(ttfaFrames(ws).length).toBe(2)
  })
})
