// Platform TTS — server-synthesized speech played through the shared Web Audio
// player. Two paths, both 24 kHz s16le mono:
//   • play()        — one-shot replay of a complete message (SoundIcon):
//                     POST /v1/audio/tts/synthesize, stream PCM, play it.
//   • createStream() — incremental voice-mode sink: POST /v1/audio/tts/session +
//                     WS /ws/audio/tts, push sentences as the reply generates.
// stop() aborts the one-shot fetch (so the server stops Cartesia) and cuts
// playback; the stream's cancel() does the same for the WS.

import { apiFetch } from '../../api/auth'
import { type TTSBackend, type TTSPlayOptions, type TtsStream } from '../types'
import { createPcmPlayer, webAudioAvailable, type PcmPlayer } from '../webaudioPlayer'

// The server's CHAT_AUDIO_TARGET_RATE. The one-shot path also reads it from the
// X-Audio-Sample-Rate response header; the WS path has no header, so it's fixed.
const CHAT_RATE = 24000

function ttsWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/audio/tts`
}

// ── Incremental streaming sink (voice mode) ───────────────────────
function createPlatformStream(opts: TTSPlayOptions): TtsStream {
  let ws: WebSocket | null = null
  let player: PcmPlayer | null = null
  let ready = false
  let finishRequested = false
  let cancelled = false
  let ending = false        // {ended} received → draining; don't let onclose cut it
  let settled = false
  const pending: string[] = []
  let resolveDone!: () => void
  const done = new Promise<void>(r => { resolveDone = r })

  const settle = () => { if (!settled) { settled = true; resolveDone() } }
  const teardown = () => {
    try { ws?.close() } catch { /* ignore */ }
    ws = null
    player?.stop()
    player = null
  }
  const sendCtl = (obj: object) => { try { ws?.send(JSON.stringify(obj)) } catch { /* ignore */ } }

  void (async () => {
    try {
      const sess = await apiFetch('/v1/audio/tts/session', {
        method: 'POST', body: JSON.stringify({ provider_id: opts.providerId ?? null }),
      })
      if (!sess.ok) throw new Error(`tts session ${sess.status}`)
      const { ws_token } = await sess.json()
      if (cancelled) return
      const sock = new WebSocket(ttsWsUrl())
      sock.binaryType = 'arraybuffer'
      ws = sock
      sock.onopen = () => sock.send(JSON.stringify({
        type: 'init', token: ws_token, language: opts.language, voice_id: opts.voiceId,
      }))
      sock.onmessage = (ev) => {
        if (typeof ev.data !== 'string') {
          if (!cancelled && player) player.enqueue(new Uint8Array(ev.data as ArrayBuffer))
          return
        }
        let msg: { type?: string }
        try { msg = JSON.parse(ev.data) } catch { return }
        if (msg.type === 'ready') {
          ready = true
          player = createPcmPlayer(CHAT_RATE)
          for (const t of pending) sendCtl({ type: 'text', text: t })
          pending.length = 0
          if (finishRequested) sendCtl({ type: 'done' })
        } else if (msg.type === 'ended') {
          ending = true
          void (async () => { try { await player?.drained() } finally { teardown(); settle() } })()
        } else if (msg.type === 'error') {
          teardown(); settle()
        }
      }
      sock.onerror = () => { teardown(); settle() }
      sock.onclose = () => { if (!ending) { teardown(); settle() } }
    } catch {
      teardown(); settle()
    }
  })()

  return {
    push(text: string) {
      if (cancelled || finishRequested || !text) return
      if (ready) sendCtl({ type: 'text', text })
      else pending.push(text)
    },
    finish() {
      if (cancelled || finishRequested) return
      finishRequested = true
      if (ready) sendCtl({ type: 'done' })   // else the ready handler sends it
    },
    cancel() {
      if (cancelled) return
      cancelled = true
      sendCtl({ type: 'cancel' })
      teardown(); settle()
    },
    done,
  }
}

// ── One-shot replay (SoundIcon) ───────────────────────────────────
let controller: AbortController | null = null
let activePlayer: PcmPlayer | null = null

export const platformTts: TTSBackend = {
  kind: 'platform',

  isAvailable() {
    return webAudioAvailable()
  },

  async play(text: string, opts: TTSPlayOptions) {
    this.stop()
    controller = new AbortController()

    const res = await apiFetch('/v1/audio/tts/synthesize', {
      method: 'POST',
      body: JSON.stringify({
        text, language: opts.language, voice_id: opts.voiceId, provider_id: opts.providerId ?? null,
      }),
      signal: controller.signal,
    })
    if (!res.ok || !res.body) throw new Error(`TTS failed (${res.status})`)
    const rate = Number(res.headers.get('X-Audio-Sample-Rate')) || CHAT_RATE

    const player = createPcmPlayer(rate)
    activePlayer = player
    const reader = res.body.getReader()
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (value.length) player.enqueue(value)
      }
    } catch (e) {
      if ((e as { name?: string })?.name === 'AbortError') return  // stop() was called
      throw e
    }
    // Resolve only after the audio finishes (so the icon flips ⏹ → ▶), and only
    // if this playback is still the active one (not superseded/stopped).
    if (activePlayer === player) await player.drained()
    if (activePlayer === player) { player.stop(); activePlayer = null }
  },

  stop() {
    try { controller?.abort() } catch { /* ignore */ }
    controller = null
    activePlayer?.stop()
    activePlayer = null
  },

  createStream(opts: TTSPlayOptions) {
    return createPlatformStream(opts)
  },
}
