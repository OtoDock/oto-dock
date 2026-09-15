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
//   in:  {type:'init', base, keywords, threshold, score, diag?}
//        {type:'frames', samples: Float32Array}   (transferred)
//        {type:'reset'}        capture resumed after a pause
//        {type:'diag', on}     toggle diagnostic breadcrumbs at runtime
//        {type:'stop'}
//   out: {type:'ready'}
//        {type:'detect', keyword, at_s, engine:{start_time, timestamps}}
//        {type:'diag', event, data}   only while diag is on
//        {type:'error', message}
//
// `keyword` is the @tag from the keywords line — the target agent slug.
// `at_s` is this worker's own sample clock (seconds of audio fed to the
// current stream): a diagnostics recording aligns to it, whereas the
// engine's timestamps restart at each of its internal resets and its
// start_time is always 0.
//
// Stream discipline (measured 2026-09-11 against the real engine with the
// replay harness): the encoder needs NO warm-up — a cold stream recalls
// every test phrase — so after a detection and on capture resume the
// stream is simply REPLACED by a fresh one. That drops the decoder's
// partial context and any undecoded pre-pause residue, and it honours the
// upstream "reset right after a detection" rule in superset form. The
// engine's own silence-timer reset is patched at build time to keep the
// encoder states (scripts/build-wasm-kws.sh); nothing here depends on that
// patch except recall.

/* eslint-disable no-undef */
'use strict'

var Module // shared global the emscripten glue picks up (var, not let)
var kws = null
var stream = null
var pending = []
var pendingSamples = 0
var overflowLogged = false
var diag = false
var fed = 0 // samples fed to the CURRENT stream
var decodes = 0
var heartbeat = 0

// Pre-ready buffer cap by DURATION (frame size varies with the page's
// AudioContext rate — a 16 kHz context would make a frame-count cap 3x
// longer): ~15 s covers a real cold engine boot on a slow phone.
var PENDING_MAX_SAMPLES = 15 * 16000
var HEARTBEAT_MS = 5000

function fail(message) {
  postMessage({ type: 'error', message: String(message) })
}

function diagPost(event, data) {
  if (diag) postMessage({ type: 'diag', event: event, data: data || {} })
}

// Replace the stream. `stream` is nulled BEFORE freeing so a throw in
// createStream leaves the worker with no stream (frames buffer, the page
// gets the error and rebuilds the worker) instead of a dangling freed
// handle that the next feed would hand back to the wasm.
function freshStream(why) {
  var old = stream
  stream = null
  try { if (old) old.free() } catch (e) { /* a dead handle must not block the recreate */ }
  stream = kws.createStream()
  fed = 0
  diagPost('stream recreated', { why: why })
}

// Engine assets are same-origin only: the page names the versioned
// /kws-assets/<version>/ folder and nothing else may be imported into this
// worker, so the base is rebuilt from the fixed prefix + the version
// segment rather than used as sent.
function assetBase(raw) {
  const m = /^\/kws-assets\/([A-Za-z0-9._-]+)\/$/.exec(String(raw))
  return m ? '/kws-assets/' + m[1] + '/' : null
}

function init(msg) {
  const base = assetBase(msg.base) // e.g. '/kws-assets/1.13.5-gigaspeech-3.3M-r2/'
  if (!base) {
    fail('invalid asset base')
    return
  }
  diag = msg.diag === true
  const t0 = Date.now()
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
          // Beam of 8, not upstream's 4 (2026-09-11, measured on an
          // operator's real recordings replayed offline): with the same
          // per-line riders, a non-native "hey personal assistant" went
          // from 14 % to 30 % recall and "hey otodock" from 13/21 to 21/21
          // alignments, zero false fires on control speech, decode time
          // unchanged (the encoder dominates). 16 buys nothing more.
          maxActivePaths: 8,
          numTrailingBlanks: 1,
          // Positive-clamp, not ||/??: a zero threshold would fire on
          // everything (the server never sends one, but this fallback is
          // the last line of defence). Constants match the server seeds.
          keywordsScore: msg.score > 0 ? msg.score : 1.0,
          keywordsThreshold: msg.threshold > 0 ? msg.threshold : 0.30,
          keywords: msg.keywords,
        })
        stream = kws.createStream()
        fed = 0
        const queued = pending
        pending = []
        pendingSamples = 0
        heartbeat = setInterval(function () {
          diagPost('heartbeat', { decodes: decodes, fed_s: Math.round(fed / 160) / 100 })
        }, HEARTBEAT_MS)
        postMessage({ type: 'ready' })
        diagPost('engine ready', { boot_ms: Date.now() - t0, buffered_s: Math.round(queued.reduce((n, s) => n + s.length, 0) / 160) / 100 })
        // The buffer may hold the very utterance the user is waiting on:
        // catch-up decode runs ~20x realtime.
        queued.forEach(feed)
      } catch (e) {
        fail(e)
      }
    },
  }
  // Also reachable as a global-scope property (what the glue reads; the
  // top-level `var` above is that same binding in a real worker) so a
  // test that evaluates this script in a function scope can reach it.
  self.Module = Module
  try {
    importScripts(base + 'sherpa-onnx-kws.js', base + 'sherpa-onnx-wasm-kws-main.js')
  } catch (e) {
    fail(e)
  }
}

function feed(samples) {
  if (!kws || !stream) {
    // Engine still booting: buffer ~15 s of audio so a wake phrase spoken
    // DURING the boot is decoded the moment the spotter is up — "say it
    // twice" on first use was this.
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
    fed += samples.length
    var detected = null
    while (kws.isReady(stream)) {
      kws.decode(stream)
      decodes++
      var r = kws.getResult(stream)
      if (r.keyword && r.keyword.length > 0) {
        detected = r
        break
      }
    }
    if (detected) {
      var at = Math.round(fed / 16) / 1000
      postMessage({
        type: 'detect',
        keyword: detected.keyword,
        at_s: at,
        engine: { start_time: detected.start_time, timestamps: detected.timestamps },
      })
      diagPost('detected', { keyword: detected.keyword, at_s: at, timestamps: detected.timestamps })
      // Upstream rule: reset immediately after a detection (prevents
      // duplicate triggers from the surviving beam). The rest of this
      // frame after the keyword (< 90 ms) is dropped with the old stream.
      freshStream('detection')
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
    // drop buffered pre-pause audio and start a fresh stream so the
    // decoder's pre-pause partial context cannot stitch onto new speech.
    pending = []
    pendingSamples = 0
    try {
      if (kws && stream) freshStream('capture resume')
    } catch (e) {
      fail(e)
    }
  }
  else if (msg.type === 'diag') {
    diag = msg.on === true
  }
  else if (msg.type === 'stop') {
    clearInterval(heartbeat)
    try { if (stream) stream.free() } catch { /* ignore */ }
    try { if (kws) kws.free() } catch { /* ignore */ }
    stream = null
    kws = null
    close()
  }
}
