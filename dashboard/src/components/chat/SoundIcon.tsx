// Sound icon — play an assistant message aloud via the resolved TTS backend
// (native or platform). Renders nothing when chat TTS is unavailable for this
// device/policy. Mounts in the assistant message footer next to copy.

import { useState, useEffect, useRef } from 'react'
import { useChatAudioCapability } from '../../hooks/useChatAudioCapability'
import { useMyAudioPrefs } from '../../api/userAudio'
import { useAudioPrefsStore } from '../../store/audioPrefsStore'
import { resolveTtsBackend } from '../../audio/resolver'
import { detectTtsLanguage } from '../../audio/lang'
import { type TTSBackend } from '../../audio/types'
import { cleanForSpeech } from '../../audio/cleanText'
import { registerStoppablePlayback, setSpeaking } from '../../audio/speechActivity'

export function SoundIcon({ text }: { text: string }) {
  const { data: cap } = useChatAudioCapability()
  const { data: prefs } = useMyAudioPrefs()
  const nativeVoiceURI = useAudioPrefsStore(s => s.nativeVoiceURI)
  const [playing, setPlaying] = useState(false)
  const activeBackend = useRef<TTSBackend | null>(null)
  const clearSpeakingRef = useRef<() => void>(() => {})

  // Stop playback when this message unmounts (switching chats / navigating
  // away) — otherwise the TTS keeps talking on the next page. Also clear
  // the speech-activity flag: play() is not guaranteed to settle after a
  // mid-play stop, and a wedged flag would defer heavy work to its cap.
  useEffect(() => () => {
    activeBackend.current?.stop()
    activeBackend.current = null
    clearSpeakingRef.current()
  }, [])

  const spoken = cleanForSpeech(text)
  if (!cap || cap.tts === 'unavailable' || !spoken) return null
  const backend = resolveTtsBackend(cap, prefs?.tts_mode ?? 'auto')
  if (!backend) return null

  // TTS language is the MESSAGE's language (not the dictation pref) — detected
  // (Greek by script + Latin stopwords) so de/es/fr/it pick the right voice and
  // the platform endpoint pronounces the right language.
  const language = detectTtsLanguage(spoken)

  const onClick = async () => {
    if (playing) {
      backend.stop()
      activeBackend.current = null
      clearSpeakingRef.current()
      setPlaying(false)
      return
    }
    setPlaying(true)
    activeBackend.current = backend
    // Speech-activity flag around the whole play (covers native AND
    // platform backends): heavy turn-end work defers while this speaks.
    // Idempotent clear shared with the unmount cleanup — the counted flag
    // must decrement exactly once per play, whichever path ends it.
    let cleared = false
    const clearSpeaking = () => {
      if (!cleared) { cleared = true; setSpeaking(false) }
    }
    clearSpeakingRef.current = clearSpeaking
    setSpeaking(true)
    // Dictation interlock: a dictation session starting mid-play silences
    // this replay (its capture runs without AEC — the replay would
    // transcribe itself into the composer).
    const unregister = registerStoppablePlayback(() => {
      backend.stop()
      activeBackend.current = null
      clearSpeaking()
      setPlaying(false)
    })
    try {
      await backend.play(spoken, {
        language,
        voiceURI: nativeVoiceURI[language],
        providerId: cap.tts_provider_id,
      })
    } catch { /* ignore */ } finally {
      unregister()
      clearSpeaking()
      setPlaying(false)
      activeBackend.current = null
    }
  }

  return (
    <button
      onClick={onClick}
      title={playing ? 'Stop' : 'Play aloud'}
      className="p-1 rounded-sm text-p-text-light hover:text-brand transition-colors"
    >
      {playing ? (
        <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24">
          <rect x="6" y="6" width="12" height="12" rx="1" />
        </svg>
      ) : (
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M15.536 8.464a5 5 0 010 7.072M17.95 6.05a8 8 0 010 11.9M5 9v6h4l5 4V5L9 9H5z" />
        </svg>
      )}
    </button>
  )
}
