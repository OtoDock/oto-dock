import { useRef, useCallback, useEffect } from 'react'
import { Capacitor } from '@capacitor/core'

type Severity = 'info' | 'success' | 'warning' | 'danger'

const SOUND_URLS: Record<string, string> = {
  success: '/sounds/success.wav',
  warning: '/sounds/warning.wav',
  danger: '/sounds/danger.wav',
  ping: '/sounds/ping.wav',
}

/**
 * Android WebView (and modern browsers) block Audio.play() unless
 * preceded by a user gesture. This flag tracks whether we've "unlocked"
 * audio by playing a silent buffer on first touch/click.
 */
let audioUnlocked = false
let audioContext: AudioContext | null = null

function unlockAudio() {
  if (audioUnlocked) return
  audioUnlocked = true

  // Create an AudioContext and play a silent buffer to unlock audio playback
  try {
    audioContext = new AudioContext()
    const buffer = audioContext.createBuffer(1, 1, 22050)
    const source = audioContext.createBufferSource()
    source.buffer = buffer
    source.connect(audioContext.destination)
    source.start(0)
  } catch { /* ignore */ }

  // Also trigger speechSynthesis unlock
  if ('speechSynthesis' in window) {
    const u = new SpeechSynthesisUtterance('')
    u.volume = 0
    window.speechSynthesis.speak(u)
  }
}

// Listen for first user interaction to unlock audio (once, globally)
if (typeof document !== 'undefined') {
  const events = ['touchstart', 'touchend', 'click', 'keydown']
  const handler = () => {
    unlockAudio()
    events.forEach(e => document.removeEventListener(e, handler, true))
  }
  events.forEach(e => document.addEventListener(e, handler, { capture: true, once: false }))
}

export function useNotificationSound() {
  const audioCache = useRef<Record<string, HTMLAudioElement>>({})
  const dangerLoop = useRef<{ stop: () => void } | null>(null)

  // Preload audio files
  useEffect(() => {
    for (const [key, url] of Object.entries(SOUND_URLS)) {
      const audio = new Audio(url)
      audio.preload = 'auto'
      audioCache.current[key] = audio
    }
  }, [])

  const playSound = useCallback((key: string) => {
    // Resume AudioContext if it was suspended (common after unlock)
    if (audioContext?.state === 'suspended') {
      audioContext.resume().catch(() => {})
    }

    const audio = audioCache.current[key]
    if (audio) {
      const clone = audio.cloneNode(true) as HTMLAudioElement
      clone.volume = key === 'ping' ? 0.3 : 0.7
      clone.play().catch(() => {})
    }
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

    switch (severity) {
      case 'info':
      case 'success':
        playSound('success')
        break
      case 'warning':
        playSound('warning')
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
