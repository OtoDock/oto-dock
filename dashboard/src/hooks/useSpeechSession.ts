// STT session lifecycle — the shared speech-recognition state machine behind the
// dictation button (MicIcon) and the voice-conversation loop (VoiceControl).
// Owns: backend resolution, the idle→connecting→recording states, the
// connecting floor + timeout (so the user doesn't speak into the connect gap),
// error handling, a finals accumulator, and a once-per-session clean-end commit
// (onCommit fires on silence/stop with the full transcript, NEVER on error).

import { useCallback, useRef, useState } from 'react'
import { useChatAudioCapability } from './useChatAudioCapability'
import { useMyAudioPrefs } from '../api/userAudio'
import { resolveSttBackend } from '../audio/resolver'
import { platformStt } from '../audio/backends/platformStt'
import { type STTBackend, type STTSession } from '../audio/types'

export type SpeechStatus = 'idle' | 'connecting' | 'recording'

const MIN_CONNECTING_MS = 500    // floor so the spinner doesn't flicker + gives the user a beat
const CONNECT_TIMEOUT_MS = 8000  // give up if the recognizer never signals ready

// Default STT language from the browser/OS when the user hasn't picked one — the
// closest the web exposes to "keyboard language" (no API gives the active layout).
function browserSttLang(): string {
  try {
    const base = (navigator.language || 'en').slice(0, 2).toLowerCase()
    return ['en', 'el', 'de', 'es', 'fr', 'it'].includes(base) ? base : 'en'
  } catch { return 'en' }
}

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))
function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    p,
    sleep(ms).then(() => { throw new Error('Timed out connecting to the speech service.') }),
  ]) as Promise<T>
}

export interface SpeechHandlers {
  onInterim?: (text: string) => void   // live partial (replaces, not committed)
  onFinal?: (text: string) => void     // a finalized phrase
  onCommit?: (text: string) => void    // clean end (silence/stop): full accumulated transcript
  onActive?: (active: boolean) => void // recording start/stop
  onError?: (message: string) => void
}

export interface SpeechSession {
  status: SpeechStatus
  available: boolean
  start: () => Promise<void>
  stop: () => void
  toggle: () => void
}

export function useSpeechSession(handlers: SpeechHandlers): SpeechSession {
  const { data: cap } = useChatAudioCapability()
  const { data: prefs } = useMyAudioPrefs()
  const [status, setStatus] = useState<SpeechStatus>('idle')

  const sessionRef = useRef<STTSession | null>(null)
  const erroredRef = useRef(false)     // this session hit an error → suppress onCommit
  const committedRef = useRef(false)   // onCommit fired once per session
  const finalsRef = useRef('')         // accumulated finals for onCommit(text)
  // Handlers may change every render; keep a live ref so the once-set session
  // callbacks always call the latest (avoids stale closures).
  const hRef = useRef(handlers)
  hRef.current = handlers

  const available = !!cap && cap.stt !== 'unavailable' && !!resolveSttBackend(cap, prefs?.stt_mode ?? 'auto')

  const stop = useCallback(() => {
    const s = sessionRef.current
    sessionRef.current = null
    setStatus('idle')
    hRef.current.onActive?.(false)
    s?.stop().catch(() => {})
  }, [])

  const start = useCallback(async () => {
    if (sessionRef.current) return
    const backend = cap ? resolveSttBackend(cap, prefs?.stt_mode ?? 'auto') : null
    if (!backend || !cap) { hRef.current.onError?.('Microphone unavailable.'); return }

    setStatus('connecting')
    erroredRef.current = false
    committedRef.current = false
    finalsRef.current = ''
    hRef.current.onActive?.(true)

    const fireCommit = () => {
      if (erroredRef.current || committedRef.current) return
      committedRef.current = true
      hRef.current.onCommit?.(finalsRef.current.trim())
    }

    const runWith = async (b: STTBackend) => {
      const session = b.create({ language: prefs?.stt_language || browserSttLang(), providerId: cap.stt_provider_id })
      session.onPartial((t) => { if (t) hRef.current.onInterim?.(t) })
      session.onFinal((t) => {
        const v = t.trim()
        if (!v) return
        finalsRef.current = finalsRef.current ? `${finalsRef.current} ${v}` : v
        hRef.current.onFinal?.(v)
      })
      session.onError((e) => {
        erroredRef.current = true
        hRef.current.onError?.(e?.message || 'Microphone error.')
        setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
      })
      // Clean end (silence or user-stop, never an error).
      session.onEnd(() => {
        setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
        fireCommit()
      })
      sessionRef.current = session
      // Hold "connecting" until the recognizer is genuinely ready (session.start()
      // resolves on the real ready event for the platform WS) AND a minimum beat.
      await Promise.all([withTimeout(session.start(), CONNECT_TIMEOUT_MS), sleep(MIN_CONNECTING_MS)])
      if (sessionRef.current === session) setStatus('recording')  // unless stopped meanwhile
    }

    try {
      await runWith(backend)
    } catch (e) {
      // Native start failed past the availability probe (recognizer service
      // flaked mid-click): under mode 'auto' on an 'either' capability, retry
      // ONCE on the platform engine instead of leaving a dead mic. An
      // explicit 'native' pick keeps its honest error.
      const mode = prefs?.stt_mode ?? 'auto'
      if (backend.kind === 'native' && mode === 'auto' && cap.stt === 'either'
          && platformStt.isAvailable() && !sessionRef.current) {
        try {
          console.warn('native STT start failed — falling back to platform:', e)
          await runWith(platformStt)
          return
        } catch { /* fall through to the shared error path */ }
      }
      hRef.current.onError?.(e instanceof Error ? e.message : 'Microphone unavailable.')
      setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
    }
  }, [cap, prefs])

  const toggle = useCallback(() => {
    if (sessionRef.current || status !== 'idle') stop()
    else void start()
  }, [status, start, stop])

  return { status, available, start, stop, toggle }
}
