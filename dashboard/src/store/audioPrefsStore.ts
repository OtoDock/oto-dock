// Device-local chat-audio preferences. The cross-device prefs (stt/tts mode,
// language) live server-side in user_audio_prefs (api/userAudio). What lives
// HERE is genuinely device-specific: which browser/native voice to use per
// language (the available native voices differ per device) and whether the
// sound icon should auto-play. Persisted to localStorage with the same
// __user_sub wipe-on-mismatch pattern as agentPrefsStore.

import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

interface AudioPrefsState {
  nativeVoiceURI: Record<string, string>   // language → SpeechSynthesisVoice.voiceURI
  autoplayTts: boolean                      // sound icon auto-plays new assistant messages
  voiceModeEnabled: boolean                 // hands-free: speak the reply aloud as it streams
  __user_sub: string

  setNativeVoice: (language: string, voiceURI: string) => void
  setAutoplayTts: (on: boolean) => void
  setVoiceModeEnabled: (on: boolean) => void
  setUserSub: (userSub: string) => void
  reset: () => void
}

export const useAudioPrefsStore = create<AudioPrefsState>()(
  persist(
    (set) => ({
      nativeVoiceURI: {},
      autoplayTts: false,
      voiceModeEnabled: false,
      __user_sub: '',

      setNativeVoice: (language, voiceURI) =>
        set((s) => ({ nativeVoiceURI: { ...s.nativeVoiceURI, [language]: voiceURI } })),

      setAutoplayTts: (on) => set({ autoplayTts: on }),

      setVoiceModeEnabled: (on) => set({ voiceModeEnabled: on }),

      setUserSub: (userSub) => set({ __user_sub: userSub }),

      reset: () => set({ nativeVoiceURI: {}, autoplayTts: false, voiceModeEnabled: false, __user_sub: '' }),
    }),
    {
      name: 'oto-dock-audio-prefs',
      storage: createJSONStorage(() => localStorage),
    },
  ),
)

// Wipe local audio prefs when the booting user differs from the persisted one
// (shared device). Call once after the user resolves (AuthContext-level).
export function migrateAudioPrefsToUser(userSub: string) {
  if (!userSub) return
  const state = useAudioPrefsStore.getState()
  if (state.__user_sub && state.__user_sub !== userSub) {
    useAudioPrefsStore.getState().reset()
  }
  useAudioPrefsStore.getState().setUserSub(userSub)
}
