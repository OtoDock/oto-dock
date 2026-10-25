// ─── Platform dictation over a weak link ────────────────────────────────────
// The server's `progress` frames tell the session the link moves; nothing for
// 5 s (or the server's audio far behind what was sent) shows a notice, 30 s
// ends the dictation with the lost-connection sentence. A socket that closes
// on its own says the same; a server error stops the mic at once and keeps
// the socket for the last words; the stop waits for the final marked last.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FakeAudioContext, emitFrame, installMicEnv, uninstallMicEnv, type MicEnv } from './fakeMicEnv'
import { platformStt, SLOW_AFTER_MS, LOST_AFTER_MS, CAP_NOTICE_MS, STOP_BACKLOG_MAX_MS } from '@/audio/backends/platformStt'
import {
  DICTATION_CAP_SOON, DICTATION_FINISHING, DICTATION_LOST, DICTATION_NO_CONNECT, DICTATION_NO_START, DICTATION_NO_START_ADMIN,
  DICTATION_SLOW,
} from '@/audio/backends/dictationWords'
import { STOP_FINAL_WAIT_MS } from '@/audio/backends/platformStt'

type Listener = (ev: { data?: string }) => void

class LinkSocket {
  static OPEN = 1
  static CLOSED = 3
  static instances: LinkSocket[] = []
  readyState = 1
  binaryType = ''
  sent: unknown[] = []
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  closed = false
  private listeners: Record<string, Listener[]> = {}
  constructor(public url: string) { LinkSocket.instances.push(this) }
  send(data: unknown) { this.sent.push(data) }
  close() { this.closed = true }
  addEventListener(kind: string, fn: Listener) { (this.listeners[kind] ??= []).push(fn) }
  removeEventListener() { /* noop */ }
  /** A server frame: the property handler first, then the listeners. */
  frame(msg: object) {
    const data = JSON.stringify(msg)
    this.onmessage?.({ data })
    for (const fn of this.listeners.message ?? []) fn({ data })
  }
  /** The socket closes under the session. */
  drop() {
    this.readyState = LinkSocket.CLOSED
    this.onclose?.()
    for (const fn of this.listeners.close ?? []) fn({})
  }
}

interface Seen { notices: (string | null)[]; errors: string[]; finals: string[]; ended: number; finishing: number }

let mic: MicEnv

async function openSession(ready: object = { progress_every_s: 2, marks_last: true }) {
  const seen: Seen = { notices: [], errors: [], finals: [], ended: 0, finishing: 0 }
  const session = platformStt.create({ language: 'el' })
  session.onNotice?.((t) => { seen.notices.push(t) })
  session.onFinishing?.(() => { seen.finishing += 1 })
  session.onError((e) => { seen.errors.push(e.message) })
  session.onFinal((t) => { seen.finals.push(t) })
  session.onEnd(() => { seen.ended += 1 })
  const started = session.start()
  await vi.waitFor(() => expect(LinkSocket.instances.length).toBe(1))
  const sock = LinkSocket.instances[0]
  sock.onopen?.()
  sock.frame({ type: 'ready', ...ready })
  await started
  return { session, sock, seen, ctx: FakeAudioContext.instances[0] }
}

beforeEach(() => {
  LinkSocket.instances = []
  mic = installMicEnv()
  vi.stubGlobal('WebSocket', LinkSocket as unknown as typeof WebSocket)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ws_token: 'tok' }) })))
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  uninstallMicEnv()
})

describe('the link watch', () => {
  it('says the link is slow after five quiet seconds and clears at the next progress', async () => {
    const { sock, seen, ctx } = await openSession()
    emitFrame(ctx)
    vi.advanceTimersByTime(SLOW_AFTER_MS + 1000)
    expect(seen.notices).toEqual([DICTATION_SLOW])
    sock.frame({ type: 'progress', audio_s: 0.17 })
    expect(seen.notices).toEqual([DICTATION_SLOW, null])
    expect(seen.errors).toEqual([])
  })

  it('ends the dictation after thirty slow seconds, mic released, socket closed', async () => {
    const { sock, seen } = await openSession()
    vi.advanceTimersByTime(LOST_AFTER_MS + 2000)
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(sock.closed).toBe(true)
    expect(mic.tracks[0].stop).toHaveBeenCalled()
    sock.drop()                                            // the close that follows says nothing twice
    expect(seen.errors).toEqual([DICTATION_LOST])
  })

  it('is slow while the server has heard far less than was sent, even with frames coming', async () => {
    const { sock, seen, ctx } = await openSession()
    for (let i = 0; i < 80; i++) emitFrame(ctx)               // about 6.8 s of audio sent
    sock.frame({ type: 'progress', audio_s: 0.5 })
    vi.advanceTimersByTime(1000)
    expect(seen.notices).toEqual([DICTATION_SLOW])
  })

  it('restarts its clock when a hidden page is shown again', async () => {
    const { seen } = await openSession()
    const state = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    vi.advanceTimersByTime(LOST_AFTER_MS + 5000)            // a phone put away
    state.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(1000)
    expect(seen.errors).toEqual([])
    expect(seen.notices).toEqual([])
  })

  it('clears a shown notice when a hidden page is shown again', async () => {
    const { seen } = await openSession()
    vi.advanceTimersByTime(SLOW_AFTER_MS + 1000)
    expect(seen.notices).toEqual([DICTATION_SLOW])
    const state = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
    vi.advanceTimersByTime(3000)
    state.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange'))
    expect(seen.notices).toEqual([DICTATION_SLOW, null])
    state.mockRestore()
  })

  it('does not watch an older server that sends no progress', async () => {
    const { seen } = await openSession({})
    vi.advanceTimersByTime(LOST_AFTER_MS + 5000)
    expect(seen.notices).toEqual([])
    expect(seen.errors).toEqual([])
  })
})

describe('the endings', () => {
  it('says the connection was lost when the socket closes on its own', async () => {
    const { sock, seen } = await openSession()
    sock.drop()
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(seen.ended).toBe(1)
  })

  it('stops the mic on a server error and says it with the last words, not before them', async () => {
    const { sock, seen } = await openSession()
    sock.frame({ type: 'error', code: 'max_seconds', message: 'Dictation reached its time limit and stopped.' })
    expect(mic.tracks[0].stop).toHaveBeenCalled()
    expect(seen.errors).toEqual([])                        // a tap now would see its words come twice
    expect(seen.finishing).toBe(1)                         // the host leaves "recording"
    expect(seen.notices).toEqual([DICTATION_FINISHING])
    sock.frame({ type: 'final', text: 'τα τελευταία λόγια', last: true })
    expect(seen.finals).toEqual(['τα τελευταία λόγια'])
    expect(seen.errors).toEqual(['Dictation reached its time limit and stopped.'])
    sock.drop()
    expect(seen.errors).toEqual(['Dictation reached its time limit and stopped.'])
  })

  it('an older server\'s error shows at once', async () => {
    const { sock, seen } = await openSession({})
    sock.frame({ type: 'error', code: 'max_seconds', message: 'Maximum recording length reached' })
    expect(seen.errors).toEqual(['Maximum recording length reached'])
  })

  it('a stop the server closes without its marked final says the last words were lost', async () => {
    const { session, sock, seen } = await openSession()
    const stopped = session.stop()
    sock.drop()
    await stopped
    expect(seen.errors).toEqual([DICTATION_LOST])
  })

  it('the stop waits for the final marked last, not a window released before it', async () => {
    const { session, sock, seen } = await openSession()
    const stopped = session.stop()
    sock.frame({ type: 'final', text: 'ένα δύο' })          // a held window, released late
    await Promise.resolve()
    expect(sock.closed).toBe(false)
    sock.frame({ type: 'final', text: 'τρία', last: true })
    await stopped
    expect(sock.closed).toBe(true)
    expect(seen.finals).toEqual(['ένα δύο', 'τρία'])
    expect(seen.errors).toEqual([])
  })

  it('an older server resolves the stop on its first final', async () => {
    const { session, sock } = await openSession({})
    const stopped = session.stop()
    sock.frame({ type: 'final', text: 'τέλος' })
    await stopped
    expect(sock.closed).toBe(true)
  })
})

describe('the start in plain words', () => {
  it('a refused session mint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, headers: new Headers(), json: async () => ({}) })))
    await expect(platformStt.create({ language: 'el' }).start()).rejects.toThrow(DICTATION_NO_START_ADMIN)
  })

  it('a socket that never opens', async () => {
    const session = platformStt.create({ language: 'el' })
    const started = session.start()
    await vi.waitFor(() => expect(LinkSocket.instances.length).toBe(1))
    LinkSocket.instances[0].onerror?.()
    await expect(started).rejects.toThrow(DICTATION_NO_CONNECT)
  })
})


describe('the fix round', () => {
  it('a stall then a burst: the notice, never a give-up while the server\'s audio advances', async () => {
    const { sock, seen, ctx } = await openSession()
    vi.advanceTimersByTime(25_000)                          // nothing from the server for 25 s
    expect(seen.notices).toEqual([DICTATION_SLOW])
    for (let i = 0; i < 300; i++) emitFrame(ctx)            // the person kept talking: about 25 s sent
    for (let t = 1; t <= 20; t++) {                         // the link is back but the upload trails
      sock.frame({ type: 'progress', audio_s: t })
      vi.advanceTimersByTime(1000)
    }
    expect(seen.errors).toEqual([])                         // 45 s in, still catching up: no give-up
    expect(sock.closed).toBe(false)
    sock.frame({ type: 'progress', audio_s: 25.5 })
    expect(seen.notices).toEqual([DICTATION_SLOW, null])
  })

  it('gives up when the server\'s audio stops advancing for thirty seconds, frames or not', async () => {
    const { sock, seen, ctx } = await openSession()
    emitFrame(ctx)
    for (let t = 0; t < 32; t++) {
      sock.frame({ type: 'progress', audio_s: 0.17 })      // the downlink lives, the upload died
      vi.advanceTimersByTime(1000)
    }
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(sock.closed).toBe(true)
  })

  it('a held server error goes out at the bound when its last words never come', async () => {
    const { sock, seen } = await openSession()
    sock.frame({ type: 'error', code: 'max_seconds', message: 'Dictation reached its time limit and stopped.' })
    expect(seen.errors).toEqual([])
    vi.advanceTimersByTime(STOP_FINAL_WAIT_MS + 100)       // the link died after the error frame
    expect(seen.errors).toEqual(['Dictation reached its time limit and stopped.'])
    expect(seen.notices).toEqual([DICTATION_FINISHING, null])
    expect(sock.closed).toBe(true)
  })

  it('a refused session mint says an admin can check the speech settings', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, headers: new Headers(), json: async () => ({}) })))
    await expect(platformStt.create({ language: 'el' }).start()).rejects.toThrow(DICTATION_NO_START_ADMIN)
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503, headers: new Headers(), json: async () => ({}) })))
    await expect(platformStt.create({ language: 'el' }).start()).rejects.toThrow(DICTATION_NO_START)
  })
})


describe('the remainders', () => {
  it('says the time limit is near once, for a while, the slow notice first', async () => {
    const { sock, seen } = await openSession()
    sock.frame({ type: 'progress', audio_s: 1, left_s: 40 })
    expect(seen.notices).toEqual([])
    sock.frame({ type: 'progress', audio_s: 3, left_s: 19.5 })
    expect(seen.notices).toEqual([DICTATION_CAP_SOON])
    sock.frame({ type: 'progress', audio_s: 5, left_s: 17.5 })
    expect(seen.notices).toEqual([DICTATION_CAP_SOON])          // once
    for (let t = 0; t < CAP_NOTICE_MS / 1000 + 1; t++) {        // a healthy link meanwhile
      vi.advanceTimersByTime(1000)
      sock.frame({ type: 'progress', audio_s: 6 + t, left_s: 16 - t })
    }
    expect(seen.notices).toEqual([DICTATION_CAP_SOON, null])
  })

  it('a slow link while the warning shows takes the row, the warning comes back after it', async () => {
    const { sock, seen } = await openSession()
    sock.frame({ type: 'progress', audio_s: 1, left_s: 19 })
    vi.advanceTimersByTime(SLOW_AFTER_MS + 1000)               // nothing from the server
    expect(seen.notices).toEqual([DICTATION_CAP_SOON, DICTATION_SLOW])
    sock.frame({ type: 'progress', audio_s: 2, left_s: 18 })     // caught up, still inside the warning
    expect(seen.notices[seen.notices.length - 1]).toBe(DICTATION_CAP_SOON)
  })

  it('a warning that came due while the link was slow shows once the link recovers', async () => {
    const { sock, seen } = await openSession()
    vi.advanceTimersByTime(SLOW_AFTER_MS + 1000)               // nothing from the server: slow
    expect(seen.notices).toEqual([DICTATION_SLOW])
    sock.frame({ type: 'progress', audio_s: 1, left_s: 19 })    // due while slow; the frame clears the slowness
    expect(seen.notices).toEqual([DICTATION_SLOW, DICTATION_CAP_SOON])
  })

  it('a slow link for the whole warning still gets its eight seconds afterwards', async () => {
    const { sock, seen, ctx } = await openSession()
    for (let i = 0; i < 120; i++) emitFrame(ctx)               // about 10 s sent
    vi.advanceTimersByTime(1000)                                // the watch: the server trails far behind
    sock.frame({ type: 'progress', audio_s: 1, left_s: 19 })    // due while slow
    expect(seen.notices).toEqual([DICTATION_SLOW])
    vi.advanceTimersByTime(CAP_NOTICE_MS + 2000)                // the warning's eight seconds pass, still slow
    sock.frame({ type: 'progress', audio_s: 10.5, left_s: 9 })  // caught up
    expect(seen.notices).toEqual([DICTATION_SLOW, DICTATION_CAP_SOON])
  })

  it('a stop whose backlog passes the cap says the last words were lost and closes', async () => {
    const { session, sock, seen } = await openSession()
    Object.assign(sock, { bufferedAmount: 32000 * 40 })         // forty seconds still to send
    const stopped = session.stop()
    vi.advanceTimersByTime(STOP_FINAL_WAIT_MS + STOP_BACKLOG_MAX_MS + 1)
    await stopped
    expect(seen.errors).toEqual([DICTATION_LOST])
    expect(sock.closed).toBe(true)
  })

  it('a stop on a slow uplink waits longer by the audio still queued', async () => {
    const { session, sock, seen } = await openSession()
    Object.assign(sock, { bufferedAmount: 32000 * 6 })          // six seconds still to send
    const stopped = session.stop()
    vi.advanceTimersByTime(STOP_FINAL_WAIT_MS + 3000)
    await Promise.resolve()
    expect(sock.closed).toBe(false)                             // past the plain bound, still waiting
    sock.frame({ type: 'final', text: 'τέλος', last: true })
    await stopped
    expect(seen.finals).toEqual(['τέλος'])
    expect(seen.errors).toEqual([])
  })
})
