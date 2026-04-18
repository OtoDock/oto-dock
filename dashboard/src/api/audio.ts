// Audio API hooks — STT/TTS providers, chat policy, turn classifier, settings.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

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
      const res = await apiFetch('/v1/admin/audio/providers')
      if (!res.ok) throw new Error('Failed to fetch audio providers')
      const data = await res.json()
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
      const res = await apiFetch('/v1/admin/audio/known-providers')
      if (!res.ok) throw new Error('Failed to fetch known providers')
      return res.json()
    },
    staleTime: Infinity,  // the registry is static for the process
  })
}

export function useCreateAudioProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: AudioProviderCreate) => {
      const res = await apiFetch('/v1/admin/audio/providers', {
        method: 'POST',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to create provider')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useUpdateAudioProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: number; data: AudioProviderUpdate }) => {
      const res = await apiFetch(`/v1/admin/audio/providers/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to update provider')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useDeleteAudioProvider() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/audio/providers/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to delete provider')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useSetProviderDefault() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, context }: { id: number; context: 'calls' | 'chat' }) => {
      const res = await apiFetch(`/v1/admin/audio/providers/${id}/default?context=${context}`, { method: 'PUT' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to set default')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useSetProviderCredential() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, value }: { id: number; value: string }) => {
      const res = await apiFetch(`/v1/admin/audio/providers/${id}/credential`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to save credential')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-providers'] }) },
  })
}

export function useDeleteProviderCredential() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/audio/providers/${id}/credential`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete credential')
      return res.json()
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
      const res = await apiFetch('/v1/admin/audio/turn-classifier')
      if (!res.ok) throw new Error('Failed to fetch turn classifier')
      return res.json()
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
      const res = await apiFetch('/v1/admin/audio/policy')
      if (!res.ok) throw new Error('Failed to fetch audio policy')
      return res.json()
    },
  })
}

export function useUpdateAudioPolicy() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: Partial<AudioPolicy>) => {
      const res = await apiFetch('/v1/admin/audio/policy', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to update audio policy')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-policy'] }) },
  })
}

// ---------------------------------------------------------------------------
// Shared audio settings (audio_* keys; VAD / smart-turn)
// ---------------------------------------------------------------------------

export function useAudioSettings() {
  return useQuery({
    queryKey: ['audio-settings'],
    queryFn: async (): Promise<AudioSettings> => {
      const res = await apiFetch('/v1/admin/audio/settings')
      if (!res.ok) throw new Error('Failed to fetch audio settings')
      return res.json()
    },
  })
}

export function useSaveAudioSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: Record<string, string>) => {
      const res = await apiFetch('/v1/admin/audio/settings', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to save audio settings')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['audio-settings'] }) },
  })
}
