// Platform TTS — server-synthesized speech played through the shared Web Audio
// player, 24 kHz s16le mono: play() replays a complete message (SoundIcon):
// POST /v1/audio/tts/synthesize, stream PCM, play it. stop() aborts the fetch
// (so the server stops the provider) and cuts playback.

import { apiFetch } from '../../api/auth'
import { type TTSBackend, type TTSPlayOptions } from '../types'
import { createPcmPlayer, webAudioAvailable, type PcmPlayer } from '../webaudioPlayer'

// The server's CHAT_AUDIO_TARGET_RATE, when the X-Audio-Sample-Rate response
// header does not say it.
const CHAT_RATE = 24000

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
    let broken = false
    let failure: unknown
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        if (value.length) player.enqueue(value)
      }
    } catch (e) {
      if ((e as { name?: string })?.name === 'AbortError') return  // stop() was called
      broken = true
      failure = e
    }
    // Resolve only after the audio finishes (so the icon flips ⏹ → ▶), and only
    // if this playback is still the active one (not superseded/stopped). A
    // stream that broke plays what came and stops the same way, so its
    // player never outlives it.
    if (activePlayer === player) await player.drained()
    if (activePlayer === player) { player.stop(); activePlayer = null }
    if (broken) throw failure
  },

  stop() {
    try { controller?.abort() } catch { /* ignore */ }
    controller = null
    activePlayer?.stop()
    activePlayer = null
  },
}
