// STT session lifecycle — the shared speech-recognition state machine behind the
// dictation button (MicIcon) and the voice-conversation loop (VoiceControl).
// Owns: backend resolution, the idle→connecting→recording states, the
// connecting floor + timeout (so the user doesn't speak into the connect gap),
// error handling, a finals accumulator, and a once-per-session clean-end commit
// (onCommit fires on silence/stop with the full transcript, NEVER on error).

import { useCallback, useEffect, useRef, useState, useLayoutEffect } from 'react'
import { useChatAudioCapability } from './useChatAudioCapability'
import { useMyAudioPrefs } from '../api/userAudio'
import { acquireMic, releaseMic } from '../audio/micCoordinator'
import { setDictationActive, stopActivePlayback } from '../audio/speechActivity'
import { browserSttLang, deviceLanguage, isAutoLanguage } from '../audio/lang'
import { resolveSttBackend } from '../audio/resolver'
import { platformStt, STOP_BACKLOG_MAX_MS, STOP_FINAL_WAIT_MS } from '../audio/backends/platformStt'
import { nativeStt } from '../audio/backends/nativeStt'
import {
  DICTATION_DEVICE_FALLBACK, DICTATION_FINISHING, DICTATION_IDLE, DICTATION_NO_MIC, DICTATION_NOT_ALLOWED,
} from '../audio/backends/dictationWords'
import { type STTBackend, type STTSession, isNativePlatform } from '../audio/types'
import { debugLog, debugWarn } from '../lib/debug/log'

// 'finishing': the mic is off and the last words are still on the way (the
// person stopped and the server's last final has not landed yet, or the
// server stopped the session on its own).
export type SpeechStatus = 'idle' | 'connecting' | 'recording' | 'finishing'

// The longest a stop waits for its last words (the platform engine's bound
// with a slow uplink's backlog): a start after a stop waits at most this.
const STOP_WAIT_MAX_MS = STOP_FINAL_WAIT_MS + STOP_BACKLOG_MAX_MS

const MIN_CONNECTING_MS = 500    // floor so the spinner doesn't flicker + gives the user a beat
const CONNECT_TIMEOUT_MS = 8000  // give up if the recognizer never signals ready
// The device session may try the on-device recognizer first (its check, a
// bounded start that may wait on a model download, the cut's wait) before
// the online one takes the press: about 11 s at worst with the "already
// running" retry's second wait, plus the plugin's round trips.
const DEVICE_CONNECT_TIMEOUT_MS = 12_000

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
  onReplace?: (text: string) => void   // the language switched: the session's words so far are this text
  onCommit?: (text: string) => void    // clean end (silence/stop): full accumulated transcript
  onActive?: (active: boolean) => void // recording start/stop
  onError?: (message: string) => void
  onNotice?: (message: string | null) => void // a passing state worth a sentence (a slow link), null when over
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
  const { data: cap, refetch: refetchCapability } = useChatAudioCapability()
  const { data: prefs } = useMyAudioPrefs()
  const [status, setStatus] = useState<SpeechStatus>('idle')

  const sessionRef = useRef<STTSession | null>(null)
  // The previous attempt's stop while it still waits for its last words,
  // and the attempt counter a stop bumps (a start waiting on that stop is
  // abandoned when the person stops again meanwhile).
  const stoppingRef = useRef<Promise<void> | null>(null)
  const attemptRef = useRef(0)
  // The attempt whose plain stop is still finishing: another plain stop with
  // nothing live (a focus while the last words are on the way) leaves it be.
  const finishingRef = useRef(-1)
  // The session that reached recording (a stop before it owes no words).
  const liveRef = useRef<STTSession | null>(null)
  // Kill switch for the CURRENT attempt's closure-local `dead` flag — set per
  // runWith invocation so stop(discardTail=true) can reach it.
  const killRef = useRef<(() => void) | null>(null)
  // Handlers may change every render; keep a live ref so the once-set session
  // callbacks always call the latest (avoids stale closures).
  const hRef = useRef(handlers)
  useLayoutEffect(() => { hRef.current = handlers })

  // Before the server's answer the microphone shows wherever a device
  // recognizer is present (the browser's own, or the app's plugin), so a
  // chat opens with it; a tap before the answer waits for it in start().
  const available = cap
    ? cap.stt !== 'unavailable' && !!resolveSttBackend(cap, prefs?.stt_mode ?? 'auto', prefs?.stt_language)
    : nativeStt.isAvailable()

  // Mic arbiter: dictation (platform OR native backend) holds the mic — the
  // ambient wake-word listener must release it while we record. Status-driven
  // so every start/stop/error path is covered without touching each one.
  useEffect(() => {
    if (status !== 'idle') acquireMic('dictation')
    else releaseMic('dictation')
    // Dictation-active interlock flag (chime suppression) — status-driven
    // for the same every-path coverage as the mic arbiter.
    setDictationActive(status !== 'idle')
  }, [status])

  const stop = useCallback((discardTail = false) => {
    if (discardTail) killRef.current?.()
    if (!discardTail && !sessionRef.current && finishingRef.current === attemptRef.current) return
    const attempt = ++attemptRef.current
    const s = sessionRef.current
    sessionRef.current = null
    // The composer stops treating edits as dictation at once: a late final
    // lands after what was typed (kept dictating, the interim would join the
    // base and the final would double it).
    hRef.current.onActive?.(false)
    const stopped = s ? s.stop().catch(() => {}) : null
    // A plain stop keeps delivering its last words: the next attempt waits
    // for them (a discarded one drops them, nothing to wait for), and a
    // session that was recording says so until they land (bounded).
    const owes = !!stopped && !discardTail && s === liveRef.current
    if (stopped && !discardTail) {
      stoppingRef.current = stopped
      void Promise.race([stopped, sleep(STOP_WAIT_MAX_MS)]).then(() => {
        if (stoppingRef.current === stopped) stoppingRef.current = null
        if (owes && attemptRef.current === attempt) { setStatus('idle'); hRef.current.onNotice?.(null) }
      })
    }
    if (owes) {
      finishingRef.current = attempt
      setStatus('finishing')
      hRef.current.onNotice?.(DICTATION_FINISHING)
    } else {
      setStatus('idle')
      hRef.current.onNotice?.(null)
    }
    liveRef.current = null
  }, [])

  // Unmount stops a running session too (a plain stop: its tail still lands
  // through the handlers in the draft it was dictated into; a user-stopped
  // attempt has already cleared sessionRef and is untouched).
  useEffect(() => () => { stop(); releaseMic('dictation'); setDictationActive(false) }, [stop])

  const start = useCallback(async () => {
    if (sessionRef.current) return
    let capability = cap
    if (!capability) {
      // A tap before the capability answered: fetch it now (a stop meanwhile
      // bumps the attempt and abandons the wait).
      setStatus('connecting')
      const waiting = ++attemptRef.current
      capability = await refetchCapability().then((r) => r.data, () => undefined)
      if (attemptRef.current !== waiting) return
    }
    const backend = capability ? resolveSttBackend(capability, prefs?.stt_mode ?? 'auto', prefs?.stt_language) : null
    if (!backend || !capability) { hRef.current.onError?.('Microphone unavailable.'); setStatus('idle'); return }
    const resolved = capability

    // Dictation captures WITHOUT echo cancellation (see platformStt) — any
    // in-flight one-shot TTS replay would transcribe itself into the
    // composer. Silence it before the mic opens.
    stopActivePlayback()

    setStatus('connecting')
    // The previous attempt's last words land before this one snapshots the
    // composer: a quick tap after a stop would otherwise put the words it
    // was still showing in the box twice.
    const attempt = ++attemptRef.current
    const previous = stoppingRef.current
    if (previous) {
      await Promise.race([previous, sleep(STOP_WAIT_MAX_MS)])
      if (attemptRef.current !== attempt) return
    }
    hRef.current.onNotice?.(null)
    hRef.current.onActive?.(true)
    const mode = prefs?.stt_mode ?? 'auto'
    // The person's device pick cannot be honoured (no device recognizer on
    // this device, or its probe failed): the platform listens, said once.
    // Under Platform only the policy gave the platform, not the device.
    if (mode === 'native' && backend === platformStt && !nativeStt.isAvailable()) {
      hRef.current.onNotice?.(DICTATION_DEVICE_FALLBACK)
    }

    // PER-ATTEMPT state, deliberately closure-local: a stopped session's
    // events land LATE (the server's stop flush takes ~1.5s before its socket
    // closes), often while the NEXT session is already connecting. Hook-level
    // accumulators let the stale session commit/poison the new one's state.
    let finals = ''       // accumulated finals for onCommit(text)
    let committed = false // onCommit fired once per attempt
    let errored = false   // attempt hit an error → suppress onCommit
    let heard = false     // a partial or a final landed in this attempt
    let fellBack = false  // the platform took over once

    const fireCommit = () => {
      if (errored || committed) return
      committed = true
      hRef.current.onCommit?.(finals.trim())
    }

    // The device recognizer failed before a word (its start, or an error
    // right after): under mode auto on an either capability the platform
    // takes over once, inside the same attempt (the composer's base stays),
    // with the notice. Not for a failure the platform shares (no microphone,
    // the microphone refused), nor for a minute of silence.
    const canFallBack = (message: string) =>
      !fellBack && mode === 'auto' && resolved.stt === 'either' && platformStt.isAvailable()
      && message !== DICTATION_NO_MIC && message !== DICTATION_NOT_ALLOWED && message !== DICTATION_IDLE
    const fallBack = async (why: unknown) => {
      fellBack = true
      debugWarn('device dictation failed before a word, the platform takes over:', why)
      setStatus('connecting')
      hRef.current.onNotice?.(DICTATION_DEVICE_FALLBACK)
      await runWith(platformStt)
    }

    const runWith = async (b: STTBackend) => {
      // No pick, or Automatic, on the device recognizer: the device's own
      // language, the whole tag (WebKit refuses a bare code as a locale).
      // No pick on the platform: the browser's base code; Automatic there:
      // the platform engine identifies the language.
      const stored = prefs?.stt_language
      const language = b.kind === 'native' && (!stored || isAutoLanguage(stored)) ? deviceLanguage()
        : !stored ? browserSttLang() : stored
      debugLog(`[stt] dictation on ${b.kind === 'native' ? 'this device' : 'the platform'} in ${language}`)
      const session = b.create({ language, providerId: resolved.stt_provider_id })
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
      session.onPartial((t) => { if (!dead && t) { heard = true; hRef.current.onInterim?.(t) } })
      session.onFinal((t) => {
        if (dead) return
        const v = t.trim()
        if (!v) return
        heard = true
        finals = finals ? `${finals} ${v}` : v
        hRef.current.onFinal?.(v)
      })
      session.onReplace?.((t) => {
        if (dead) return
        finals = t.trim()
        hRef.current.onReplace?.(finals)
      })
      session.onNotice?.((t) => {
        if (!dead && sessionRef.current === session) hRef.current.onNotice?.(t)
      })
      session.onFinishing?.(() => {
        if (!dead && sessionRef.current === session) setStatus('finishing')
      })
      session.onError((e) => {
        if (dead) return // the connect failure was already surfaced
        // A stopped attempt's late error (its last words never arrived)
        // must not land over the next recording.
        if (sessionRef.current && sessionRef.current !== session) return
        const message = e?.message || 'Microphone error.'
        if (b.kind === 'native' && !heard && sessionRef.current === session && canFallBack(message)) {
          dead = true   // the device session's end must not commit the attempt
          session.stop().catch(() => {})
          sessionRef.current = null
          void fallBack(e).catch((err) => {
            hRef.current.onError?.(err instanceof Error ? err.message : 'Microphone unavailable.')
            setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
          })
          return
        }
        errored = true
        hRef.current.onError?.(message)
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
        const connect = b.kind === 'native' && isNativePlatform() ? DEVICE_CONNECT_TIMEOUT_MS : CONNECT_TIMEOUT_MS
        await Promise.all([withTimeout(session.start(), connect), sleep(MIN_CONNECTING_MS)])
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
      liveRef.current = session
      setStatus('recording')
    }

    try {
      await runWith(backend)
    } catch (e) {
      // The device start failed past the availability probe (the recognizer
      // refused, the service flaked mid-click): the platform takes over
      // once instead of leaving a dead mic. An explicit device pick keeps
      // its honest error.
      if (backend.kind === 'native' && !sessionRef.current && canFallBack(e instanceof Error ? e.message : '')) {
        try {
          await fallBack(e)
          return
        } catch { /* fall through to the shared error path */ }
      }
      hRef.current.onError?.(e instanceof Error ? e.message : 'Microphone unavailable.')
      setStatus('idle'); hRef.current.onActive?.(false); sessionRef.current = null
    }
  }, [cap, prefs, refetchCapability])

  // A tap while a stopped dictation finishes starts the next one (it waits
  // for those last words); a tap on a live or connecting session stops it.
  const toggle = useCallback(() => {
    if (sessionRef.current || status === 'recording' || status === 'connecting') stop()
    else void start()
  }, [status, start, stop])

  return { status, available, start, stop, toggle }
}
