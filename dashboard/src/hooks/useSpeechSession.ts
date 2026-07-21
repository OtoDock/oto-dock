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
  // stop(true) discards the tail: late finals/partials from the server's stop
  // flush are dropped instead of delivered (Send already consumed the text —
  // a delivered tail would re-fill the cleared input). Plain stop() keeps
  // today's tail delivery (mic-button/focus stops WANT the last phrase).
  stop: (discardTail?: boolean) => void
  toggle: () => void
}

export function useSpeechSession(handlers: SpeechHandlers): SpeechSession {
  const { data: cap } = useChatAudioCapability()
  const { data: prefs } = useMyAudioPrefs()
  const [status, setStatus] = useState<SpeechStatus>('idle')

  const sessionRef = useRef<STTSession | null>(null)
  // Kill switch for the CURRENT attempt's closure-local `dead` flag — set per
  // runWith invocation so stop(discardTail=true) can reach it.
  const killRef = useRef<(() => void) | null>(null)
  // Handlers may change every render; keep a live ref so the once-set session
  // callbacks always call the latest (avoids stale closures).
  const hRef = useRef(handlers)
  hRef.current = handlers

  const available = !!cap && cap.stt !== 'unavailable' && !!resolveSttBackend(cap, prefs?.stt_mode ?? 'auto')

  const stop = useCallback((discardTail = false) => {
    if (discardTail) killRef.current?.()
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
    hRef.current.onActive?.(true)

    // PER-ATTEMPT state, deliberately closure-local: a stopped session's
    // events land LATE (the server's stop flush takes ~1.5s before its socket
    // closes), often while the NEXT session is already connecting. Hook-level
    // accumulators let the stale session commit/poison the new one's state.
    let finals = ''       // accumulated finals for onCommit(text)
    let committed = false // onCommit fired once per attempt
    let errored = false   // attempt hit an error → suppress onCommit

    const fireCommit = () => {
      if (errored || committed) return
      committed = true
      hRef.current.onCommit?.(finals.trim())
    }

    const runWith = async (b: STTBackend) => {
      const session = b.create({ language: prefs?.stt_language || browserSttLang(), providerId: cap.stt_provider_id })
      // Set when start() fails or times out. withTimeout REJECTS but the
      // underlying start keeps running — without this flag (and the stop()
      // in the catch) a slow platform connect leaves a ZOMBIE session whose
      // late 'ready' attaches the mic pump and keeps feeding these handlers.
      // The user retries the mic, and two live sessions transcribe the same
      // microphone: every finalized sentence lands in the composer twice
      // (live repro 2026-07-16: paired 60s max_seconds usage records).
      // Note: a session stopped by the USER stays undead on purpose — its
      // tail final (the server's stop flush) must still be delivered.
      // stop(true) is the exception: Send already took the text, so the
      // kill switch marks the attempt dead and the tail is dropped.
      let dead = false
      killRef.current = () => { dead = true }
      session.onPartial((t) => { if (!dead && t) hRef.current.onInterim?.(t) })
      session.onFinal((t) => {
        if (dead) return
        const v = t.trim()
        if (!v) return
        finals = finals ? `${finals} ${v}` : v
        hRef.current.onFinal?.(v)
      })
      session.onError((e) => {
        if (dead) return // the connect failure was already surfaced
        errored = true
        hRef.current.onError?.(e?.message || 'Microphone error.')
        // Only the CURRENT session may flip the hook's state — a stopped
        // session's late error must not knock out its successor.
        if (sessionRef.current === session) {
          setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
        }
      })
      // Clean end (silence or user-stop, never an error).
      session.onEnd(() => {
        if (dead) return
        // Same stale-guard: a stopped session's close event lands LATE (the
        // server stop-flush runs first) — often while the next session is
        // CONNECTING. Without the guard it reset status to idle and cleared
        // sessionRef, so the new session came up with a dead icon while
        // recording in the background (live repro 2026-07-16, third window).
        if (sessionRef.current === session) {
          setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
        }
        fireCommit()
      })
      sessionRef.current = session
      // Hold "connecting" until the recognizer is genuinely ready (session.start()
      // resolves on the real ready event for the platform WS) AND a minimum beat.
      try {
        await Promise.all([withTimeout(session.start(), CONNECT_TIMEOUT_MS), sleep(MIN_CONNECTING_MS)])
      } catch (e) {
        dead = true
        session.stop().catch(() => {}) // release the mic/socket the failed start may still hold
        if (sessionRef.current !== session) return // user already stopped it — nothing to report
        sessionRef.current = null
        throw e
      }
      if (sessionRef.current !== session) {
        // Detached (stopped) while connecting: that early stop() may have run
        // BEFORE start() had created the mic/socket, in which case its
        // teardown was a no-op and the session just came up in the background
        // — the second zombie window (live repro 2026-07-16: UI idle, mic
        // recording, duplicates stacking per retry). Kill it now that its
        // resources exist; the backend's own stopping-flag checkpoints make
        // this belt-and-braces.
        dead = true
        session.stop().catch(() => {})
        return
      }
      setStatus('recording')
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
