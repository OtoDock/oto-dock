// Gapless PCM playback over the Web Audio API — schedule raw 16-bit signed LE
// mono chunks back-to-back as they arrive, for low time-to-first-audio. Shared
// by the one-shot platform-TTS replay (SoundIcon) and the streaming voice-mode
// sink. Whole int16 samples are carried across chunk boundaries (an odd trailing
// byte joins the next chunk).

type ACtor = typeof AudioContext

function audioContextCtor(): ACtor | null {
  const w = window as unknown as { AudioContext?: ACtor; webkitAudioContext?: ACtor }
  return w.AudioContext || w.webkitAudioContext || null
}

export function webAudioAvailable(): boolean {
  return typeof window !== 'undefined' && audioContextCtor() !== null
}

export interface PcmPlayer {
  /** Schedule one PCM chunk (s16le mono) to play after the previously queued audio. */
  enqueue(bytes: Uint8Array): void
  /** Stop immediately: halt all scheduled nodes and close the context. */
  stop(): void
  /** Suspend playback in place (barge-in pause): the context clock freezes,
   *  scheduled audio is retained, resume() continues exactly where it froze.
   *  Lossless — nothing is discarded. */
  pause(): void
  /** Undo pause(). No-op when not paused or already stopped. */
  resume(): void
  /** Resolves once every scheduled chunk has finished playing (or stop() ran). */
  drained(): Promise<void>
  /** Level (0..1) of what is audible RIGHT NOW — an AnalyserNode tap on the
   *  output path, read on demand (the presence halo polls it per frame).
   *  0 after stop(). */
  getLevel(): number
}

export function createPcmPlayer(
  sampleRate: number, leadS = 0,
  // Fires on each REAL underrun (a mid-audio gap, not a segment boundary) —
  // the duplex hook forwards these up its socket so a phone test's audio
  // hiccups land in the proxy log without needing device console access.
  onUnderrun?: (gapMs: number, afterAudioS: number) => void,
): PcmPlayer {
  const AC = audioContextCtor()
  if (!AC) throw new Error('Web Audio not available')
  const ctx = new AC()
  // A fresh AudioContext can start "suspended"; the user gesture that began
  // playback (icon/toggle click) is what unlocks it.
  void ctx.resume().catch(() => {})

  const live = new Set<AudioBufferSourceNode>()
  const ended: Promise<void>[] = []
  // Passive output tap for getLevel() — sources route through it to the
  // destination (an AnalyserNode adds no latency and doesn't alter audio).
  const analyser = ctx.createAnalyser()
  analyser.fftSize = 512
  analyser.connect(ctx.destination)
  const levelData = new Uint8Array(analyser.fftSize)
  // ``leadS`` is a jitter buffer: the first chunk starts this far in the
  // future so a real-time-paced sender (the duplex engine emits one 20 ms
  // frame per 20 ms over two websocket hops) can run late without an
  // audible gap. Provider-burst senders (half-duplex TTS, SoundIcon) keep
  // the default 0 — their chunks arrive far ahead of playback anyway.
  let nextStart = ctx.currentTime + leadS
  let carry = new Uint8Array(0)
  let stopped = false
  // The lead ADAPTS (R4.5): one player spans many TTS segments, and the
  // [pcm] round-3 data split cleanly in two — gaps over ~1 s are natural
  // inter-segment silences (filler→reply, pre-tool speech→post-tool answer,
  // turn→turn), NOT underruns; the real micro-interruptions were repeated
  // 10-99 ms gaps within continuous audio, i.e. delivery running slightly
  // behind real time and eroding the base lead within seconds. Each real
  // underrun grows the lead so a click or two buys stability for the rest
  // of the segment; a segment boundary re-arms at the base lead (a bad
  // patch must not tax every later reply's time-to-first-audio).
  const SEGMENT_GAP_S = 1
  const LEAD_STEP_S = 0.15
  const LEAD_MAX_S = 1
  let lead = leadS
  // R3.1 instrumentation: count real underruns and their sizes so a live
  // test says where remaining micro-interruptions come from. Read the
  // summary line (console.info on stop) next to the daemon's TTS logs.
  let underruns = 0
  let underrunS = 0
  let chunks = 0
  let audioS = 0

  return {
    enqueue(bytes: Uint8Array) {
      if (stopped) return
      const merged = new Uint8Array(carry.length + bytes.length)
      merged.set(carry)
      merged.set(bytes, carry.length)
      const usable = merged.length - (merged.length % 2)
      carry = merged.slice(usable)
      if (usable === 0) return
      const pcm = new Int16Array(merged.buffer, 0, usable / 2)  // s16le (LE hardware)
      const f32 = new Float32Array(pcm.length)
      for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768
      const buf = ctx.createBuffer(1, f32.length, sampleRate)
      buf.copyToChannel(f32, 0)
      const src = ctx.createBufferSource()
      src.buffer = buf
      src.connect(analyser)
      // Fell behind: re-arm the lead so the buffer re-forms, instead of
      // riding the knife edge and clicking on every late frame from here on.
      let startAt = nextStart
      if (nextStart < ctx.currentTime) {
        const gap = ctx.currentTime - nextStart
        if (chunks === 0 || gap >= SEGMENT_GAP_S) {
          // Startup, or a natural inter-segment silence — not an underrun.
          lead = leadS
        } else {
          underruns++
          underrunS += gap
          lead = Math.min(lead + LEAD_STEP_S, LEAD_MAX_S)
          console.info(
            `[pcm] underrun #${underruns}: ${(gap * 1000).toFixed(0)}ms gap `
            + `after ${audioS.toFixed(1)}s of audio (lead → ${lead.toFixed(2)}s)`)
          try { onUnderrun?.(gap * 1000, audioS) } catch { /* never break playback */ }
        }
        startAt = ctx.currentTime + lead
      }
      src.start(startAt)
      nextStart = startAt + buf.duration
      chunks++
      audioS += buf.duration
      live.add(src)
      ended.push(new Promise<void>(resolve => { src.onended = () => { live.delete(src); resolve() } }))
    },

    pause() {
      // ctx.suspend() freezes currentTime — every scheduled source holds its
      // position and enqueue() during the pause keeps scheduling relative to
      // the frozen clock, so resume() plays it all out in order, gapless.
      if (!stopped) void ctx.suspend().catch(() => {})
    },

    resume() {
      if (!stopped) void ctx.resume().catch(() => {})
    },

    stop() {
      stopped = true
      if (chunks > 0 && underruns > 0) {
        console.info(
          `[pcm] playback summary: ${chunks} chunks, ${audioS.toFixed(1)}s `
          + `audio, ${underruns} underruns (${(underrunS * 1000).toFixed(0)}ms `
          + `total gap)`)
      }
      for (const s of live) { try { s.stop() } catch { /* already stopped */ } }
      live.clear()
      try { ctx.close() } catch { /* ignore */ }
    },

    getLevel() {
      if (stopped) return 0
      analyser.getByteTimeDomainData(levelData)
      let sum = 0
      for (let i = 0; i < levelData.length; i++) {
        const d = (levelData[i] - 128) / 128
        sum += d * d
      }
      // Speech RMS sits well under full scale — scale up so normal speech
      // reads near 1 (same normalization as the mic tap in useDuplexVoice).
      return Math.min(1, Math.sqrt(sum / levelData.length) * 3)
    },

    async drained() {
      // Loop in case more chunks were still enqueuing when first awaited; settles
      // once the scheduled set stops growing.
      let n = -1
      while (ended.length !== n) {
        n = ended.length
        await Promise.all(ended)
      }
    },
  }
}
