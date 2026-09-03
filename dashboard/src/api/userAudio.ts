// Per-user audio preferences — chat sound (TTS) / mic (STT) icon behaviour.
// Consumed by the chat audio UI; the hooks ship now alongside the backend.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

export type AudioMode = 'native' | 'platform' | 'auto'

export interface VoicePref {
  source: 'native' | 'platform'
  voiceId: string
}

export interface AudioPrefs {
  user_sub?: string
  stt_mode: AudioMode
  tts_mode: AudioMode
  tts_voice_map: Record<string, VoicePref>
  stt_language: string | null
  // Wake word ("hey <agent>") opt-in — privacy default OFF; the app-level
  // listener (useWakeWord) only ever opens the mic when this is true.
  wake_word_enabled?: boolean
  updated_at?: string | null
}

export type AudioPrefsUpdate = Partial<Pick<AudioPrefs, 'stt_mode' | 'tts_mode' | 'tts_voice_map' | 'stt_language' | 'wake_word_enabled'>>

export function useMyAudioPrefs(enabled = true) {
  // `enabled=false` for pre-auth mounts (useWakeWord under RequireAuth): an
  // unauthenticated fetch 401s and apiFetch's expired-session redirect then
  // reloads the login page in a loop.
  return useQuery({
    queryKey: ['my-audio-prefs'],
    enabled,
    queryFn: async (): Promise<AudioPrefs> => {
      const res = await apiFetch('/v1/users/me/audio-prefs')
      if (!res.ok) throw new Error('Failed to fetch audio preferences')
      return res.json()
    },
  })
}

export function useUpdateMyAudioPrefs() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: AudioPrefsUpdate) => {
      const res = await apiFetch('/v1/users/me/audio-prefs', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to save audio preferences')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['my-audio-prefs'] }) },
  })
}
