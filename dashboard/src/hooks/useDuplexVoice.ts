// useDuplexVoice — the full-duplex conversation mode (the phone engine on a
// dashboard chat). One WebSocket carries everything: 16 kHz PCM mic frames up,
// 24 kHz TTS frames down, JSON control/state/caption frames both ways. The
// server side (proxy bridge → phone daemon) owns VAD, turn-taking, and
// barge-in — this hook is a thin audio terminal: capture with AEC, play, and
// surface the session state for the UI.
//
// The mic stays OPEN while the reply plays — browser echo cancellation
// (echoCancellation:true, the whole reason for the explicit constraints) keeps
// the TTS out of the mic path, and the daemon's VAD handles interruptions.
// This replaced the old half-duplex hands-free loop outright.
//
// Lifecycle: start() opens the MIC IMMEDIATELY (in parallel with the session
// mint POST /v1/duplex/session → /ws/duplex → init → `ready`): captured
// frames land in a bounded ring until `ready`, then flush frame-by-frame and
// go live. This is the first-words fix (2026-08-15): the browser's
// AEC/NS/AGC chain converges during the connecting window instead of over
// the user's first syllables, the STT provider gets leading room tone, and
// words spoken before the session is live are DELIVERED, not lost. The
// 'listening' phase (the blue halo) requires BOTH mic-wired and
// ready+flushed — blue now honestly means "capturing and able to respond".
// Any `end`/`error` frame, socket drop, chat switch, or unmount tears the
// whole thing down; the server enforces the session budget and closes with
// reason "budget" — the UI surfaces the reason and offers a fresh start().

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '../api/auth'
import { acquireMic, releaseMic } from '../audio/micCoordinator'
import { setSpeaking } from '../audio/speechActivity'
import { createPcmPlayer, type PcmPlayer } from '../audio/webaudioPlayer'
import { createPcmRing, zeroedFrame, type PcmRing } from '../audio/pcmRing'
import { downsampleToPCM16 } from '../audio/backends/platformStt'

const MIC_RATE = 16000
const PLAY_RATE = 24000
// Jitter buffer for the real-time-paced engine audio (adds this much
// time-to-first-audio; absorbs network cadence wobble without clicks).
const PLAY_LEAD_S = 0.25

export type DuplexPhase =
  | 'off' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'error'

export interface DuplexVoiceController {
  active: boolean
  phase: DuplexPhase
  /** The ACCUMULATING utterance while the user speaks: finalized pieces
   *  (interim_final frames) committed + the live partial (interim frames)
   *  overlaid — the same accumulate/overlay pair dictation uses. Cleared
   *  when the engine dispatches the turn. */
  caption: string
  /** Why the session ended, when phase === 'error' ('' otherwise). */
  endReason: string
  /** True when the AGENT closed the session ([DUPLEX_COMPLETE] farewell) —
   *  the exit note is skipped: the agent knows it ended the spoken mode. */
  endedByAgent: boolean
  start: () => void
  stop: () => void
  bargeIn: () => void
  /** User-facing mic mute toggle (the in-conversation mic button). While
   *  muted the browser streams ZEROED frames (cadence keeps the engine's
   *  STT keepalive fed; no voice content leaves the device) and the engine
   *  gates its side too. USER-OWNED (operator decision 2026-08-14): only
   *  the mic tap flips it — release/sendTyped re-assert the user's intent
   *  instead of force-unmuting, so a manual mute survives every hold /
   *  typed-send cycle. Reset only when a new session starts. */
  muted: boolean
  toggleMute: () => void
  /** Click-to-edit: focus in the composer — mute the mic and make the
   *  engine drop its pending turn (the composer owns the text now). */
  hold: () => void
  /** Resume listening without sending (composer emptied / edit abandoned). */
  release: () => void
  /** Unmute-after-hold: resume live listening AND hand the held composer
   *  draft back to the engine as the pending turn (auto-dispatched when
   *  the user finishes talking). False when the socket isn't open. */
  releaseWithDraft: (text: string) => boolean
  /** Send edited text as a SPOKEN-mode turn (reply comes back as TTS) and
   *  resume listening. Returns false when the socket isn't open — the
   *  caller falls back to the normal typed send. */
  sendTyped: (text: string) => boolean
  /** Live audio levels (0..1) for the presence halo: `mic` = what the user
   *  is saying, `out` = what the agent's TTS is playing. Ref-read, stable
   *  identity — poll from a rAF loop, never from render. Zeros when idle. */
  getLevels: () => { mic: number; out: number }
}

// Commit/overlay join: a space between pieces unless either side is empty.
function joinCaption(a: string, b: string): string {
  return a && b ? `${a} ${b}` : a || b
}

function wsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/ws/duplex`
}

export function useDuplexVoice(chatId: string | null | undefined): DuplexVoiceController {
  const [phase, setPhase] = useState<DuplexPhase>('off')
  const [caption, setCaption] = useState('')
  const [endReason, setEndReason] = useState('')
  const [endedByAgent, setEndedByAgent] = useState(false)
  const [muted, setMuted] = useState(false)
  const mutedRef = useRef(false)

  const wsRef = useRef<WebSocket | null>(null)
  // Finalized-but-undispatched pieces of the utterance the user is speaking.
  // The STT provider finalizes phrases DURING continuous speech; the live
  // partial restarts after each one, so the caption must commit finals and
  // overlay only the current partial (dictation semantics) — rendering the
  // partial alone made the composer reset to each newest phrase (R4.6).
  const accumRef = useRef('')
  const ctxRef = useRef<AudioContext | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const nodeRef = useRef<ScriptProcessorNode | null>(null)
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null)
  const playerRef = useRef<PcmPlayer | null>(null)
  // Passive mic tap for getLevels() — a second edge off the mic source
  // (per-frame smooth; the 4096-sample ScriptProcessor cadence is ~12 Hz).
  const micAnalyserRef = useRef<AnalyserNode | null>(null)
  const micDataRef = useRef<Uint8Array<ArrayBuffer> | null>(null)
  const startingRef = useRef(false)
  // Session generation: teardown bumps it, every await-resumption in the
  // early-mic path checks it. This (not ws identity — there is no ws yet
  // when the mic opens) is what stops an in-flight getUserMedia from
  // leaking a live stream after teardown.
  const runRef = useRef(0)
  // Pre-ready mic buffering: frames ring-buffer until `ready` flushes them
  // and sets the send target. null target = still buffering.
  const sendTargetRef = useRef<WebSocket | null>(null)
  const ringRef = useRef<PcmRing | null>(null)
  const micLiveRef = useRef(false)
  const chatRef = useRef(chatId)
  // Diagnostics beacon (2026-08-12, the turn-end TTS gap hunt): audio
  // underruns + main-thread long tasks ride the duplex socket as
  // `client_stat` frames and land in the PROXY log — phone tests
  // self-report without device console access. Correlation is the
  // discriminator: underruns WITH longtasks = browser jank (turn-end
  // render burst starving the 0.25s jitter buffer); underruns WITHOUT
  // = upstream (proxy relay stall). Capped per session.
  const statCountRef = useRef(0)
  const longTaskObsRef = useRef<PerformanceObserver | null>(null)
  // Perceived time-to-first-audio: armed at the `final` frame (turn
  // dispatched), consumed by the first binary chunk after it. PERCEIVED —
  // a thinking filler rides the same channel and counts; the daemon's
  // First-audio breakdown lines disambiguate filler vs reply. One stat per
  // turn, deliberately OUTSIDE the 40-stat cap (own budget: the armed ref).
  const ttfaArmRef = useRef(0)

  const sendStat = useCallback((stat: Record<string, unknown>) => {
    if (statCountRef.current >= 40) return
    statCountRef.current++
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'client_stat', ...stat })) } catch { /* ignore */ }
    }
  }, [])

  const teardown = useCallback((reason: string, isError: boolean) => {
    startingRef.current = false
    runRef.current++
    // Stale room audio must never flush into the NEXT session.
    ringRef.current?.clear()
    ringRef.current = null
    sendTargetRef.current = null
    micLiveRef.current = false
    const ws = wsRef.current
    wsRef.current = null
    if (ws) {
      ws.onmessage = ws.onclose = ws.onerror = null
      try { ws.close() } catch { /* ignore */ }
    }
    try { nodeRef.current?.disconnect(); sourceRef.current?.disconnect() } catch { /* ignore */ }
    try { void ctxRef.current?.close() } catch { /* ignore */ }
    streamRef.current?.getTracks().forEach(t => t.stop())
    try { playerRef.current?.stop() } catch { /* ignore */ }
    nodeRef.current = null; sourceRef.current = null
    ctxRef.current = null; streamRef.current = null; playerRef.current = null
    micAnalyserRef.current = null; micDataRef.current = null
    try { longTaskObsRef.current?.disconnect() } catch { /* ignore */ }
    longTaskObsRef.current = null
    statCountRef.current = 0
    ttfaArmRef.current = 0
    mutedRef.current = false
    setMuted(false)
    accumRef.current = ''
    setCaption('')
    setEndReason(isError ? reason : '')
    setPhase(isError ? 'error' : 'off')
  }, [])

  const stop = useCallback(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'end' })) } catch { /* ignore */ }
    }
    teardown('', false)
  }, [teardown])

  const bargeIn = useCallback(() => {
    // Cut the audio locally NOW (the daemon's fade races the tap), then tell
    // the engine so the turn bookkeeping (spoken chars) is correct. Flip the
    // phase locally too — the tap means "the floor is mine": the halo must
    // read listening immediately, not after the daemon's cut→drain edge
    // round-trips (the daemon's own 'listening' frame follows and agrees).
    try { playerRef.current?.stop() } catch { /* ignore */ }
    playerRef.current = null
    setPhase(p => (p === 'speaking' || p === 'thinking') ? 'listening' : p)
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'barge_in' })) } catch { /* ignore */ }
    }
  }, [])

  const toggleMute = useCallback(() => {
    const next = !mutedRef.current
    mutedRef.current = next
    setMuted(next)
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: next ? 'mute' : 'unmute' }))
      } catch { /* ignore */ }
    }
  }, [])

  const hold = useCallback(() => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: 'hold' })) } catch { /* ignore */ }
    }
    // STICKY MUTE (operator revision 2026-08-24 of the 2026-08-14 rule):
    // composer focus IS a mute — the icon shows it and it survives the
    // send; ONLY the mic tap unmutes (which releases the hold and hands
    // any draft back to the engine). This ends the dead-mic desync where
    // a non-empty draft left the engine gated behind a live-looking icon.
    mutedRef.current = true
    setMuted(true)
    // The caption's text lives on in the composer as a normal draft; the
    // caption state itself is done (the bridge deactivates before this).
    // The engine gates its caption channels while held, so late STT output
    // can't resurrect the accumulator into the user's draft.
    accumRef.current = ''
    setCaption('')
  }, [])

  const releaseWithDraft = useCallback((text: string): boolean => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    try {
      // One frame carries both: mic live again + the held draft re-seeds
      // the engine's pending turn (dispatched when the user stops talking,
      // or alone after the classifier grace).
      ws.send(JSON.stringify({ type: 'release', text }))
    } catch { return false }
    mutedRef.current = false
    setMuted(false)
    return true
  }, [])

  const release = useCallback(() => {
    // Restore the USER'S intent, don't overwrite it (sticky mute, 2026-08-14):
    // a hold ends with the engine muted — if the user had muted the mic,
    // re-assert 'mute' (idempotent) and keep the icon; only an unmuted user
    // gets 'unmute'. Icon and engine state stay in agreement either way —
    // the dead-mic-trap rule, correctly scoped.
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: mutedRef.current ? 'mute' : 'unmute' }))
      } catch { /* ignore */ }
    }
  }, [])

  const sendTyped = useCallback((text: string): boolean => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    try {
      ws.send(JSON.stringify({ type: 'typed_utterance', text }))
      // Sticky mute: a typed send resumes LISTENING only if the user hadn't
      // muted the mic — mute is about the mic, the reply still plays as TTS.
      ws.send(JSON.stringify({ type: mutedRef.current ? 'mute' : 'unmute' }))
    } catch { return false }
    return true
  }, [])

  const start = useCallback(() => {
    if (startingRef.current || wsRef.current || !chatRef.current) return
    startingRef.current = true
    const run = ++runRef.current
    // Own the mic arbiter BEFORE anything async: the phase effect only
    // acquires on the next commit — too late now that the mic opens at
    // start-time (the wake listener must yield first; acquire is
    // idempotent, the effect's own acquire/release stays authoritative).
    acquireMic('duplex')
    setEndReason('')
    setEndedByAgent(false)
    mutedRef.current = false
    setMuted(false)
    ringRef.current = createPcmRing()
    sendTargetRef.current = null
    micLiveRef.current = false
    setPhase('connecting')
    // AudioContext synchronously, while a tap-initiated start still has its
    // user activation (after any await it is gone on Safari); resume errors
    // are swallowed — a suspended context just yields an empty ring, which
    // must not wedge the phase (the listening gate ignores ring fill).
    try {
      const ctx = new AudioContext()
      ctxRef.current = ctx
      void ctx.resume().catch(() => { /* ignore */ })
    } catch { /* openMic falls back to constructing one */ }
    void openMic(run)
    void (async () => {
      try {
        const res = await apiFetch('/v1/duplex/session', {
          method: 'POST',
          body: JSON.stringify({ chat_id: chatRef.current }),
        })
        if (!res.ok) {
          const detail = (await res.json().catch(() => null))?.detail
          throw new Error(detail || `duplex session refused (${res.status})`)
        }
        const { ws_token } = await res.json()
        if (!startingRef.current) return

        const ws = new WebSocket(wsUrl())
        ws.binaryType = 'arraybuffer'
        wsRef.current = ws
        ws.onopen = () => ws.send(JSON.stringify({ type: 'init', token: ws_token }))
        ws.onclose = () => { if (wsRef.current === ws) teardown('connection lost', true) }
        ws.onerror = () => { if (wsRef.current === ws) teardown('connection error', true) }
        ws.onmessage = (ev) => {
          if (typeof ev.data !== 'string') {
            if (ttfaArmRef.current) {
              const ms = Math.round(performance.now() - ttfaArmRef.current)
              ttfaArmRef.current = 0
              try { ws.send(JSON.stringify({ type: 'client_stat', kind: 'ttfa', ms })) } catch { /* ignore */ }
            }
            // 24 kHz s16le TTS audio. Player is per-reply: barge-in stops and
            // clears it; the next chunk lazily opens a fresh one.
            let player = playerRef.current
            if (!player) {
              player = createPcmPlayer(PLAY_RATE, PLAY_LEAD_S, (gapMs, afterS) =>
                sendStat({
                  kind: 'underrun',
                  gap_ms: Math.round(gapMs),
                  after_s: Math.round(afterS * 10) / 10,
                }))
              playerRef.current = player
            }
            player.enqueue(new Uint8Array(ev.data as ArrayBuffer))
            return
          }
          let frame: Record<string, unknown>
          try { frame = JSON.parse(ev.data) } catch { return }
          switch (frame.type) {
            case 'ready': {
              // Go live: flush the pre-ready ring FRAME-BY-FRAME (never
              // concatenated — the daemon's inbound queue is frame-counted
              // and wire framing must match live streaming), then set the
              // send target. Synchronous, so no onaudioprocess can
              // interleave between drain and target assignment. Never send
              // binary before this frame: the proxy handshake reads text
              // first. Muted-at-flush zeroes the whole burst — a user who
              // muted during connecting gets no pre-mute speech sent either.
              const buffered = ringRef.current?.drain() ?? []
              for (const f of buffered) {
                try { ws.send(mutedRef.current ? zeroedFrame(f) : f) } catch { break }
              }
              sendTargetRef.current = ws
              if (mutedRef.current) {
                // The engine learned nothing of a mute toggled during
                // connecting (no socket yet) — re-assert it now (the same
                // class of rule as release()'s sticky mute).
                try { ws.send(JSON.stringify({ type: 'mute' })) } catch { /* ignore */ }
              }
              maybeListening()
              // Arm the long-task probe for the session (see sendStat).
              // ≥200ms main-thread stalls are the ones that can starve the
              // 250ms audio jitter buffer.
              try {
                const obs = new PerformanceObserver((list) => {
                  for (const e of list.getEntries()) {
                    if (e.duration >= 200) {
                      sendStat({ kind: 'longtask', ms: Math.round(e.duration) })
                    }
                  }
                })
                obs.observe({ type: 'longtask', buffered: false })
                longTaskObsRef.current = obs
              } catch { /* unsupported (Safari) — underruns still report */ }
              break
            }
            case 'state': {
              const s = String(frame.state || '')
              if (s === 'listening' || s === 'thinking' || s === 'speaking') setPhase(s)
              // Any state edge consumes the caption: 'thinking' = the turn
              // dispatched (it read as "my text is still in the input"
              // otherwise — live-test), 'speaking' covers re-dispatched
              // queued speech (no fresh final/thinking frames — the reply
              // audio IS the dispatch signal), 'listening' = clean slate at
              // mic-live. In-progress speech rebuilds from the next frames.
              accumRef.current = ''
              setCaption('')
              break
            }
            case 'interim':
              // Live partial overlaid onto the committed pieces.
              setCaption(joinCaption(accumRef.current, String(frame.text || '')))
              break
            case 'interim_final':
              // A finalized piece of the still-accumulating utterance —
              // commit it; the provider's partial restarts after each one.
              accumRef.current = joinCaption(accumRef.current, String(frame.text || ''))
              setCaption(accumRef.current)
              break
            case 'final':
              // Dispatched: the utterance leaves the composer (it shows up
              // as the chat's user message) — the thinking frame follows.
              accumRef.current = ''
              setCaption('')
              ttfaArmRef.current = performance.now()
              break
            case 'flush':
              // Daemon barge-in cut: drop everything scheduled locally too —
              // the player buffers 250ms–1s of lead and would keep talking
              // over the user otherwise. The manual tap already stopped and
              // nulled the player (this is then a no-op); VAD-detected
              // barge-ins arrive ONLY through here. The next binary chunk
              // lazily opens a fresh player at base lead.
              try { playerRef.current?.stop() } catch { /* ignore */ }
              playerRef.current = null
              break
            case 'pause':
              // Barge-in pause: the daemon heard speech and is waiting for
              // transcript confirmation — suspend playback in place (the
              // scheduled lead is retained; a 'resume' continues exactly
              // where it froze, a commit arrives as 'flush' instead).
              // The halo flips to 'listening' the moment the daemon hears
              // the user — even for episodes that later resume (operator
              // live-test 2026-08-24: the yellow "speaking" halo over an
              // active barge-in read as "it can't hear me").
              try { playerRef.current?.pause() } catch { /* ignore */ }
              setPhase('listening')
              break
            case 'resume':
              try { playerRef.current?.resume() } catch { /* ignore */ }
              setPhase('speaking')
              break
            case 'end':
              if (frame.reason === 'agent_complete') setEndedByAgent(true)
              teardown(String(frame.reason || ''), frame.reason === 'budget')
              break
            case 'error':
              teardown(String(frame.reason || 'error'), true)
              break
          }
        }
      } catch (e) {
        teardown(e instanceof Error ? e.message : 'could not start', true)
      }
    })()

    // 'listening' (the blue halo) needs BOTH legs — mic wired AND
    // ready+flushed — whichever finishes second calls this. Deliberately
    // NOT conditioned on ring fill: a suspended AudioContext produces no
    // frames but must not wedge the phase.
    function maybeListening() {
      if (micLiveRef.current && sendTargetRef.current) {
        startingRef.current = false
        setPhase('listening')
      }
    }

    async function openMic(run: number) {
      try {
        // Explicit AEC/NS/AGC: an open mic during playback is only viable
        // with echo cancellation — the plain {audio:true} default the
        // dictation path uses is not enough here.
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        })
        if (runRef.current !== run) { stream.getTracks().forEach(t => t.stop()); return }
        streamRef.current = stream
        const ctx = ctxRef.current ?? new AudioContext()
        ctxRef.current = ctx
        await ctx.resume().catch(() => {})
        if (runRef.current !== run) { stream.getTracks().forEach(t => t.stop()); return }
        const source = ctx.createMediaStreamSource(stream)
        const node = ctx.createScriptProcessor(4096, 1, 1)
        sourceRef.current = source
        nodeRef.current = node
        const micAnalyser = ctx.createAnalyser()
        micAnalyser.fftSize = 512
        source.connect(micAnalyser)
        micAnalyserRef.current = micAnalyser
        micDataRef.current = new Uint8Array(micAnalyser.fftSize)
        node.onaudioprocess = (e) => {
          if (runRef.current !== run) return
          const pcm = downsampleToPCM16(e.inputBuffer.getChannelData(0), ctx.sampleRate, MIC_RATE)
          // Muted: keep the frame CADENCE but zero the payload — no voice
          // content leaves the device, while the engine's STT keepalive
          // stays fed (Deepgram idle-kills its socket after ~12s without
          // audio or keepalives — the 2026-08-12 dead-mic incident). The
          // transform applies at PUSH time too, so a mute/unmute inside the
          // connecting window zeroes exactly the muted stretch of the ring.
          const frame = mutedRef.current ? zeroedFrame(pcm) : pcm
          const target = sendTargetRef.current
          if (!target) { ringRef.current?.push(frame); return }
          if (target.readyState !== WebSocket.OPEN) return
          target.send(frame)
        }
        source.connect(node)
        node.connect(ctx.destination)
        micLiveRef.current = true
        maybeListening()
      } catch {
        // A stale run's failure must not tear down a NEWER session.
        if (runRef.current !== run) return
        teardown('microphone unavailable', true)
      }
    }
  }, [teardown, sendStat])

  const getLevels = useCallback((): { mic: number; out: number } => {
    let mic = 0
    const analyser = micAnalyserRef.current
    const data = micDataRef.current
    if (analyser && data && !mutedRef.current) {
      analyser.getByteTimeDomainData(data)
      let sum = 0
      for (let i = 0; i < data.length; i++) {
        const d = (data[i] - 128) / 128
        sum += d * d
      }
      // Same speech-RMS normalization as PcmPlayer.getLevel.
      mic = Math.min(1, Math.sqrt(sum / data.length) * 3)
    }
    return { mic, out: playerRef.current?.getLevel() ?? 0 }
  }, [])

  // Chat switch or unmount ends the session (the bridge is per-chat). The
  // guard must see a CONNECTING session too — the mic now opens before the
  // ws exists, so wsRef alone would leak a live stream (and the wake
  // listener would re-open the mic on top of it).
  const liveOrConnecting = () =>
    Boolean(wsRef.current || startingRef.current || streamRef.current)
  useEffect(() => {
    if (chatRef.current !== chatId && liveOrConnecting()) stop()
    chatRef.current = chatId
  }, [chatId, stop])
  useEffect(() => () => { if (liveOrConnecting()) stop() }, [stop])

  // Mic arbiter: a live session (any phase, incl. connecting — the mic is
  // already open) makes the ambient wake-word listener release the mic.
  // start() also acquires synchronously (this effect runs a commit later,
  // after the early getUserMedia is already in flight); this effect stays
  // authoritative for release.
  useEffect(() => {
    if (phase !== 'off' && phase !== 'error') acquireMic('duplex')
    else releaseMic('duplex')
  }, [phase])
  useEffect(() => () => releaseMic('duplex'), [])

  // Speech-activity flag: heavy turn-end UI work defers while the agent is
  // audibly speaking (speechActivity.onIdle). Phase-driven on purpose — the
  // player's drain edge fires at every natural inter-segment silence. The
  // ref keeps the counted flag balanced across phase churn and unmount.
  const speakingFlaggedRef = useRef(false)
  useEffect(() => {
    const s = phase === 'speaking'
    if (s !== speakingFlaggedRef.current) {
      speakingFlaggedRef.current = s
      setSpeaking(s)
    }
  }, [phase])
  useEffect(() => () => {
    if (speakingFlaggedRef.current) {
      speakingFlaggedRef.current = false
      setSpeaking(false)
    }
  }, [])

  return {
    active: phase !== 'off' && phase !== 'error',
    phase, caption, endReason, endedByAgent, muted,
    start, stop, bargeIn, toggleMute, hold, release, releaseWithDraft, sendTyped, getLevels,
  }
}
