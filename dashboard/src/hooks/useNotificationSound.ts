import { useRef, useCallback, useEffect } from 'react'
import { Capacitor } from '@capacitor/core'
import { isDictationActive, isSpeaking } from '../audio/speechActivity'
import { isNativePlatform } from '../audio/types'

type Severity = 'info' | 'success' | 'warning' | 'danger'

// Each chime stays under five seconds on purpose: for a short file whose
// length is known as it starts, the WebView asks other apps only to lower
// their sound for its duration (Chromium's media session treats a known
// duration of at most 5 s as transient, may duck); a longer one, or one
// whose length is unknown as it starts, takes the audio focus outright and
// stops the car's music. notificationSound.test.ts reads the headers.
const SOUND_URLS: Record<string, string> = {
  success: '/sounds/success.wav',
  warning: '/sounds/warning.wav',
  danger: '/sounds/danger.wav',
  ping: '/sounds/ping.wav',
}

// Speech synthesis in a browser speaks on its own (the danger readout's web
// fallback) only once a user gesture let it: warmed with an empty utterance
// at the first tap or key. Inside an app shell the app speaks through its
// native plugin and plays media without a gesture, so nothing is warmed
// there, and the page opens no sound output until a sound plays.
let speechWarmed = false

function warmSpeechOnce() {
  if (speechWarmed) return
  speechWarmed = true
  if (isNativePlatform()) return
  if ('speechSynthesis' in window) {
    const u = new SpeechSynthesisUtterance('')
    u.volume = 0
    window.speechSynthesis.speak(u)
  }
}

if (typeof document !== 'undefined') {
  const events = ['touchstart', 'touchend', 'click', 'keydown']
  const handler = () => {
    warmSpeechOnce()
    events.forEach(e => document.removeEventListener(e, handler, true))
  }
  events.forEach(e => document.addEventListener(e, handler, { capture: true, once: false }))
}

// The files, warmed into memory once per page when the page is idle: a chime
// then plays from a Blob URL, with no network between the moment it is asked
// for and the sound (the proxy serves the files with no-cache, so a media
// element's own load would revalidate first) and with its length known as it
// starts. Nothing is warmed on a media element: a preloaded element is a
// media pipeline the page keeps open from every start. Before the warm
// lands, or when it failed (offline, a sign-in redirect), the file's URL
// plays instead.
const warmed: Record<string, string> = {}
let warmStarted = false

function warmSounds() {
  if (warmStarted || typeof fetch !== 'function' || typeof URL.createObjectURL !== 'function') return
  warmStarted = true
  const run = () => {
    for (const [key, url] of Object.entries(SOUND_URLS)) {
      fetch(url)
        .then(r => (r.ok && (r.headers.get('content-type') || '').startsWith('audio/')
          ? r.blob()
          : Promise.reject(new Error(`not a sound: ${r.status}`))))
        .then(b => { warmed[key] = URL.createObjectURL(b) })
        .catch(() => { /* the file's URL plays instead */ })
    }
  }
  if (typeof requestIdleCallback === 'function') requestIdleCallback(run, { timeout: 5000 })
  else setTimeout(run, 1000)
}

export function useNotificationSound() {
  const dangerLoop = useRef<{ stop: () => void } | null>(null)

  useEffect(() => {
    warmSounds()
  }, [])

  const playSound = useCallback((key: string) => {
    const url = SOUND_URLS[key]
    if (!url) return
    const audio = new Audio(warmed[key] ?? url)
    audio.volume = key === 'ping' ? 0.3 : 0.7
    audio.play().catch(() => {})
  }, [])

  const speak = useCallback(async (text: string) => {
    // Try Capacitor native TTS first (reliable on Android)
    try {
      if (Capacitor.isNativePlatform()) {
        const { TextToSpeech } = await import('@capacitor-community/text-to-speech')
        await TextToSpeech.speak({ text, rate: 0.9 })
        return
      }
    } catch { /* not native or plugin unavailable */ }

    // Fallback: Web Speech API (works in desktop browsers)
    if (!('speechSynthesis' in window)) return
    window.speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance(text)
    utterance.rate = 0.9
    utterance.volume = 0.8
    const voices = window.speechSynthesis.getVoices()
    const enVoice = voices.find(v => v.lang.startsWith('en'))
    if (enVoice) utterance.voice = enVoice
    window.speechSynthesis.speak(utterance)
  }, [])

  const playForSeverity = useCallback((severity: Severity, title: string, body: string) => {
    // Stop any existing danger loop
    if (dangerLoop.current) {
      dangerLoop.current.stop()
      dangerLoop.current = null
    }

    // A chime while a spoken reply plays or a dictation records lands in
    // the open mic (the turn-complete ping is held the same way); a danger
    // alarm is never held.
    const quiet = isSpeaking() || isDictationActive()
    switch (severity) {
      case 'info':
      case 'success':
        if (!quiet) playSound('success')
        break
      case 'warning':
        if (!quiet) playSound('warning')
        break
      case 'danger': {
        // Loop alarm + TTS until stopped
        let running = true
        const loop = async () => {
          while (running) {
            playSound('danger')
            await new Promise(r => setTimeout(r, 2500))
            if (!running) break
            speak(`${title}. ${body}`)
            await new Promise(r => setTimeout(r, 4000))
          }
        }
        loop()
        dangerLoop.current = {
          stop: () => {
            running = false
            window.speechSynthesis?.cancel()
          },
        }
        break
      }
    }
  }, [playSound, speak])

  const stopDangerAlarm = useCallback(async () => {
    if (dangerLoop.current) {
      dangerLoop.current.stop()
      dangerLoop.current = null
    }
    window.speechSynthesis?.cancel()
    // Also stop Capacitor native TTS
    try {
      if (Capacitor.isNativePlatform()) {
        const { TextToSpeech } = await import('@capacitor-community/text-to-speech')
        await TextToSpeech.stop()
      }
    } catch { /* ignore */ }
  }, [])

  const playPing = useCallback(() => playSound('ping'), [playSound])

  return { playForSeverity, stopDangerAlarm, playPing }
}
