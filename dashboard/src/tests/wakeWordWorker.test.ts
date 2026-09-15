import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = '/kws-assets/1.13.5-gigaspeech-3.3M-r2/'

// Scripted fake spotter: `script` is what getResult returns across
// successive decodes ('' = nothing yet); isReady stays true while anything
// is scripted, so a feed drains the script in one decode loop.
function fakeEngine(script: string[] = [], opts: { failCreateAt?: number } = {}) {
  const streams: { acceptWaveform: ReturnType<typeof vi.fn>; free: ReturnType<typeof vi.fn> }[] = []
  const queue = [...script]
  const kws = {
    createStream: vi.fn(() => {
      if (opts.failCreateAt !== undefined && streams.length === opts.failCreateAt) throw new Error('no memory')
      const s = { acceptWaveform: vi.fn(), free: vi.fn() }
      streams.push(s)
      return s
    }),
    isReady: vi.fn(() => queue.length > 0),
    decode: vi.fn(),
    getResult: vi.fn(() => ({ keyword: queue.shift() ?? '', start_time: 0, timestamps: [0.12, 0.4] })),
    reset: vi.fn(),
    free: vi.fn(),
  }
  return { kws, streams, queue }
}

// The worker is a classic (importScripts-style) script, so it is evaluated
// with its globals supplied: what it posts, imports and asks of the engine
// is observable without a real Worker. `self.Module` is what the worker
// hands the emscripten glue; the test drives `onRuntimeInitialized` itself.
function loadWorker(engine = fakeEngine()) {
  // vitest runs from the dashboard root (the config's root).
  const src = readFileSync(resolve(process.cwd(), 'public', 'wake-word-worker.js'), 'utf8')
  const scope: {
    onmessage?: (ev: { data: unknown }) => void
    Module?: { onRuntimeInitialized: () => void }
  } = {}
  const postMessage = vi.fn()
  const importScripts = vi.fn()
  const close = vi.fn()
  const createKws = vi.fn(() => engine.kws)
  const setInterval = vi.fn(() => 7)
  const clearInterval = vi.fn()
  new Function('self', 'postMessage', 'importScripts', 'close', 'createKws', 'setInterval', 'clearInterval', src)(
    scope, postMessage, importScripts, close, createKws, setInterval, clearInterval,
  )
  const send = (data: unknown) => scope.onmessage!({ data })
  const init = (extra: Record<string, unknown> = {}) =>
    send({ type: 'init', base: BASE, keywords: '▁HE Y ▁A @alpha', threshold: 0.3, score: 1, ...extra })
  const ready = () => scope.Module!.onRuntimeInitialized()
  const frames = (n = 1365) => send({ type: 'frames', samples: new Float32Array(n) })
  const posted = (type: string) =>
    postMessage.mock.calls.map((c) => c[0] as { type: string; [k: string]: unknown }).filter((m) => m.type === type)
  return { scope, postMessage, importScripts, close, createKws, setInterval, clearInterval, send, init, ready, frames, posted, engine }
}

describe('wake-word worker asset base', () => {
  it('imports the engine only from the same-origin /kws-assets/<version>/ folder', () => {
    const { init, importScripts, posted } = loadWorker()
    init()
    expect(importScripts).toHaveBeenCalledWith(
      `${BASE}sherpa-onnx-kws.js`,
      `${BASE}sherpa-onnx-wasm-kws-main.js`,
    )
    expect(posted('error')).toHaveLength(0)
  })

  it.each([
    'https://evil.example/kws-assets/1.13.5/',
    '//evil.example/kws-assets/1.13.5/',
    '/kws-assets/../worker/',
    '/kws-assets/1.13.5',
    'kws-assets/1.13.5/',
    undefined,
  ])('refuses %s and imports nothing', (base) => {
    const { send, importScripts, postMessage } = loadWorker()
    send({ type: 'init', base })
    expect(importScripts).not.toHaveBeenCalled()
    expect(postMessage).toHaveBeenCalledWith({ type: 'error', message: 'invalid asset base' })
  })
})

describe('wake-word worker stream discipline', () => {
  it('buffers audio during boot, drains it on ready, and replaces the stream after a detection', () => {
    const w = loadWorker(fakeEngine(['', 'alpha']))
    w.init()
    w.frames() // spoken during the boot → buffered, not fed
    expect(w.engine.streams).toHaveLength(0)
    w.ready()
    expect(w.posted('ready')).toHaveLength(1)
    // The spotter is created with the measured beam (2026-09-11: 8, not
    // upstream's 4 — accented names need the wider search) and the
    // server's keywords/threshold.
    const cfg = (w.createKws.mock.calls[0] as unknown as [unknown, { maxActivePaths: number; keywordsThreshold: number; keywords: string }])[1]
    expect(cfg.maxActivePaths).toBe(8)
    expect(cfg.keywordsThreshold).toBe(0.3)
    expect(cfg.keywords).toBe('▁HE Y ▁A @alpha')
    // The buffered frame was fed to the first stream and the scripted
    // decodes ran '' then 'alpha'.
    expect(w.engine.streams[0].acceptWaveform).toHaveBeenCalledTimes(1)
    const detects = w.posted('detect')
    expect(detects).toHaveLength(1)
    expect(detects[0].keyword).toBe('alpha')
    expect(detects[0].at_s).toBeCloseTo(1365 / 16000, 3)
    // Upstream rule "reset after detection", in fresh-stream form: the old
    // stream is freed, a new one created, kws.reset never used.
    expect(w.engine.streams[0].free).toHaveBeenCalledTimes(1)
    expect(w.engine.kws.createStream).toHaveBeenCalledTimes(2)
    expect(w.engine.kws.reset).not.toHaveBeenCalled()
    // No warm-up: nothing was fed to the fresh stream.
    expect(w.engine.streams[1].acceptWaveform).not.toHaveBeenCalled()
  })

  it('a reset before the engine is ready drops the buffer and touches nothing', () => {
    const w = loadWorker(fakeEngine())
    w.init()
    w.frames()
    w.send({ type: 'reset' })
    w.ready()
    expect(w.engine.streams[0].acceptWaveform).not.toHaveBeenCalled()
    expect(w.engine.kws.createStream).toHaveBeenCalledTimes(1)
    expect(w.posted('error')).toHaveLength(0)
  })

  it('a reset after ready replaces the stream (capture resumed after a pause)', () => {
    const w = loadWorker(fakeEngine())
    w.init()
    w.ready()
    w.send({ type: 'reset' })
    expect(w.engine.streams[0].free).toHaveBeenCalledTimes(1)
    expect(w.engine.kws.createStream).toHaveBeenCalledTimes(2)
    expect(w.engine.kws.reset).not.toHaveBeenCalled()
  })

  it('a failing stream replacement reports an error instead of going silently deaf', () => {
    const w = loadWorker(fakeEngine(['alpha'], { failCreateAt: 1 }))
    w.init()
    w.ready()
    w.frames()
    expect(w.posted('detect')).toHaveLength(1)
    expect(w.posted('error')).toHaveLength(1)
    expect(String(w.posted('error')[0].message)).toContain('no memory')
  })

  it('posts no diag breadcrumbs unless asked, and the listed ones when asked', () => {
    const quiet = loadWorker(fakeEngine(['alpha']))
    quiet.init()
    quiet.ready()
    quiet.frames()
    expect(quiet.posted('diag')).toHaveLength(0)

    const loud = loadWorker(fakeEngine(['alpha']))
    loud.init({ diag: true })
    loud.ready()
    loud.frames()
    const events = loud.posted('diag').map((m) => m.event)
    expect(events).toEqual(['engine ready', 'detected', 'stream recreated'])
    // The heartbeat is wall-clock, so a stalled feed still shows a flat counter.
    expect(loud.setInterval).toHaveBeenCalledWith(expect.any(Function), 5000)
    loud.send({ type: 'diag', on: false })
    loud.send({ type: 'reset' })
    expect(loud.posted('diag')).toHaveLength(3)
  })

  it('stop frees the stream and the spotter, clears the heartbeat and closes', () => {
    const w = loadWorker(fakeEngine())
    w.init()
    w.ready()
    w.send({ type: 'stop' })
    expect(w.engine.streams[0].free).toHaveBeenCalledTimes(1)
    expect(w.engine.kws.free).toHaveBeenCalledTimes(1)
    expect(w.clearInterval).toHaveBeenCalledWith(7)
    expect(w.close).toHaveBeenCalledTimes(1)
  })
})
