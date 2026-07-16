// Platform STT — capture the mic, downsample to 16 kHz PCM, and stream it over
// /ws/audio/stt to the server STT provider. Used when the browser has no native
// Web Speech STT (Firefox/Safari) or the policy forces platform.
//
// The token is minted via POST /v1/audio/stt/session (cookie-authed) and sent in
// the first WS frame — never in the URL. Mic capture uses a
// ScriptProcessorNode: deprecated but supported everywhere, and avoids shipping a
// separate AudioWorklet module (AudioWorklet is a future upgrade).

import { apiFetch } from '../../api/auth'
import { type STTBackend, type STTSession, type STTCreateOptions } from '../types'

const TARGET_RATE = 16000

function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/audio/stt`
}

function downsampleToPCM16(input: Float32Array, srcRate: number, dstRate: number): ArrayBuffer {
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
    // recorder" that doubled every dictated sentence, live repro 2026-07-16).
    let stopping = false
    const handlers = {
      partial: (_: string) => {},
      final: (_: string) => {},
      error: (_: Error) => {},
      end: () => {},
    }

    const teardown = () => {
      try { node?.disconnect() } catch { /* ignore */ }
      try { source?.disconnect() } catch { /* ignore */ }
      try { ctx?.close() } catch { /* ignore */ }
      stream?.getTracks().forEach(t => t.stop())
      node = source = null; ctx = null; stream = null
    }

    return {
      async start() {
        // 1. Mint the short-lived token.
        const sessRes = await apiFetch('/v1/audio/stt/session', {
          method: 'POST',
          body: JSON.stringify({ provider_id: opts.providerId ?? null }),
        })
        if (stopping) return // stopped during the mint — nothing was opened
        if (!sessRes.ok) throw new Error(`Could not start STT session (${sessRes.status})`)
        const { ws_token } = await sessRes.json()
        if (stopping) return

        // 2. Mic.
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
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
            sock.send(JSON.stringify({
              type: 'init', token: ws_token, language: opts.language,
              sample_rate: TARGET_RATE, encoding: 'pcm_s16le',
            }))
          }
          sock.onerror = () => reject(new Error('STT socket error'))
          sock.onmessage = (ev) => {
            let msg: { type?: string; text?: string; message?: string }
            try { msg = JSON.parse(ev.data) } catch { return }
            if (msg.type === 'ready') {
              resolve()
            } else if (msg.type === 'final' && msg.text) {
              handlers.final(msg.text)
            } else if (msg.type === 'interim' && msg.text) {
              handlers.partial(msg.text)
            } else if (msg.type === 'error') {
              handlers.error(new Error(msg.message || 'STT error'))
            }
          }
          // Rejecting after resolve is inert — this also settles the await
          // when a mid-connect stop() closes the socket, instead of hanging.
          sock.onclose = () => { teardown(); handlers.end(); reject(new Error('STT socket closed')) }
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
        }
        source.connect(node)
        node.connect(ctx.destination)
      },

      async stop() {
        stopping = true
        const sock = ws
        ws = null
        teardown() // release the mic immediately — the flush below is receive-only
        if (sock && sock.readyState === WebSocket.OPEN) {
          try { sock.send(JSON.stringify({ type: 'stop' })) } catch { /* ignore */ }
          // The server flushes the buffered tail as one last {"final"} before
          // closing. WAIT for it (bounded) instead of closing instantly —
          // close() raced that frame, and whatever the user said since the
          // last VAD commit was silently dropped (the truncated live-mode
          // sends, 2026-07-16). The property onmessage handler still runs
          // first, so the final reaches h.final before we proceed.
          await new Promise<void>((resolve) => {
            const done = () => { clearTimeout(timer); resolve() }
            const timer = setTimeout(done, 1800)
            sock.addEventListener('message', (ev) => {
              try { if (JSON.parse((ev as MessageEvent).data).type === 'final') done() } catch { /* not JSON */ }
            })
            sock.addEventListener('close', done)
          })
        }
        try { sock?.close() } catch { /* ignore */ }
      },

      onPartial(h) { handlers.partial = h },
      onFinal(h) { handlers.final = h },
      onError(h) { handlers.error = h },
      onEnd(h) { handlers.end = h },
    }
  },
}
