// useWakeWord — the app-level "hey <agent>" listener.
//
// Mounted once at the authenticated root (RequireAuth, beside useFcmPush) so
// it runs on EVERY dashboard page. Fully on-device: the mic feeds a Web
// Worker running the wasm keyword spotter; audio never leaves the browser —
// the network is touched only AFTER a detection, when the wake navigates to
// the agent and the normal duplex attach flow takes over.
//
// Gate chain (every link required before the mic opens):
//   authed && user opt-in (audio-prefs wake_word_enabled, default OFF)
//   && duplex available (platform-global capability)
//   && compiled keywords non-empty && browser support && secure context
//   && no other mic owner (micCoordinator — duplex/dictation always win).
//
// Wake action (locked): earcon → navigate('/chat/<slug>?wake=1') — AgentChat
// consumes the param and arms its existing never-warmed duplex seam (new
// chat, never resume). Detection pauses for a cooldown so one utterance
// can't double-fire.
//
// Dev harnesses: `?wakeDebug=<slug>` on any page triggers the full wake
// action without mic/worker/secure-context — the live-verify path for
// plain-HTTP installs where getUserMedia does not exist. `?wakeDiag=1` (or
// the settings button) switches the diagnostics recorder on
// (audio/wakeDiag.ts): the last 30 s of what the spotter hears plus an
// event log, saved locally on request.

import { useEffect, useRef, useSyncExternalStore } from 'react'
import { useLocation, type NavigateFunction } from 'react-router-dom'
import { apiFetch } from '../api/auth'
import { useChatAudioCapability } from './useChatAudioCapability'
import { useMyAudioPrefs } from '../api/userAudio'
import { useWakeKeywords } from '../api/wakeWord'
import { useNotificationSound } from './useNotificationSound'
import { lastReleasedMicOwner, micBusy, subscribeMic } from '../audio/micCoordinator'
import { ensureNativeMicPermission } from '../audio/micPermission'
import { isNativePlatform } from '../audio/types'
import {
  isWakeDiagOn, setWakeDiag, subscribeWakeDiag, wakeDiagContext, wakeDiagEvent,
  wakeDiagFrames, wakeDiagListening,
} from '../audio/wakeDiag'

// The directory name is the immutable-cache buster: a rebuilt engine ships
// in a new directory (the -r2 suffix is the revision of the engine patch
// scripts/build-wasm-kws.sh applies), and the previous directory stays
// served through the release so an already-open dashboard keeps working.
export const KWS_ASSETS_BASE = '/kws-assets/1.13.5-gigaspeech-3.3M-r2/'

const CAPTURE_BUF = 4096
const TARGET_RATE = 16000
const WAKE_COOLDOWN_MS = 3000
// Resume grace after another owner released the mic. Short by default (a
// long deaf window was the "say it twice after a conversation" bug); native
// dictation keeps the old 1s — its stop() is an async bridge + ~600ms
// finalize window and the device mic frees well after releaseMic fires.
const RESUME_AFTER_BUSY_MS = 300
const RESUME_AFTER_NATIVE_DICTATION_MS = 1000
// A worker that dies (an engine asset fetch failing during boot, a wasm
// abort) is rebuilt a bounded number of times — a transient failure used
// to leave the wake deaf for the whole session. The budget refills once a
// worker has been up for a minute, so one hiccup a day never exhausts it.
const WORKER_REBUILD_DELAYS_MS = [5_000, 15_000, 45_000]
const WORKER_STABLE_MS = 60_000

function browserSupported(): boolean {
  return (
    typeof Worker !== 'undefined' &&
    typeof WebAssembly !== 'undefined' &&
    typeof AudioContext !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia
  )
}

// Plain averaging decimator (the KWS reference pattern): context rate in,
// 16 kHz mono Float32 out. Precision beyond this buys nothing for a spotter.
function downsampleFloat(input: Float32Array, inRate: number): Float32Array {
  if (inRate === TARGET_RATE) return input.slice()
  const ratio = inRate / TARGET_RATE
  const outLen = Math.floor(input.length / ratio)
  const out = new Float32Array(outLen)
  let pos = 0
  for (let i = 0; i < outLen; i++) {
    const next = Math.round((i + 1) * ratio)
    let sum = 0
    let count = 0
    for (; pos < next && pos < input.length; pos++) { sum += input[pos]; count++ }
    out[i] = count ? sum / count : 0
  }
  return out
}

function trackSettings(stream: MediaStream): Record<string, unknown> {
  try {
    const s = stream.getAudioTracks()[0]?.getSettings?.() as Record<string, unknown> | undefined
    if (!s) return {}
    const keep = ['sampleRate', 'channelCount', 'echoCancellation', 'noiseSuppression', 'autoGainControl', 'latency', 'deviceId']
    const out: Record<string, unknown> = {}
    for (const k of keep) if (k in s) out[k] = s[k]
    return out
  } catch {
    return {}
  }
}

export function useWakeWord(navigate: NavigateFunction, authed: boolean) {
  const location = useLocation()
  // Gated on auth: this hook mounts under RequireAuth BEFORE login renders,
  // and unauthenticated fetches here 401 → apiFetch's expired-session
  // redirect → the login page reload-loops (live-hit 2026-08-24).
  const { data: prefs } = useMyAudioPrefs(authed)
  const { data: capability } = useChatAudioCapability(authed)
  const optedIn = authed && prefs?.wake_word_enabled === true
  const duplexOk = capability?.duplex?.available === true
  const { data: kw } = useWakeKeywords(optedIn && duplexOk)
  const { playPing } = useNotificationSound()
  const busy = useSyncExternalStore(subscribeMic, micBusy, micBusy)
  const diag = useSyncExternalStore(subscribeWakeDiag, isWakeDiagOn, () => false)
  const diagRef = useRef(diag)
  diagRef.current = diag

  const navigateRef = useRef(navigate)
  navigateRef.current = navigate
  const playRef = useRef(playPing)
  playRef.current = playPing
  const lastWakeRef = useRef(0)

  const wake = (slug: string) => {
    const now = Date.now()
    if (now - lastWakeRef.current < WAKE_COOLDOWN_MS) {
      console.log(`[wake] cooldown — swallowed detection for "${slug}"`)
      wakeDiagEvent('cooldown swallowed', { slug })
      return
    }
    lastWakeRef.current = now
    playRef.current()
    wakeDiagEvent('wake', { slug })
    // Fire-and-forget CLI pre-warm: the earcon + SPA navigation + chat-WS
    // connect take ~0.5-1.5s before the normal warmup can spawn — this buys
    // that window (the warmup claims the pre-warmed session by key; a miss
    // is just reaped server-side). MUST NOT delay the navigate.
    void apiFetch('/v1/duplex/prewarm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent: slug }),
    }).catch(() => { /* best-effort */ })
    navigateRef.current(`/chat/${slug}?wake=1`)
  }
  const wakeRef = useRef(wake)
  wakeRef.current = wake

  // Dev harnesses: synthetic wake (no mic involved) and the recorder switch.
  useEffect(() => {
    if (!authed) return
    const params = new URLSearchParams(location.search)
    if (params.get('wakeDiag') === '1') setWakeDiag(true)
    const slug = params.get('wakeDebug')
    if (slug) wakeRef.current(slug)
  }, [location.search, authed])

  // Engine vs capture are SEPARATE lifecycles: the wasm spotter boots in
  // seconds on a phone, so it persists across mic pauses (duplex/dictation)
  // instead of rebooting per wake — the pause only releases the microphone.
  const engineOn =
    optedIn && duplexOk && kw?.enabled === true && !!kw.keywords &&
    browserSupported()
  const capturing = engineOn && !busy && window.isSecureContext

  const keywords = kw?.keywords ?? ''
  const threshold = kw?.threshold ?? 0.30

  const workerRef = useRef<Worker | null>(null)
  useEffect(() => {
    if (!engineOn) return
    let cancelled = false
    let attempt = 0
    let rebuildTimer = 0
    wakeDiagContext({ keywords, threshold, base: KWS_ASSETS_BASE })

    const spawn = () => {
      const t0 = Date.now()
      let readyAt = 0
      const worker = new Worker('/wake-word-worker.js')
      workerRef.current = worker
      const drop = (why: string) => {
        if (workerRef.current === worker) workerRef.current = null
        try { worker.terminate() } catch { /* ignore */ }
        if (cancelled) return
        if (readyAt && Date.now() - readyAt > WORKER_STABLE_MS) attempt = 0
        if (attempt >= WORKER_REBUILD_DELAYS_MS.length) {
          console.warn(`[wake] engine ${why} — giving up after ${attempt} rebuilds`)
          wakeDiagEvent('engine gone', { why })
          return
        }
        const delay = WORKER_REBUILD_DELAYS_MS[attempt++]
        console.log(`[wake] engine ${why} — rebuilding in ${delay / 1000}s`)
        wakeDiagEvent('engine rebuild scheduled', { why, delay_ms: delay, attempt })
        rebuildTimer = window.setTimeout(() => { if (!cancelled) spawn() }, delay)
      }
      worker.onmessage = (ev) => {
        // A replaced worker (rebuild, dev double mount) must not speak.
        if (workerRef.current !== worker) return
        const msg = ev.data
        if (msg?.type === 'ready') {
          readyAt = Date.now()
          console.log(`[wake] engine ready in ${readyAt - t0}ms`)
          wakeDiagEvent('engine ready', { boot_ms: readyAt - t0 })
        } else if (msg?.type === 'detect' && typeof msg.keyword === 'string') {
          console.log(`[wake] detected "${msg.keyword}" — navigating`)
          wakeDiagEvent('detected', { keyword: msg.keyword, at_s: msg.at_s, engine: msg.engine })
          wakeRef.current(msg.keyword)
        } else if (msg?.type === 'diag') {
          wakeDiagEvent(String(msg.event), msg.data)
        } else if (msg?.type === 'error') {
          console.warn('[wake] worker error:', msg.message)
          drop(`error: ${msg.message}`)
        }
      }
      worker.onerror = (e) => {
        if (workerRef.current !== worker) return
        console.warn('[wake] worker failed:', e.message)
        drop(`failed: ${e.message}`)
      }
      worker.postMessage({
        type: 'init', base: KWS_ASSETS_BASE, keywords, threshold, score: 1.0, diag: diagRef.current,
      })
    }
    spawn()
    return () => {
      cancelled = true
      window.clearTimeout(rebuildTimer)
      const worker = workerRef.current
      workerRef.current = null
      try { worker?.postMessage({ type: 'stop' }) } catch { /* ignore */ }
      try { worker?.terminate() } catch { /* ignore */ }
    }
  }, [engineOn, keywords, threshold])

  // The recorder toggles the worker's breadcrumbs at runtime (no reboot).
  useEffect(() => {
    try { workerRef.current?.postMessage({ type: 'diag', on: diag }) } catch { /* ignore */ }
  }, [diag])

  // The resume grace applies ONLY after another owner released the mic.
  // Armed as a marker on the busy edge; the capture effect maps it to the
  // right duration at consume time (the releasing owner is only known then).
  const resumePendingRef = useRef(false)
  useEffect(() => {
    if (busy) resumePendingRef.current = true
  }, [busy])

  useEffect(() => {
    if (!capturing) return
    let cancelled = false
    let stream: MediaStream | null = null
    let ctx: AudioContext | null = null
    let source: MediaStreamAudioSourceNode | null = null
    let node: ScriptProcessorNode | null = null
    // Staleness guard for watchdog-driven restarts: two in-flight start()s
    // (visibilitychange + statechange back-to-back is the normal Android
    // resume sequence) must not leak the older one's stream.
    let run = 0
    let recoverTimer = 0
    let retryTimer = 0
    let retried = false
    let gumFailed = false

    const stopCapture = () => {
      // Null onstatechange BEFORE close(): our own close fires a
      // statechange ('closed' !== 'running') that would otherwise arm the
      // watchdog against the context we just intentionally killed —
      // an infinite stop/start loop.
      if (ctx) { ctx.onstatechange = null; wakeDiagEvent('capture stopped') }
      window.clearTimeout(recoverTimer)
      try { node?.disconnect(); source?.disconnect() } catch { /* ignore */ }
      try { void ctx?.close() } catch { /* ignore */ }
      stream?.getTracks().forEach((t) => t.stop())
      node = null; source = null; ctx = null; stream = null
      wakeDiagListening(false)
    }

    // Suspension watchdog: Android can suspend the AudioContext
    // (backgrounding, audio-focus loss) without anything in `capturing`
    // changing — onaudioprocess just stops and the listener is silently
    // deaf. Try resume; if the context is still not running shortly after,
    // do a full capture cycle. A failed getUserMedia (mic race) is also
    // retried here on the next foreground.
    const recover = (why: string) => {
      if (cancelled) return
      if (gumFailed && !ctx && !stream) {
        gumFailed = false
        console.log(`[wake] retrying failed capture (${why})`)
        wakeDiagEvent('capture retry', { why })
        void start()
        return
      }
      const c = ctx
      if (!c || c.state === 'running') return
      console.log(`[wake] ctx ${c.state} (${why}) — resuming`)
      wakeDiagEvent('ctx not running', { state: c.state, why })
      void c.resume().catch(() => { /* verified below */ })
      window.clearTimeout(recoverTimer)
      recoverTimer = window.setTimeout(() => {
        if (cancelled || !ctx || ctx.state === 'running') return
        console.log('[wake] resume failed — full capture cycle')
        wakeDiagEvent('capture cycle', { why: 'resume failed' })
        stopCapture()
        void start()
      }, 500)
    }

    const start = async () => {
      const myRun = ++run
      try {
        if (isNativePlatform()) await ensureNativeMicPermission()
        if (cancelled || myRun !== run) return
        // NS/AGC OFF on purpose (deliberate divergence from duplex's
        // full triple): their convergence on a fresh stream mangles the
        // first ~200ms — exactly the discriminative "Hey" — which was the
        // "say it twice" bug. AEC stays ON as the self-wake guard while
        // chat-audio replay speaks through the speakers. (First launch on
        // native: micPermission's {audio:true} primer may run once just
        // before this open — permission grant only, then cached.)
        const s = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: false },
        })
        if (cancelled || myRun !== run) { s.getTracks().forEach((t) => t.stop()); return }
        stream = s
        ctx = new AudioContext()
        ctx.onstatechange = () => recover('statechange')
        await ctx.resume()
        if (cancelled || myRun !== run) return
        source = ctx.createMediaStreamSource(stream)
        node = ctx.createScriptProcessor(CAPTURE_BUF, 1, 1)
        const rate = ctx.sampleRate
        // Drop any half-decoded audio from before the pause — a stale
        // context must not stitch onto fresh speech.
        workerRef.current?.postMessage({ type: 'reset' })
        wakeDiagEvent('reset sent')
        // Frame-cadence watch for the recorder: a gap of more than two
        // buffers means the main thread stalled and audio was dropped.
        const frameMs = (CAPTURE_BUF / rate) * 1000
        let lastFrameAt = 0
        node.onaudioprocess = (e) => {
          const now = performance.now()
          if (lastFrameAt && now - lastFrameAt > frameMs * 2) {
            wakeDiagEvent('frame gap', { ms: Math.round(now - lastFrameAt) })
          }
          lastFrameAt = now
          const samples = downsampleFloat(e.inputBuffer.getChannelData(0), rate)
          // Before the transfer below detaches the buffer.
          wakeDiagFrames(samples)
          const w = workerRef.current
          if (!w) return
          try {
            w.postMessage({ type: 'frames', samples }, [samples.buffer])
          } catch { /* worker gone mid-frame */ }
        }
        source.connect(node)
        node.connect(ctx.destination)
        gumFailed = false
        retried = false
        console.log('[wake] listening')
        wakeDiagEvent('listening', { context_rate: rate, ctx_state: ctx.state, ...trackSettings(stream) })
        wakeDiagListening(true)
      } catch (e) {
        if (cancelled || myRun !== run) return
        console.warn('[wake] listener unavailable:', e)
        wakeDiagEvent('listener unavailable', { error: e instanceof Error ? e.message : String(e) })
        stopCapture()
        gumFailed = true
        if (!retried) {
          // One bounded retry — mic-release races (native STT teardown)
          // resolve within a second; beyond that the watchdog retries on
          // the next foreground event.
          retried = true
          retryTimer = window.setTimeout(() => {
            if (!cancelled) void start()
          }, 1000)
        }
      }
    }

    const onVisible = () => {
      if (document.visibilityState === 'visible') recover('visible')
    }
    document.addEventListener('visibilitychange', onVisible)
    // Android MainActivity dispatches this on onResume — visibilitychange
    // is unreliable for short screen off/on cycles (useDashboardWs
    // precedent).
    window.addEventListener('otodock:force-health-check', onVisible)

    const delay = resumePendingRef.current
      ? (isNativePlatform() && lastReleasedMicOwner() === 'dictation'
        ? RESUME_AFTER_NATIVE_DICTATION_MS
        : RESUME_AFTER_BUSY_MS)
      : 0
    resumePendingRef.current = false
    const startTimer = window.setTimeout(() => { void start() }, delay)

    return () => {
      cancelled = true
      window.clearTimeout(startTimer)
      window.clearTimeout(retryTimer)
      window.clearTimeout(recoverTimer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('otodock:force-health-check', onVisible)
      stopCapture()
    }
  }, [capturing])
}
