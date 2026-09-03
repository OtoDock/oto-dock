// VoiceControl — the chat mic + full-duplex conversation control, on the right
// of the input (immediately left of Send).
//
// Two modes only:
//   - **Dictation** (the mic): tap to toggle speech-to-text into the composer.
//     Nothing auto-sends — the user reviews and hits Send.
//   - **Full duplex** (the phone toggle): a live conversation on the phone
//     daemon's engine — open mic, spoken replies, barge-in BY VOICE (just
//     talk; the engine cuts TTS and takes the interjection). The toggle
//     slides out when the mic engages (same choreography the old headphones
//     toggle had) or whenever a session is live; slide-left on the mic is
//     the fast enable. While active, the mic button is a MUTE TOGGLE
//     (operator UX decision 2026-08-12 — the old tap-to-barge-in stop
//     square is gone; spoken barge-in and the chat's red Stop cover it) and
//     the live transcript renders in the composer through the dictation
//     display path (interim text, cleared on dispatch).
//
// The old half-duplex hands-free loop (listen → auto-send → speak → re-listen)
// is gone — full duplex replaced it. Dictation is the only client-side speech
// capture left; everything conversational lives server-side.

import { useEffect, useRef, useState } from 'react'
import { useSpeechSession } from '../../hooks/useSpeechSession'
import { MicGlyph } from './MicGlyph'

const SLIDE_PX = 36          // horizontal drag to enable duplex mode

export interface DuplexControlProps {
  available: boolean           // capability.duplex.available (server, fail-closed)
  active: boolean
  phase: 'off' | 'connecting' | 'listening' | 'thinking' | 'speaking' | 'error'
  caption: string              // live transcript of the user's speech
  endReason: string            // why the session errored ('' otherwise)
  onToggle: () => void
  /** Mic mute toggle while the conversation is live (the mic button). */
  muted?: boolean
  onToggleMute?: () => void
  /** Click-to-edit: composer focus — mute + engine drops its pending turn. */
  onHold?: () => void
  /** Resume listening without sending (composer emptied / edit abandoned). */
  onRelease?: () => void
  /** Unmute tap during/after a hold: resume live listening and hand the
   *  held composer draft back to the engine — it becomes the pending turn
   *  and auto-dispatches when the user finishes talking (operator design
   *  2026-08-24). Returns false when the socket is closed. */
  onReleaseWithDraft?: (text: string) => boolean
  /** The composer's current draft (ChatInput provides it — the unmute tap
   *  reads it to hand the text back). */
  getHeldDraft?: () => string
  /** The draft was handed back to the engine — clear the composer. */
  onDraftConsumed?: () => void
  /** Live mic/TTS levels for the PresenceHalo (ChatInput consumes it;
   *  VoiceControl itself never reads it). Optional: absent means the halo
   *  breathes without audio reactivity. */
  getLevels?: () => { mic: number; out: number }
}

export interface VoiceControlProps {
  duplex?: DuplexControlProps  // full-duplex conversation mode (phone engine)
  onDictateInterim: (text: string) => void       // input live partial (feedback)
  onDictateFinal: (text: string) => void          // input committed phrase
  onDictateActive: (active: boolean) => void       // input base snapshot
  interruptSignal: number                        // bumped on input focus → stop the mic, KEEP the tail
  discardSignal: number                          // bumped on manual send → stop the mic, DROP the tail
  disabled?: boolean
}

export function VoiceControl({
  duplex,
  onDictateInterim, onDictateFinal, onDictateActive, interruptSignal, discardSignal, disabled,
}: VoiceControlProps) {
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const dragRef = useRef<{ x: number; slid: boolean } | null>(null)  // slide-to-enable gesture
  const interruptMounted = useRef(false)
  const discardMounted = useRef(false)
  const captionShownRef = useRef(false)

  const showError = (msg: string) => {
    setErrorMsg(msg)
    if (errorTimer.current) clearTimeout(errorTimer.current)
    errorTimer.current = setTimeout(() => setErrorMsg(null), 7000)
  }
  useEffect(() => () => { if (errorTimer.current) clearTimeout(errorTimer.current) }, [])

  const speech = useSpeechSession({
    onInterim: onDictateInterim,
    onFinal: onDictateFinal,
    onActive: onDictateActive,
    onCommit: () => {},   // dictation never auto-sends
    onError: (msg) => showError(msg),
  })

  // Duplex live transcript → the composer, through the same display path
  // dictation uses (interim text over the input, cleared when the engine
  // dispatches the utterance). Replaces the old floating caption pill, which
  // overlapped the input on mobile.
  const dupActive = !!duplex?.active
  const dupCaption = duplex?.caption ?? ''
  useEffect(() => {
    if (!dupActive) {
      if (captionShownRef.current) {
        captionShownRef.current = false
        onDictateInterim('')
        onDictateActive(false)
      }
      return
    }
    if (dupCaption) {
      if (!captionShownRef.current) { captionShownRef.current = true; onDictateActive(true) }
      onDictateInterim(dupCaption)
    } else if (captionShownRef.current) {
      captionShownRef.current = false
      onDictateInterim('')
      onDictateActive(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dupActive, dupCaption])

  // Clicking into the input closes the dictation mic (the tail final still
  // lands in the input so the user keeps the words they said). While duplex
  // is live it's CLICK-TO-EDIT: the caption bridge deactivates (the text
  // becomes a normal editable draft) and the engine holds — mic muted, its
  // pending turn dropped — until Send dispatches or the edit is abandoned.
  useEffect(() => {
    if (!interruptMounted.current) { interruptMounted.current = true; return }
    speech.stop()
    if (duplex?.active) {
      if (captionShownRef.current) {
        captionShownRef.current = false
        onDictateActive(false)
      }
      duplex.onHold?.()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interruptSignal])

  // A manual send closes the mic AND discards the tail: Send already consumed
  // the input, a late final must not re-fill the cleared composer.
  useEffect(() => {
    if (!discardMounted.current) { discardMounted.current = true; return }
    speech.stop(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [discardSignal])

  // Stop the mic if this control unmounts.
  useEffect(() => () => { speech.stop() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  if (!speech.available) return null  // no STT on this device → no mic at all

  const recording = speech.status === 'recording'
  const connecting = speech.status === 'connecting'
  const dupPhase = duplex?.active ? duplex.phase : null
  // The phone toggle slides out when the mic engages or a session is live —
  // the same reveal choreography the old headphones toggle had.
  const showDuplexToggle = !!duplex?.available
    && (duplex.active || duplex.phase === 'connecting' || speech.status !== 'idle')

  const onMicTap = () => {
    setErrorMsg(null)
    if (duplex?.active) {
      // Live conversation: the mic button is the MUTE toggle. Barging in
      // is voice-first (just talk — the engine cuts TTS) and the chat's
      // red Stop covers a silent hard stop.
      if (duplex.muted && duplex.onReleaseWithDraft) {
        // Un-muting releases any composer hold: live listening resumes
        // and a held draft rides back to the engine as the pending turn
        // (auto-sent when the user finishes talking).
        const draft = duplex.getHeldDraft?.() ?? ''
        if (duplex.onReleaseWithDraft(draft)) {
          if (draft) duplex.onDraftConsumed?.()
          return
        }
      }
      duplex.onToggleMute?.()
      return
    }
    speech.toggle()
  }

  // Slide-left on the mic starts a duplex conversation (fast path on mobile).
  const onPointerDown = (e: React.PointerEvent) => {
    dragRef.current = { x: e.clientX, slid: false }
    try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId) } catch { /* ignore */ }
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d || d.slid) return
    if (duplex?.available && !duplex.active && d.x - e.clientX > SLIDE_PX) {
      d.slid = true
      speech.stop(true)
      duplex.onToggle()
    }
  }
  const onMicClick = () => {
    const slid = dragRef.current?.slid
    dragRef.current = null
    if (slid) return  // was a slide-to-enable, not a tap
    onMicTap()
  }

  const dupMuted = !!duplex?.muted
  const micTitle = errorMsg ? errorMsg
    : dupPhase && dupMuted ? 'Mic muted — tap to unmute'
    : dupPhase ? 'Tap to mute the mic'
    : connecting ? 'Connecting…'
    : recording ? 'Stop dictation'
    : 'Dictate'

  const micClass = dupPhase && dupMuted
    // Muted: the slashed glyph alone — no background chip (operator
    // decision 2026-08-14). The red pulsing chip means "live and hearing
    // you"; any colored fill on the muted state reads as activity.
    ? 'text-p-text-light'
    : dupPhase ? 'text-red-500 bg-red-500/10 animate-pulse'
    : recording ? 'text-red-500 bg-red-500/10 animate-pulse'
    : connecting ? 'text-brand'
    : errorMsg ? 'text-red-400 hover:text-red-500'
    : 'text-p-text-secondary hover:text-brand hover:bg-brand/5'

  return (
    <div className="relative flex items-center shrink-0">
      {duplex && duplex.phase === 'error' && duplex.endReason && !errorMsg && (
        <div
          role="alert"
          className="absolute bottom-full mb-2 right-0 z-50 w-60 px-3 py-2 rounded-lg
            text-xs leading-snug bg-red-600 text-white shadow-lg"
        >
          {duplex.endReason}
        </div>
      )}
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

      {/* Full-duplex toggle (phone) — slides out to the LEFT of the mic. */}
      <div className={`overflow-hidden transition-all duration-200 ${showDuplexToggle ? 'w-9 opacity-100' : 'w-0 opacity-0'}`}>
        {duplex?.available && (
          <button
            type="button"
            onClick={() => { speech.stop(true); duplex.onToggle() }}
            disabled={disabled}
            aria-pressed={duplex.active}
            title={duplex.active ? 'End the live conversation'
              : 'Start a live conversation (full duplex)'}
            className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors shrink-0
              disabled:opacity-40 disabled:cursor-not-allowed
              ${duplex.active ? 'text-brand bg-brand/10'
                : duplex.phase === 'connecting' ? 'text-brand animate-pulse'
                : 'text-p-text-secondary hover:text-brand hover:bg-brand/5'}`}
          >
            {/* phone handset */}
            <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
              <path d="M6.62 10.79a15.05 15.05 0 006.59 6.59l2.2-2.2a1 1 0 011.02-.24c1.12.37 2.33.57 3.57.57a1 1 0 011 1V20a1 1 0 01-1 1C10.85 21 3 13.15 3 3.5a1 1 0 011-1h3.5a1 1 0 011 1c0 1.24.2 2.45.57 3.57a1 1 0 01-.24 1.02l-2.21 2.7z" />
            </svg>
          </button>
        )}
      </div>

      {/* Mic — dictation, or the mute toggle while duplex is live. */}
      <button
        type="button"
        onClick={onMicClick}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        disabled={disabled}
        title={micTitle}
        aria-pressed={dupPhase ? dupMuted : undefined}
        className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors shrink-0 touch-none
          disabled:cursor-not-allowed ${micClass}`}
      >
        {dupPhase && dupMuted ? (
          // muted: the mic glyph with a slash — the conversation is live
          // but the engine hears nothing until unmuted.
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2"
               strokeLinecap="round" strokeLinejoin="round" viewBox="0 0 24 24">
            <path d="M9 9v3a3 3 0 005.12 2.12M15 9.34V5a3 3 0 00-5.94-.6" />
            <path d="M17 16.95A7 7 0 015 12v-2m14 0v2a7 7 0 01-.11 1.23" />
            <line x1="12" y1="19" x2="12" y2="22" />
            <line x1="2" y1="2" x2="22" y2="22" />
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
