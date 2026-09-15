// @vitest-environment node
//
// The COMMITTED wake-word engine bundle, run for real (the emscripten glue
// evaluated in this process, the model from the .data package). Pins the
// behaviour the 2026-09-11 rebuild fixed: sherpa-onnx's automatic reset
// after 1.5 s of trailing silence used to wipe the encoder states, and a
// phrase whose onset landed 80–160 ms before that reset was lost. On the
// previous bundle this test FAILS at the 1.44 s offset; on the patched
// one every offset detects. Also pins that the control phrase routes to
// its own agent tag and never to a platform-word line.
//
// Runs in ~3 s here; the timeout leaves room for a slow CI runner.

import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const VERSION_DIR = '1.13.5-gigaspeech-3.3M-r2'
const ASSETS = path.resolve(here, '../../../proxy/assets/kws', VERSION_DIR)
const CLIPS = path.resolve(here, 'fixtures/wake')
const RATE = 16000
const FRAME = 1365 // what the page feeds at a 48 kHz context (4096 / 3)

// Production shape: one agent line plus the platform variants with their
// riders — but tagged apart so a fire can be attributed to a line family.
const KEYWORDS = [
  '▁HE Y ▁PERSON AL ▁AS S IST ANT @agent',
  '▁HE Y ▁O T O ▁DO CK :2 #0.2 @platform',
  '▁HE Y ▁O T T O ▁DO CK :2 #0.2 @platform',
  '▁HE Y ▁A U T O D O CK :2 #0.2 @platform',
  '▁HE Y ▁A U T O ▁DO CK :2 #0.2 @platform',
].join('\n')

interface Stream { acceptWaveform(rate: number, s: Float32Array): void; free(): void }
interface Kws {
  createStream(): Stream
  isReady(s: Stream): boolean
  decode(s: Stream): void
  getResult(s: Stream): { keyword: string }
  free(): void
}

function readWav(file: string): Float32Array {
  const b = readFileSync(file)
  let off = 12
  let data: Buffer | null = null
  while (off + 8 <= b.length) {
    const id = b.toString('ascii', off, off + 4)
    const size = b.readUInt32LE(off + 4)
    if (id === 'data') { data = b.subarray(off + 8, off + 8 + size); break }
    off += 8 + size + (size & 1)
  }
  if (!data) throw new Error(`no data chunk in ${file}`)
  const out = new Float32Array(data.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = data.readInt16LE(i * 2) / 32768
  return out
}

// Deterministic mic-floor noise (an LCG, not Math.random): the offsets
// under test must not wobble between runs.
function noise(seconds: number, amp: number, seed = 12345): Float32Array {
  const out = new Float32Array(Math.round(seconds * RATE))
  let x = seed >>> 0
  for (let i = 0; i < out.length; i++) {
    x = (Math.imul(x, 1664525) + 1013904223) >>> 0
    out[i] = ((x / 4294967296) * 2 - 1) * amp
  }
  return out
}

function loadEngine(): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const g = globalThis as Record<string, unknown>
    g.Module = {
      locateFile: (f: string) => path.join(ASSETS, f),
      print: () => {},
      printErr: () => {},
      onAbort: (why: unknown) => reject(new Error(`wasm aborted: ${String(why)}`)),
      onRuntimeInitialized: () => resolve(g.Module as Record<string, unknown>),
    }
    g.require = createRequire(import.meta.url)
    g.__dirname = ASSETS
    g.__filename = path.join(ASSETS, 'sherpa-onnx-wasm-kws-main.js')
    vm.runInThisContext(readFileSync(g.__filename as string, 'utf8'), { filename: g.__filename as string })
  })
}

// Feed like the worker does: frame by frame, decode while ready, stop at
// the first keyword. Returns the tags fired, in order.
function run(kws: Kws, parts: Float32Array[]): string[] {
  const stream = kws.createStream()
  const fired: string[] = []
  try {
    for (const part of parts) {
      for (let i = 0; i < part.length; i += FRAME) {
        stream.acceptWaveform(RATE, part.subarray(i, Math.min(i + FRAME, part.length)))
        while (kws.isReady(stream)) {
          kws.decode(stream)
          const r = kws.getResult(stream)
          if (r.keyword && r.keyword.length > 0) { fired.push(r.keyword); return fired }
        }
      }
    }
  } finally {
    stream.free()
  }
  return fired
}

describe('wake-word engine bundle (real wasm)', () => {
  let kws: Kws
  beforeAll(async () => {
    const Module = await loadEngine()
    const { createKws } = (globalThis as { require: NodeJS.Require }).require(path.join(ASSETS, 'sherpa-onnx-kws.js'))
    kws = createKws(Module, {
      featConfig: { samplingRate: RATE, featureDim: 80 },
      modelConfig: {
        transducer: {
          encoder: './encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
          decoder: './decoder-epoch-12-avg-2-chunk-16-left-64.onnx',
          joiner: './joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
        },
        tokens: './tokens.txt', provider: 'cpu', modelType: '', numThreads: 1, debug: 0, modelingUnit: '', bpeVocab: '',
      },
      maxActivePaths: 4, numTrailingBlanks: 1, keywordsScore: 1.0, keywordsThreshold: 0.30, keywords: KEYWORDS,
    })
  }, 60_000)
  afterAll(() => { kws?.free() })

  it('the listener names the same bundle directory this test pins', () => {
    const hook = readFileSync(path.resolve(here, '../hooks/useWakeWord.ts'), 'utf8')
    expect(hook).toContain(`'/kws-assets/${VERSION_DIR}/'`)
  })

  it('recalls the platform word at every onset offset around the old dead window', () => {
    const clip = readWav(path.join(CLIPS, 'hey-otodock.wav'))
    const tail = noise(1.5, 1e-3, 777)
    const misses: number[] = []
    for (let pre = 1.28; pre <= 1.8401; pre += 0.08) {
      const fired = run(kws, [noise(pre, 1e-3), clip, tail])
      if (fired[0] !== 'platform') misses.push(Math.round(pre * 100) / 100)
    }
    expect(misses).toEqual([])
  }, 60_000)

  it('routes the control phrase to its agent line and never to a platform line', () => {
    const clip = readWav(path.join(CLIPS, 'hey-personal-assistant.wav'))
    const fired = run(kws, [noise(0.5, 1e-3), clip, noise(1.5, 1e-3, 99)])
    expect(fired).toEqual(['agent'])
  }, 60_000)
})
