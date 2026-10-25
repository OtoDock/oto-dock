// Platform STT — capture the mic, downsample to 16 kHz PCM, and stream it over
// /ws/audio/stt to the server STT provider. Used when the browser has no native
// Web Speech STT (Firefox/Safari) or the policy forces platform.
//
// The token is minted via POST /v1/audio/stt/session (cookie-authed) and sent in
// the first WS frame — never in the URL. Mic capture uses a
// ScriptProcessorNode: deprecated but supported everywhere, and avoids shipping a
// separate AudioWorklet module (AudioWorklet is a future upgrade).
//
// A weak link: where the server's `ready` says so it sends `progress` every
// few seconds (the audio it received), and the session watches it while the
// mic pumps. Nothing from the server for a while, or the server's audio far
// behind what was sent, shows a notice; nothing at all, or audio that stops
// arriving, for long enough ends the dictation with the lost-connection
// error. A socket that closes on its own says the same, never a silent stop.

import { apiFetch } from '../../api/auth'
import { browserSttLang } from '../lang'
import { type STTBackend, type STTSession, type STTCreateOptions } from '../types'
import {
  DICTATION_FINISHING, DICTATION_LOST, DICTATION_NO_CONNECT, DICTATION_NO_START, DICTATION_NO_START_ADMIN,
  DICTATION_SLOW, DICTATION_STOPPED, DICTATION_CAP_SOON,
} from './dictationWords'

const TARGET_RATE = 16000
// The server's stop can wait for a language switch still being decided,
// for the new stream to catch up and for a window the vendor answered
// without words to be listened to again (8 s at most) before its last
// final, then closes the socket: the bound only matters when the server
// never answers.
export const STOP_FINAL_WAIT_MS = 10000
// The link: slow after this long without a server frame (or with the
// server's audio this far behind what was sent); lost after this long with
// nothing from the server, or with the server's audio not advancing (a
// trailing upload that still advances is only slow).
export const SLOW_AFTER_MS = 5000
// The time limit, ahead of it: the server's progress frames carry the
// seconds left (`left_s`, every language stream counted); at CAP_WARN_S or
// less the notice row says so once, for CAP_NOTICE_MS (a slow link's notice
// wins meanwhile).
export const CAP_WARN_S = 20
export const CAP_NOTICE_MS = 8000
// A stop on a slow uplink: the stop frame queues behind the audio still in
// the socket, so the wait for the last words grows by that audio, capped.
export const STOP_BACKLOG_MAX_MS = 20000
export const LOST_AFTER_MS = 30000
export const BEHIND_S = 5
const WATCH_EVERY_MS = 1000

export type StopOutcome = 'final' | 'close' | 'timeout'
const ANSWERED: StopOutcome = 'final'
const CLOSED: StopOutcome = 'close'
const TIMED_OUT: StopOutcome = 'timeout'
// The server's frame saying how much audio it received (where `ready`
// announces it).
const PROGRESS_FRAME = 'progress'

// Resolves at the first `final` frame (with `marked`, the final marked
// `last`: a window released late can arrive as a final before it), the
// socket's close, or the bound.
export function waitForStopFinal(
  sock: Pick<WebSocket, 'addEventListener'>, ms = STOP_FINAL_WAIT_MS, marked = false,
): Promise<StopOutcome> {
  return new Promise<StopOutcome>((resolve) => {
    let settled = false
    const done = (outcome: StopOutcome) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(outcome)
    }
    const timer = setTimeout(() => done(TIMED_OUT), ms)
    sock.addEventListener('message', (ev) => {
      try {
        const msg = JSON.parse((ev as MessageEvent).data)
        if (msg.type === 'final' && (!marked || msg.last === true)) done(ANSWERED)
      } catch { /* not JSON */ }
    })
    sock.addEventListener('close', () => done(CLOSED))
  })
}

function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/audio/stt`
}

export function downsampleToPCM16(input: Float32Array, srcRate: number, dstRate: number): ArrayBuffer {
  const ratio = srcRate / dstRate
  const outLen = Math.floor(input.length / ratio)
  const out = new Int16Array(outLen)
  for (let i = 0; i < outLen; i++) {
    const s = Math.max(-1, Math.min(1, input[Math.floor(i * ratio)]))
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
  }
  return out.buffer
}

export const platformStt: STTBackend = {
  kind: 'platform',

  isAvailable() {
    return typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia
  },

  create(opts: STTCreateOptions): STTSession {
    let ws: WebSocket | null = null
    let stream: MediaStream | null = null
    let ctx: AudioContext | null = null
    let node: ScriptProcessorNode | null = null
    let source: MediaStreamAudioSourceNode | null = null
    // stop() arrived while start() was still in flight. stop()'s teardown is a
    // no-op at that point (nothing created yet), so start() must abort ITSELF
    // at its next checkpoint — otherwise it opens the mic and reaches ready
    // as a background session the UI no longer shows (the "invisible second
    // recorder" that doubled every dictated sentence).
    let stopping = false
    // The link, once the server is ready: what `ready` announced, what was
    // sent and heard, and how this session ended when it was not the person.
    let ready = false
    let progressEvery = 0
    let marksLast = false
    let sentBytes = 0
    let serverAudioS = 0
    let lastHeardAt = 0
    let lastAdvanceAt = 0
    let slowSince = 0
    let capUntil = 0
    let capOwed = false
    let capTimer: ReturnType<typeof setTimeout> | null = null
    let serverError = false
    // The server's error waits for the words it still sends (its final
    // marked last, or the close): shown at once, a tap on the mic would
    // start a new dictation the old one's last words then land in twice.
    let pendingError: string | null = null
    let finishBound: ReturnType<typeof setTimeout> | null = null
    let gaveUp = false
    let watch: ReturnType<typeof setInterval> | null = null
    const handlers = {
      partial: (_: string) => {},
      final: (_: string) => {},
      replace: (_: string) => {},
      error: (_: Error) => {},
      end: () => {},
      notice: (_: string | null) => {},
      finishing: () => {},
    }

    // The held server error goes out with the words it waited for, or at
    // the bound when they never come (a link that died after the error).
    const sayPending = () => {
      if (finishBound) { clearTimeout(finishBound); finishBound = null }
      if (pendingError === null) return
      const said = pendingError
      pendingError = null
      handlers.notice(null)
      handlers.error(new Error(said))
    }

    const onVisible = () => {
      // A hidden page's timers are throttled: its clock starts again when
      // it is shown, so a phone put away for a moment is not a lost link.
      if (!document.hidden) { lastHeardAt = lastAdvanceAt = Date.now(); setSlow(false) }
    }

    const stopWatch = () => {
      if (watch) { clearInterval(watch); watch = null }
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisible)
    }

    const teardown = () => {
      stopWatch()
      if (capTimer) { clearTimeout(capTimer); capTimer = null }
      try { node?.disconnect() } catch { /* ignore */ }
      try { source?.disconnect() } catch { /* ignore */ }
      try { ctx?.close() } catch { /* ignore */ }
      stream?.getTracks().forEach(t => t.stop())
      node = source = null; ctx = null; stream = null
    }

    // The notice row: the slow link first, then the time limit's warning
    // while it lasts.
    const sayNotice = () => {
      handlers.notice(slowSince ? DICTATION_SLOW : Date.now() < capUntil ? DICTATION_CAP_SOON : null)
    }

    // The time limit's warning shows once for CAP_NOTICE_MS, its clock
    // starting when the row is free: a link slow when it came due shows it
    // once it recovers.
    const showCap = (): boolean => {
      if (!capOwed || capUntil || slowSince || stopping || serverError || gaveUp) return false
      capUntil = Date.now() + CAP_NOTICE_MS
      capTimer = setTimeout(() => { capTimer = null; if (!stopping && !serverError && !gaveUp) sayNotice() },
        CAP_NOTICE_MS)
      return true
    }

    const setSlow = (slow: boolean) => {
      if (slow && !slowSince) { slowSince = Date.now(); sayNotice() }
      else if (!slow && slowSince) { slowSince = 0; showCap(); sayNotice() }
    }

    const warnCap = (left: number) => {
      if (left > CAP_WARN_S || capOwed || stopping || serverError) return
      capOwed = true
      if (showCap()) sayNotice()
    }

    const giveUp = (sock: WebSocket) => {
      gaveUp = true
      teardown()
      handlers.notice(null)
      try { sock.close() } catch { /* ignore */ }
      handlers.error(new Error(DICTATION_LOST))
    }

    const startWatch = (sock: WebSocket) => {
      if (progressEvery <= 0 || typeof document === 'undefined') return
      lastHeardAt = lastAdvanceAt = Date.now()
      document.addEventListener('visibilitychange', onVisible)
      watch = setInterval(() => {
        if (document.hidden || stopping) return
        const now = Date.now()
        const behind = sentBytes / (TARGET_RATE * 2) - serverAudioS
        setSlow(now - lastHeardAt > SLOW_AFTER_MS || behind > BEHIND_S)
        if (now - lastHeardAt > LOST_AFTER_MS || now - lastAdvanceAt > LOST_AFTER_MS) giveUp(sock)
      }, WATCH_EVERY_MS)
    }

    return {
      async start() {
        // 1. Mint the short-lived token.
        const sessRes = await apiFetch('/v1/audio/stt/session', {
          method: 'POST',
          body: JSON.stringify({ provider_id: opts.providerId ?? null }),
        })
        if (stopping) return // stopped during the mint — nothing was opened
        if (!sessRes.ok) {
          // Refused (the policy, the provider's setup): only an admin can
          // change that; anything else may pass on a retry.
          const refused = sessRes.status === 401 || sessRes.status === 403
          throw new Error(refused ? DICTATION_NO_START_ADMIN : DICTATION_NO_START)
        }
        const { ws_token } = await sessRes.json()
        if (stopping) return

        // 2. Mic. echoCancellation OFF, deliberately: Chrome/Android flips
        // the whole app into MODE_IN_COMMUNICATION (in-call volume slider,
        // quiet call-stream output) whenever a capture opens with the
        // hardware AEC attached — and dictation normally has nothing
        // playing to cancel. NS/AGC stay ON (they never trigger the mode
        // flip, and AGC keeps quiet speakers recognizable). Playback that
        // could overlap dictation is interlocked in useSpeechSession
        // (SoundIcon flush + chime suppression).
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: false,
            noiseSuppression: true,
            autoGainControl: true,
          },
        })
        if (stopping) { teardown(); return } // stopped while the mic opened
        ctx = new AudioContext()
        // Autoplay policy can leave a fresh AudioContext "suspended"; the mic tap
        // is a user gesture, so resume it so audioprocess actually fires.
        await ctx.resume().catch(() => {})
        if (stopping) { teardown(); return }
        source = ctx.createMediaStreamSource(stream)
        node = ctx.createScriptProcessor(4096, 1, 1)

        // 3. WebSocket.
        ws = new WebSocket(wsUrl())
        ws.binaryType = 'arraybuffer'
        const sock = ws
        await new Promise<void>((resolve, reject) => {
          sock.onopen = () => {
            // On the Automatic pick the engine starts in the browser's language
            // when the install lists it (else the list's first) and reads the
            // speaker's from the audio.
            sock.send(JSON.stringify({
              type: 'init', token: ws_token, language: opts.language,
              default_language: browserSttLang(),
              sample_rate: TARGET_RATE, encoding: 'pcm_s16le',
            }))
          }
          sock.onerror = () => reject(new Error(DICTATION_NO_CONNECT))
          sock.onmessage = (ev) => {
            let msg: { type?: string; text?: string; message?: string; audio_s?: number; left_s?: number
              progress_every_s?: number; marks_last?: boolean; last?: boolean }
            try { msg = JSON.parse(ev.data) } catch { return }
            lastHeardAt = Date.now()
            if (msg.type === 'ready') {
              ready = true
              progressEvery = typeof msg.progress_every_s === 'number' ? msg.progress_every_s : 0
              marksLast = msg.marks_last === true
              resolve()
            } else if (msg.type === PROGRESS_FRAME) {
              if (typeof msg.audio_s === 'number') {
                if (msg.audio_s > serverAudioS) lastAdvanceAt = lastHeardAt
                serverAudioS = msg.audio_s
              }
              if (typeof msg.left_s === 'number') warnCap(msg.left_s)
            } else if (msg.type === 'final') {
              if (msg.text) handlers.final(msg.text)
              if (msg.last === true) sayPending()
            } else if (msg.type === 'interim' && msg.text) {
              handlers.partial(msg.text)
            } else if (msg.type === 'replace') {
              handlers.replace(typeof msg.text === 'string' ? msg.text : '')
            } else if (msg.type === 'error') {
              // The server ended the dictation (its time limit, a speech
              // service that stopped answering): the mic stops at once, the
              // socket stays open for the words it still sends, the error
              // shows with them (an older server sends no marked final:
              // its close shows it).
              serverError = true
              teardown()
              const said = msg.message || DICTATION_STOPPED
              if (ready && marksLast) {
                pendingError = said
                handlers.notice(DICTATION_FINISHING)
                handlers.finishing()
                finishBound = setTimeout(() => { sayPending(); try { sock.close() } catch { /* ignore */ } },
                  STOP_FINAL_WAIT_MS)
              } else {
                handlers.notice(null)
                handlers.error(new Error(said))
              }
            }
            if (slowSince && sentBytes / (TARGET_RATE * 2) - serverAudioS <= BEHIND_S) setSlow(false)
          }
          // Rejecting after resolve is inert — this also settles the await
          // when a mid-connect stop() closes the socket, instead of hanging.
          sock.onclose = () => {
            const unasked = ready && !stopping && !serverError && !gaveUp
            teardown()
            sayPending()
            if (unasked) { handlers.notice(null); handlers.error(new Error(DICTATION_LOST)) }
            handlers.end()
            reject(new Error(DICTATION_NO_CONNECT))
          }
        })
        if (stopping) { // stopped while connecting — never start pumping
          try { sock.close() } catch { /* ignore */ }
          teardown()
          return
        }

        // 4. Pump PCM once the server is ready.
        node.onaudioprocess = (e) => {
          if (sock.readyState !== WebSocket.OPEN || !ctx) return
          const pcm = downsampleToPCM16(e.inputBuffer.getChannelData(0), ctx.sampleRate, TARGET_RATE)
          sock.send(pcm)
          sentBytes += pcm.byteLength
        }
        source.connect(node)
        node.connect(ctx.destination)
        startWatch(sock)
      },

      async stop() {
        stopping = true
        const sock = ws
        ws = null
        teardown() // release the mic immediately — the flush below is receive-only
        handlers.notice(null)
        if (sock && sock.readyState === WebSocket.OPEN) {
          try { sock.send(JSON.stringify({ type: 'stop' })) } catch { /* ignore */ }
          // The server flushes the buffered tail as one last {"final"} before
          // closing. WAIT for it (bounded) instead of closing instantly —
          // close() raced that frame, and whatever the user said since the
          // last VAD commit was silently dropped. The property onmessage
          // handler still runs first, so the final reaches h.final before we
          // proceed.
          const backlogMs = Math.min(STOP_BACKLOG_MAX_MS,
            Math.round(((sock.bufferedAmount || 0) / (TARGET_RATE * 2)) * 1000))
          const outcome = await waitForStopFinal(sock, STOP_FINAL_WAIT_MS + backlogMs, marksLast)
          // The last words did not arrive (a server that marks its stop
          // answer never sent it): say so. An older server marks nothing and
          // is not watched: nothing to tell.
          if (marksLast && outcome !== ANSWERED && !serverError) handlers.error(new Error(DICTATION_LOST))
        }
        try { sock?.close() } catch { /* ignore */ }
      },

      onPartial(h) { handlers.partial = h },
      onFinal(h) { handlers.final = h },
      onReplace(h) { handlers.replace = h },
      onError(h) { handlers.error = h },
      onEnd(h) { handlers.end = h },
      onNotice(h) { handlers.notice = h },
      onFinishing(h) { handlers.finishing = h },
    }
  },
}
