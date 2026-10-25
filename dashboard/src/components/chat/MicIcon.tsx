// Mic icon — dictate into the chat input via the resolved STT backend (native
// Web Speech / Android plugin, or the platform WS). Renders nothing when chat
// STT is unavailable. Built on useSpeechSession (the shared speech lifecycle):
// a brief "connecting" spinner until the recognizer is genuinely ready (so the
// user doesn't speak into the connect gap), then a pulsing "recording" state.
// Errors show in a small popover (a hover title is invisible on touch) and stay
// until tapped: a dictation that stopped on its own must not pass unseen. A
// slow link shows a quieter one while recording. Used for plain dictation;
// the hands-free conversation UI lives in VoiceControl.

import { useRef, useState, useEffect } from 'react'
import { useSpeechSession } from '../../hooks/useSpeechSession'
import { MicGlyph } from './MicGlyph'
import { DictationSentence } from './VoiceControl'

export function MicIcon({ onTranscript, onInterim, onReplace, onActive, onCommit, discardSignal, disabled, sentenceSlot }: {
  onTranscript: (text: string) => void
  onInterim?: (text: string) => void       // live partial (replaces, not committed)
  onReplace?: (text: string) => void       // the language switched: the session's words so far
  onActive?: (active: boolean) => void      // recording start/stop (for base snapshot)
  onCommit?: () => void                     // clean end (silence/stop, not error)
  discardSignal?: number                    // bumped on send / chat switch → stop the mic, DROP the tail
  disabled?: boolean
  sentenceSlot?: HTMLElement | null         // the composer's row above the pill (VoiceControl's prop)
}) {
  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [noticeMsg, setNoticeMsg] = useState<string | null>(null)
  // A send or a chat switch clears the sentence with the mic.
  const [seenDiscard, setSeenDiscard] = useState(discardSignal)
  if (seenDiscard !== discardSignal) {
    setSeenDiscard(discardSignal)
    setErrorMsg(null)
    setNoticeMsg(null)
  }

  const speech = useSpeechSession({
    onInterim,
    onFinal: (t) => onTranscript(t),
    onReplace,
    onActive,
    onCommit: onCommit ? () => onCommit() : undefined,
    onError: setErrorMsg,
    onNotice: setNoticeMsg,
  })

  // A send or a chat switch closes the mic and drops its tail (VoiceControl
  // does the same). Compared against the previous value, so the mount run
  // and StrictMode's second run are no-ops.
  const prevDiscardRef = useRef(discardSignal)
  useEffect(() => {
    if (prevDiscardRef.current === discardSignal) return
    prevDiscardRef.current = discardSignal
    speech.stop(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [discardSignal])

  if (!speech.available) return null
  const recording = speech.status === 'recording'
  const connecting = speech.status === 'connecting' || speech.status === 'finishing'

  return (
    <div className="relative shrink-0">
      <DictationSentence
        slot={sentenceSlot}
        notice={recording || connecting ? noticeMsg : null}
        error={errorMsg}
        onDismiss={() => setErrorMsg(null)}
      />
      <button
        type="button"
        onClick={() => { setErrorMsg(null); speech.toggle() }}
        disabled={disabled}
        title={errorMsg ? errorMsg : speech.status === 'finishing' ? 'Finishing…' : connecting ? 'Connecting…'
          : recording ? 'Stop dictation' : 'Dictate'}
        className={`w-9 h-9 rounded-control flex items-center justify-center transition-colors
          disabled:opacity-40 disabled:cursor-not-allowed
          ${recording ? 'text-status-danger-fg bg-status-danger-bg animate-pulse motion-reduce:animate-none'
            : connecting ? 'text-link'
            : errorMsg ? 'text-status-danger-fg hover:text-status-danger-fg'
            : 'text-secondary hover:text-primary hover:bg-surface-hover active:scale-95'}`}
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
