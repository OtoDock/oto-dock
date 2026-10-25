// Audio API hooks — STT/TTS providers, chat policy, turn classifier, settings.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiJson, withMessage } from '../lib/http/apiJson'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AudioProvider {
  id: number
  provider_type: 'stt' | 'tts'
  provider_name: string
  label: string
  credential_key: string | null
  enabled_for_calls: boolean
  enabled_for_chat: boolean
  is_default_calls: boolean
  is_default_chat: boolean
  voices: Record<string, string>
  advanced: Record<string, unknown>
  credential_configured: boolean
  /** The engine's advanced-settings defaults (server-provided; drives which
   *  advanced fields render + the Restore button). */
  advanced_defaults: Record<string, unknown>
  /** The model ids that run when the row stores none (placeholders; never
   *  stored, so a row follows a later default). */
  model_defaults?: Record<string, string>
  /** The voices the engine ships per language (placeholders; the admin's
   *  voice for a language wins over the shipped one). */
  voice_defaults?: Record<string, string>
}

export type AudioProviderCreate = {
  provider_type: 'stt' | 'tts'
  provider_name: string
  label?: string
  credential_key?: string | null
  enabled_for_calls?: boolean
  enabled_for_chat?: boolean
  voices?: Record<string, string>
  advanced?: Record<string, unknown>
}

export type AudioProviderUpdate = Partial<Omit<AudioProviderCreate, 'provider_type' | 'provider_name'>>

export interface TurnClassifier {
  // Active = a Groq key is configured in the Direct LLM execution layer (its
  // single source). No enable toggle or model — both removed.
  active: boolean
}

export interface AudioPolicy {
  chat_enabled: boolean
  chat_user_policy: 'native_only' | 'native_preferred' | 'user_choice'
  show_experimental: boolean
}

export type AudioSettings = Record<string, string>

// ---------------------------------------------------------------------------
// Providers
// ---------------------------------------------------------------------------

export function useAudioProviders() {
  return useQuery({
    queryKey: ['audio-providers'],
    queryFn: async (): Promise<AudioProvider[]> => {
      const data = await apiJson<{ providers: AudioProvider[] }>('/v1/admin/audio/providers')
        .catch(withMessage('Failed to fetch audio providers'))
      return data.providers
    },
  })
}

export interface KnownProviders {
  stt: string[]
  tts: string[]
}

export function useKnownProviders() {
  return useQuery({
    queryKey: ['audio-known-providers'],
    queryFn: async (): Promise<KnownProviders> => {
      return apiJson<KnownProviders>('/v1/admin/audio/known-providers')
        .catch(withMessage('Failed to fetch known providers'))
    },
    staleTime: Infinity,  // the registry is static for the process
  })
}

export function useCreateAudioProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: AudioProviderCreate) => {
      return apiJson('/v1/admin/audio/providers', {
        method: 'POST',
        body: JSON.stringify(data),
      }, 'Failed to create provider')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useUpdateAudioProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: number; data: AudioProviderUpdate }) => {
      return apiJson(`/v1/admin/audio/providers/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }, 'Failed to update provider')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audio-providers'] }),
  })
}

export function useDeleteAudioProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/audio/providers/${id}`, { method: 'DELETE' }, 'Failed to delete provider')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useSetProviderDefault() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, context }: { id: number; context: 'calls' | 'chat' }) => {
      return apiJson(`/v1/admin/audio/providers/${id}/default?context=${context}`, { method: 'PUT' }, 'Failed to set default')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audio-providers'] }),
  })
}

export function useSetProviderCredential() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, value }: { id: number; value: string }) => {
      return apiJson(`/v1/admin/audio/providers/${id}/credential`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      }, 'Failed to save credential')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audio-providers'] }),
  })
}

export function useDeleteProviderCredential() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/audio/providers/${id}/credential`, { method: 'DELETE' },
        'Failed to delete credential')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

// ---------------------------------------------------------------------------
// Turn classifier (Groq)
// ---------------------------------------------------------------------------

export function useTurnClassifier() {
  return useQuery({
    queryKey: ['audio-turn-classifier'],
    queryFn: async (): Promise<TurnClassifier> => {
      return apiJson<TurnClassifier>('/v1/admin/audio/turn-classifier')
        .catch(withMessage('Failed to fetch turn classifier'))
    },
  })
}

// ---------------------------------------------------------------------------
// Chat audio policy
// ---------------------------------------------------------------------------

export function useAudioPolicy() {
  return useQuery({
    queryKey: ['audio-policy'],
    queryFn: async (): Promise<AudioPolicy> => {
      return apiJson<AudioPolicy>('/v1/admin/audio/policy').catch(withMessage('Failed to fetch audio policy'))
    },
  })
}

export function useUpdateAudioPolicy() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: Partial<AudioPolicy>) => {
      return apiJson('/v1/admin/audio/policy', {
        method: 'PUT',
        body: JSON.stringify(data),
      }, 'Failed to update audio policy')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audio-policy'] }),
  })
}

// ---------------------------------------------------------------------------
// Shared audio settings (audio_* keys; VAD / smart-turn)
// ---------------------------------------------------------------------------

export function useAudioSettings() {
  return useQuery({
    queryKey: ['audio-settings'],
    queryFn: async (): Promise<AudioSettings> => {
      return apiJson<AudioSettings>('/v1/admin/audio/settings').catch(withMessage('Failed to fetch audio settings'))
    },
  })
}

export function useSaveAudioSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: Record<string, string>) => {
      // A refusal names the key and its rule: the message the toast shows.
      return apiJson('/v1/admin/audio/settings', {
        method: 'PUT',
        body: JSON.stringify(data),
      }, 'Failed to save audio settings')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['audio-settings'] }),
  })
}
