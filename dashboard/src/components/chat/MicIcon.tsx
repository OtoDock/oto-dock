// Mic icon — dictate into the chat input via the resolved STT backend (native
// Web Speech / Android plugin, or the platform WS). Renders nothing when chat
// STT is unavailable. Built on useSpeechSession (the shared speech lifecycle):
// a brief "connecting" spinner until the recognizer is genuinely ready (so the
// user doesn't speak into the connect gap), then a pulsing "recording" state.
// Errors show in a small popover (a hover title is invisible on touch). Used for
// plain dictation; the hands-free conversation UI lives in VoiceControl.

import { useRef, useState, useEffect } from 'react'
import { useSpeechSession } from '../../hooks/useSpeechSession'
import { MicGlyph } from './MicGlyph'

export function MicIcon({ onTranscript, onInterim, onActive, onCommit, disabled }: {
  onTranscript: (text: string) => void
  onInterim?: (text: string) => void       // live partial (replaces, not committed)
  onActive?: (active: boolean) => void      // recording start/stop (for base snapshot)
  onCommit?: () => void                     // clean end (silence/stop, not error)
  disabled?: boolean
}) {
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const errorTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (errorTimer.current) clearTimeout(errorTimer.current) }, [])

  const showError = (msg: string) => {
    setErrorMsg(msg)
    if (errorTimer.current) clearTimeout(errorTimer.current)
    errorTimer.current = setTimeout(() => setErrorMsg(null), 7000)
  }

  const speech = useSpeechSession({
    onInterim,
    onFinal: (t) => onTranscript(t),
    onActive,
    onCommit: onCommit ? () => onCommit() : undefined,
    onError: showError,
  })

  if (!speech.available) return null
  const recording = speech.status === 'recording'
  const connecting = speech.status === 'connecting'

  return (
    <div className="relative shrink-0">
      {errorMsg && (
        <div
          role="alert"
          onClick={() => setErrorMsg(null)}
          className="absolute bottom-full mb-2 left-0 z-50 w-60 px-3 py-2 rounded-lg cursor-pointer
            text-xs leading-snug bg-red-600 text-white shadow-lg"
        >
          {errorMsg}
        </div>
      )}
      <button
        type="button"
        onClick={() => { setErrorMsg(null); speech.toggle() }}
        disabled={disabled}
        title={errorMsg ? errorMsg : connecting ? 'Connecting…' : recording ? 'Stop dictation' : 'Dictate'}
        className={`w-9 h-9 rounded-lg flex items-center justify-center transition-colors
          disabled:opacity-40 disabled:cursor-not-allowed
          ${recording ? 'text-red-500 bg-red-500/10 animate-pulse motion-reduce:animate-none'
            : connecting ? 'text-brand'
            : errorMsg ? 'text-red-400 hover:text-red-500'
            : 'text-p-text-secondary hover:text-brand hover:bg-brand/5 active:scale-95'}`}
      >
        {connecting ? (
          <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
        ) : (
          <MicGlyph filled={recording} />
        )}
      </button>
    </div>
  )
}
