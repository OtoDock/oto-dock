// Wake-word detection worker — hosts the sherpa-onnx KWS wasm engine.
//
// Classic worker on purpose: the emscripten glue is an importScripts-style
// script (its `var Module = typeof Module != "undefined" ? Module : {}`
// only sees a shared-global Module, never a module import). All engine
// assets load from /kws-assets/<version>/ (same-origin, immutable-cached);
// audio arrives as 16 kHz mono Float32Array frames from the page and NEVER
// leaves this worker — detection is fully on-device.
//
// Protocol:
//   in:  {type:'init', base, keywords, threshold, score}
//        {type:'frames', samples: Float32Array}   (transferred)
//        {type:'stop'}
//   out: {type:'ready'} | {type:'detect', keyword} | {type:'error', message}
//
// `keyword` is the @tag from the keywords line — the target agent slug.

/* eslint-disable no-undef */
'use strict'

var Module // shared global the emscripten glue picks up (var, not let)
var kws = null
var stream = null
var pending = []
var pendingSamples = 0
var overflowLogged = false

// Pre-ready buffer cap by DURATION (frame size varies with the page's
// AudioContext rate — a 16 kHz context would make a frame-count cap 3x
// longer): ~15 s covers a real cold engine boot on a slow phone.
var PENDING_MAX_SAMPLES = 15 * 16000

// Encoder warm-up material: the zipformer runs chunk-16-left-64 — 64 left
// frames x 40 ms = ~2.56 s of trained left context. After every stream
// reset the encoder starts from init states (~cold recall until the cache
// refills), so we pre-roll ~2.6 s of near-silence. Low-amplitude noise, not
// digital zeros: real microphone silence always has a noise floor, exact
// zeros hit the fbank's epsilon clamp (out-of-distribution features).
var WARMUP = null
function warmupBuf() {
  if (!WARMUP) {
    WARMUP = new Float32Array(Math.round(16000 * 2.6))
    for (var i = 0; i < WARMUP.length; i++) WARMUP[i] = (Math.random() * 2 - 1) * 1e-4
  }
  return WARMUP
}

// Rebuild encoder left context after a reset. The drain deliberately never
// posts detections: reset does NOT flush already-accepted feature frames,
// so residue audio from before the reset (e.g. duplex speech) is decoded
// first — if it latched a keyword, swallow it here instead of firing a
// phantom wake, and hard-reset (accepting a cold encoder on this ~never
// branch).
function warmStream() {
  if (!kws || !stream) return
  try {
    stream.acceptWaveform(16000, warmupBuf())
    while (kws.isReady(stream)) kws.decode(stream)
    var r = kws.getResult(stream)
    if (r.keyword && r.keyword.length > 0) {
      kws.reset(stream)
      console.log('[wake] warmup swallowed stale keyword')
    }
  } catch (e) {
    fail(e)
  }
}

function fail(message) {
  postMessage({ type: 'error', message: String(message) })
}

function init(msg) {
  const base = msg.base // e.g. '/kws-assets/1.13.5-gigaspeech-3.3M/'
  Module = {
    locateFile: (f) => base + f,
    print: () => {},
    printErr: () => {},
    onAbort: (why) => fail('wasm aborted: ' + why),
    onRuntimeInitialized: () => {
      try {
        // createKws comes from sherpa-onnx-kws.js (same worker global scope)
        kws = createKws(Module, {
          featConfig: { samplingRate: 16000, featureDim: 80 },
          modelConfig: {
            transducer: {
              encoder: './encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
              decoder: './decoder-epoch-12-avg-2-chunk-16-left-64.onnx',
              joiner: './joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
            },
            tokens: './tokens.txt',
            provider: 'cpu',
            modelType: '',
            numThreads: 1,
            debug: 0,
            modelingUnit: '',
            bpeVocab: '',
          },
          maxActivePaths: 4,
          numTrailingBlanks: 1,
          // Positive-clamp, not ||/??: a zero threshold would fire on
          // everything (the server never sends one, but this fallback is
          // the last line of defence). Constants match the server seeds.
          keywordsScore: msg.score > 0 ? msg.score : 1.0,
          keywordsThreshold: msg.threshold > 0 ? msg.threshold : 0.30,
          keywords: msg.keywords,
        })
        stream = kws.createStream()
        // Warm BEFORE draining the pre-ready buffer: a fresh stream has
        // zero left context, and the buffer may hold the very utterance
        // the user is waiting on.
        warmStream()
        const queued = pending
        pending = []
        pendingSamples = 0
        postMessage({ type: 'ready' })
        queued.forEach(feed)
      } catch (e) {
        fail(e)
      }
    },
  }
  try {
    importScripts(base + 'sherpa-onnx-kws.js', base + 'sherpa-onnx-wasm-kws-main.js')
  } catch (e) {
    fail(e)
  }
}

function feed(samples) {
  if (!kws || !stream) {
    // Engine still booting: buffer ~15 s of audio so a wake phrase spoken
    // DURING the boot is decoded the moment the spotter is up (catch-up
    // decode runs ~20x realtime) — "say it twice" on first use was this.
    pending.push(samples)
    pendingSamples += samples.length
    while (pendingSamples > PENDING_MAX_SAMPLES && pending.length > 1) {
      pendingSamples -= pending.shift().length
      if (!overflowLogged) {
        overflowLogged = true
        console.log('[wake] pre-ready buffer overflowed — dropping oldest audio')
      }
    }
    return
  }
  try {
    stream.acceptWaveform(16000, samples)
    var detected = null
    while (kws.isReady(stream)) {
      kws.decode(stream)
      var r = kws.getResult(stream)
      if (r.keyword && r.keyword.length > 0) {
        // Upstream rule: reset immediately after a detection (prevents
        // duplicate triggers from the surviving beam). Warm-up runs AFTER
        // the loop — never inside it (the warm-up audio itself re-arms
        // isReady and would spin the loop through getResult).
        detected = r.keyword
        kws.reset(stream)
        break
      }
    }
    if (detected) {
      postMessage({ type: 'detect', keyword: detected })
      warmStream()
    }
  } catch (e) {
    fail(e)
  }
}

self.onmessage = (ev) => {
  const msg = ev.data
  if (!msg) return
  if (msg.type === 'init') init(msg)
  else if (msg.type === 'frames') feed(msg.samples)
  else if (msg.type === 'reset') {
    // Capture resumed after a pause (the engine outlives mic pauses):
    // drop buffered pre-pause audio and the decoder's partial context,
    // then rebuild encoder left context so the first fresh utterance
    // isn't decoded cold.
    pending = []
    pendingSamples = 0
    try {
      if (kws && stream) {
        kws.reset(stream)
        warmStream()
      }
    } catch { /* ignore */ }
  }
  else if (msg.type === 'stop') {
    try { if (stream) stream.free() } catch { /* ignore */ }
    try { if (kws) kws.free() } catch { /* ignore */ }
    stream = null
    kws = null
    close()
  }
}
