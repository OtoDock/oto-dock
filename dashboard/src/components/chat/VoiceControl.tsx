// VoiceControl — the chat mic + live-voice control, on the right of the input
// (immediately left of Send). Default: just the mic (dictation). Tap or slide-left
// to reveal/enable LIVE mode, a hands-free half-duplex conversation:
//
//   listen → (you stop talking) the mic CLOSES and the provider flushes its
//   tail → the FULL transcript auto-sends → the reply is spoken (mic stays
//   closed: no echo, no provider max-seconds) → on TTS end OR a mic tap the
//   mic re-opens (a fresh STT session) and listens again.
//
// End-of-turn is detected client-side: native Web Speech ends on its own
// silence; the platform WS — which never ends on silence — is stopped by a
// silence timer. Either way the SEND happens from onCommit, AFTER the stop
// flush delivered everything (sending at timer expiry raced the provider's
// end-of-utterance commit and truncated long turns). The STT connection is
// torn down between turns, so it can't carry a transcript into the next
// message.
//
// The TTS side (speaking the reply) lives in useVoiceMode (parent); this owns the
// mic + the loop and reacts to `speaking`.

import { useEffect, useRef, useState } from 'react'
import { useSpeechSession } from '../../hooks/useSpeechSession'
import { MicGlyph } from './MicGlyph'

const SLIDE_PX = 36          // horizontal drag to enable live mode
const SILENCE_MS = 1700      // end the turn this long after you stop talking
const NO_SPEECH_MS = 12000   // give up a live listen if nothing is said

export interface VoiceControlProps {
  ttsAvailable: boolean        // chat TTS resolvable → live mode is possible
  live: boolean                // live mode on (persisted by the parent)
  onSetLive: (on: boolean) => void
  speaking: boolean            // a reply is being spoken right now (useVoiceMode)
  onBargeIn: () => void        // cancel the current spoken reply
  streaming: boolean           // a reply is generating
  onSendText: (text: string) => void           // live: send the transcript
  onClearInput: () => void                       // live: clear the input after a turn auto-sends
  onDictateInterim: (text: string) => void       // input live partial (both modes — feedback)
  onDictateFinal: (text: string) => void          // input committed phrase
  onDictateActive: (active: boolean) => void       // input base snapshot
  interruptSignal: number                        // bumped on manual send / input focus → stop the mic
  disabled?: boolean
}

type Phase = 'off' | 'listen' | 'flush' | 'await' | 'paused'

export function VoiceControl({
  ttsAvailable, live, onSetLive, speaking, onBargeIn, streaming,
  onSendText, onClearInput, onDictateInterim, onDictateFinal, onDictateActive, interruptSignal, disabled,
}: VoiceControlProps) {
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const phaseRef = useRef<Phase>('off')
  const sawActivityRef = useRef(false)   // saw the reply generate/speak since sending
  const didMountRef = useRef(false)
  const dragRef = useRef<{ x: number; slid: boolean } | null>(null)  // slide-to-enable gesture
  const interruptMounted = useRef(false)
  const silenceTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const endRef = useRef<() => void>(() => {})  // end-of-turn (assigned after `speech`)

  const showError = (msg: string) => {
    setErrorMsg(msg)
    if (errorTimer.current) clearTimeout(errorTimer.current)
    errorTimer.current = setTimeout(() => setErrorMsg(null), 7000)
  }
  useEffect(() => () => { if (errorTimer.current) clearTimeout(errorTimer.current) }, [])

  const clearSilence = () => { if (silenceTimer.current) { clearTimeout(silenceTimer.current); silenceTimer.current = null } }
  const armSilence = (ms: number) => { clearSilence(); silenceTimer.current = setTimeout(() => endRef.current(), ms) }

  const speech = useSpeechSession({
    onInterim: (t) => { onDictateInterim(t); if (live) armSilence(SILENCE_MS) },
    onFinal: (t) => { onDictateFinal(t); if (live) armSilence(SILENCE_MS) },
    onActive: (a) => {
      onDictateActive(a)
      if (live) { if (a) armSilence(NO_SPEECH_MS); else clearSilence() }
    },
    // The turn's text is the session's FULL accumulated transcript, delivered
    // AFTER the stop flush — never a locally-buffered subset. The old path
    // sent a client-side finals buffer the moment the silence timer fired,
    // which RACED the provider's end-of-utterance commit (client 1.7s vs
    // server 1.5s + network): long turns went out with the whole tail
    // missing (live repro 2026-07-16). 'flush' = we stopped on silence;
    // 'listen' = a native recognizer ended on its own silence detection.
    onCommit: (text) => {
      if (!live) return
      if (phaseRef.current !== 'flush' && phaseRef.current !== 'listen') return
      clearSilence()
      const t = text.trim()
      if (t) {
        phaseRef.current = 'await'
        sawActivityRef.current = false
        onSendText(t)
        onClearInput()
      } else {
        phaseRef.current = 'paused'  // nothing said → wait for a tap
      }
    },
    onError: (msg) => { showError(msg); clearSilence(); if (live) phaseRef.current = 'paused' },
  })

  // End the live turn: STOP the mic and let the flush deliver the transcript —
  // onCommit above does the send. Idempotent via the phase guard.
  endRef.current = () => {
    if (phaseRef.current !== 'listen') return
    clearSilence()
    phaseRef.current = 'flush'
    speech.stop()
  }

  // Live enable/disable (NOT on mount — a persisted "on" shouldn't open the mic on
  // page load; the user taps the mic to begin).
  useEffect(() => {
    if (!didMountRef.current) {
      didMountRef.current = true
      phaseRef.current = live ? 'paused' : 'off'
      return
    }
    if (live) {
      phaseRef.current = 'listen'; sawActivityRef.current = false
      void speech.start()
    } else {
      phaseRef.current = 'off'; clearSilence(); onBargeIn(); speech.stop()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live])

  // Loop continuation: once the sent reply has finished generating AND speaking,
  // reopen the mic for the next turn.
  useEffect(() => {
    if (!live || phaseRef.current !== 'await') return
    if (streaming || speaking) sawActivityRef.current = true
    if (sawActivityRef.current && !streaming && !speaking) {
      phaseRef.current = 'listen'
      void speech.start()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, speaking, streaming, speech.status])

  // A manual send (Enter / Send) or clicking into the input closes the mic — in
  // live mode it pauses the loop (the user is taking manual control).
  useEffect(() => {
    if (!interruptMounted.current) { interruptMounted.current = true; return }
    clearSilence()
    if (live) phaseRef.current = 'paused'
    speech.stop()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interruptSignal])

  // Stop the mic if this control unmounts.
  useEffect(() => () => { clearSilence(); speech.stop() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!speech.available) return null  // no STT on this device → no mic at all

  const recording = speech.status === 'recording'
  const connecting = speech.status === 'connecting'
  const liveSpeaking = live && speaking
  const liveAwaiting = live && streaming && !speaking && !recording && !connecting  // reply generating, mic off
  const showToggle = ttsAvailable && (live || speech.status !== 'idle')

  const onMicTap = () => {
    setErrorMsg(null)
    if (!live) { speech.toggle(); return }
    if (speaking) {                          // barge-in: stop TTS, listen now
      onBargeIn()
      phaseRef.current = 'listen'; sawActivityRef.current = false
      void speech.start()
      return
    }
    if (streaming) return                    // reply generating, mic off — wait
    if (recording || connecting) {           // pause the loop
      phaseRef.current = 'paused'; speech.stop()
      return
    }
    phaseRef.current = 'listen'; sawActivityRef.current = false  // (re)start
    void speech.start()
  }

  // Slide-left on the mic enables live mode (fast path on mobile).
  const onPointerDown = (e: React.PointerEvent) => {
    dragRef.current = { x: e.clientX, slid: false }
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId) } catch { /* ignore */ }
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d || d.slid) return
    if (ttsAvailable && !live && d.x - e.clientX > SLIDE_PX) { d.slid = true; onSetLive(true) }
  }
  const onMicClick = () => {
    const slid = dragRef.current?.slid
    dragRef.current = null
    if (slid) return  // was a slide-to-enable, not a tap
    onMicTap()
  }

  const micTitle = errorMsg ? errorMsg
    : liveSpeaking ? 'Tap to interrupt and speak'
    : liveAwaiting ? 'Working…'
    : connecting ? 'Connecting…'
    : recording ? (live ? 'Listening… (tap to pause)' : 'Stop dictation')
    : live ? 'Tap to speak'
    : 'Dictate'

  const micClass = liveSpeaking ? 'text-brand bg-brand/10 animate-pulse'
    : liveAwaiting ? 'text-p-text-light'
    : recording ? 'text-red-500 bg-red-500/10 animate-pulse'
    : connecting ? 'text-brand'
    : errorMsg ? 'text-red-400 hover:text-red-500'
    : live ? 'text-brand hover:bg-brand/5'
    : 'text-p-text-secondary hover:text-brand hover:bg-brand/5'

  return (
    <div className="relative flex items-center shrink-0">
      {errorMsg && (
        <div
          role="alert"
          onClick={() => setErrorMsg(null)}
          className="absolute bottom-full mb-2 right-0 z-50 w-60 px-3 py-2 rounded-lg cursor-pointer
            text-xs leading-snug bg-red-600 text-white shadow-lg"
        >
          {errorMsg}
        </div>
      )}

      {/* Live toggle — slides out to the LEFT of the mic when engaged. */}
      <div className={`overflow-hidden transition-all duration-200 ${showToggle ? 'w-9 opacity-100' : 'w-0 opacity-0'}`}>
        <button
          type="button"
          onClick={() => onSetLive(!live)}
          disabled={disabled}
          aria-pressed={live}
          title={live ? 'Live voice mode on — tap to turn off' : 'Turn on live voice (hands-free)'}
          className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors shrink-0
            disabled:opacity-40 disabled:cursor-not-allowed
            ${live ? 'text-brand bg-brand/10' : 'text-p-text-secondary hover:text-brand hover:bg-brand/5'}`}
        >
          {/* headphones */}
          <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
            <path d="M12 3a9 9 0 00-9 9v7a2 2 0 002 2h2a1 1 0 001-1v-5a1 1 0 00-1-1H5v-2a7 7 0 0114 0v2h-2a1 1 0 00-1 1v5a1 1 0 001 1h2a2 2 0 002-2v-7a9 9 0 00-9-9z" />
          </svg>
        </button>
      </div>

      {/* Mic — dictation (live off) or the conversation control (live on). */}
      <button
        type="button"
        onClick={onMicClick}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        disabled={disabled || liveAwaiting}
        title={micTitle}
        className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors shrink-0 touch-none
          disabled:cursor-not-allowed ${micClass}`}
      >
        {liveSpeaking ? (
          // speaking: a stop square — tap to interrupt
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
            <rect x="6" y="6" width="12" height="12" rx="1.5" />
          </svg>
        ) : connecting ? (
          <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
        ) : (
          <MicGlyph />
        )}
      </button>
    </div>
  )
}
