// Phone API hooks — telephony servers, routes, and call-only settings.
// STT/TTS providers + chat audio policy live in api/audio.ts.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// Per-route filler toggle (backchannel / thinking filler)
export type RouteMode = 'on' | 'off'

export interface PhoneRoute {
  id: string
  direction: 'inbound' | 'outbound'
  name: string
  agent: string
  language: string
  llm_mode: string
  phone_server_id: number | null
  stt_provider_id: number | null
  tts_provider_id: number | null
  greeting: string
  phone_context_override: string
  backchannel_mode: RouteMode
  thinking_filler_mode: RouteMode
  // Background ambience bed played through the whole call
  background_sound: 'off' | 'call_center' | 'office' | 'city' | 'nature'
  enabled: boolean
  audiosocket_uuid: string | null
  did: string
  ami_caller_id: string
  ami_outbound_context: string
  dial_prefix: string
  // Optional bound trigger slug (scope='agent', matching agent).
  // When set, the proxy fetches the trigger row at warmup and enriches the
  // session prompt via manifest agent_context `${trigger.*}` tokens.
  trigger_slug: string | null
  created_at: string
  updated_at: string
}

export type PhoneRouteCreate = Omit<PhoneRoute, 'id' | 'created_at' | 'updated_at'>
export type PhoneRouteUpdate = Partial<PhoneRouteCreate>

export interface PhoneServer {
  id: number
  name: string
  adapter_type: 'asterisk_manual' | 'asterisk_freepbx' | 'twilio' | 'three_cx'
  host: string
  credentials: Record<string, unknown>
  config: Record<string, unknown>
  bootstrap_status: 'pending' | 'snippet_provided' | 'verified' | 'failed' | 'drift'
  bootstrap_log: string
  last_health_check: string | null
  last_health_status: string
  last_health_detail: string
  is_default: boolean
  ami_secret_configured: boolean
  created_at: string
  updated_at: string
}

export type PhoneServerCreate = {
  name: string
  adapter_type?: PhoneServer['adapter_type']
  host?: string
  config?: Record<string, unknown>
  credentials?: Record<string, unknown>
  is_default?: boolean
  ami_secret?: string
}
export type PhoneServerUpdate = Partial<Omit<PhoneServerCreate, 'ami_secret' | 'is_default'>>

export type PhoneSettings = Record<string, string>

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

export function usePhoneRoutes() {
  return useQuery({
    queryKey: ['phone-routes'],
    queryFn: async (): Promise<PhoneRoute[]> => {
      const res = await apiFetch('/v1/admin/phone/routes')
      if (!res.ok) throw new Error('Failed to fetch phone routes')
      const data = await res.json()
      return data.routes
    },
  })
}

export function useCreatePhoneRoute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: PhoneRouteCreate) => {
      const res = await apiFetch('/v1/admin/phone/routes', {
        method: 'POST',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to create route')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-routes'] }) },
  })
}

export function useUpdatePhoneRoute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: PhoneRouteUpdate }) => {
      const res = await apiFetch(`/v1/admin/phone/routes/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to update route')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-routes'] }) },
  })
}

export function useDeletePhoneRoute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      const res = await apiFetch(`/v1/admin/phone/routes/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to delete route')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-routes'] }) },
  })
}

// ---------------------------------------------------------------------------
// Phone servers
// ---------------------------------------------------------------------------

export function usePhoneServers() {
  return useQuery({
    queryKey: ['phone-servers'],
    queryFn: async (): Promise<PhoneServer[]> => {
      const res = await apiFetch('/v1/admin/phone-servers')
      if (!res.ok) throw new Error('Failed to fetch phone servers')
      const data = await res.json()
      return data.servers
    },
    refetchInterval: 15000,  // keep health + bootstrap badges fresh
  })
}

export function useCreatePhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: PhoneServerCreate) => {
      const res = await apiFetch('/v1/admin/phone-servers', {
        method: 'POST',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to create server')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useUpdatePhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: number; data: PhoneServerUpdate }) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to update server')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useDeletePhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to delete server')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useSetDefaultPhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/default`, { method: 'PUT' })
      if (!res.ok) throw new Error('Failed to set default server')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useSetPhoneServerAmiSecret() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, value }: { id: number; value: string }) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/ami-secret`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      })
      if (!res.ok) throw new Error('Failed to save AMI secret')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useDeletePhoneServerAmiSecret() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/ami-secret`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete AMI secret')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

// ---------------------------------------------------------------------------
// Bootstrap + health
// ---------------------------------------------------------------------------

export interface ServerBootstrap {
  status: PhoneServer['bootstrap_status']
  log: string
  snippet: string | null
  /** Generated AMI manager-user block (credentials pre-wired server-side);
   * null for adapters that don't speak AMI (cloud stubs). */
  ami_snippet: string | null
  /** Where the AMI block belongs: manager_custom.conf (FreePBX) / manager.conf. */
  ami_snippet_file: string | null
  ami_username: string | null
  requires_bootstrap: boolean
  supports_sftp: boolean
}

export function useServerBootstrap(id: number, enabled: boolean) {
  return useQuery({
    queryKey: ['phone-server-bootstrap', id],
    enabled,
    queryFn: async (): Promise<ServerBootstrap> => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/bootstrap`)
      if (!res.ok) throw new Error('Failed to fetch bootstrap')
      return res.json()
    },
  })
}

export function useVerifyServerBootstrap() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/bootstrap/verify`, { method: 'POST' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Verify failed')
      return res.json()
    },
    onSuccess: (_d, id) => {
      qc.invalidateQueries({ queryKey: ['phone-servers'] })
      qc.invalidateQueries({ queryKey: ['phone-server-bootstrap', id] })
    },
  })
}

export function useApplyServerBootstrap() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, creds }: { id: number; creds: Record<string, string> }) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/bootstrap/apply`, {
        method: 'POST', body: JSON.stringify(creds),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Apply failed')
      return res.json()
    },
    onSuccess: (_d, { id }) => {
      qc.invalidateQueries({ queryKey: ['phone-servers'] })
      qc.invalidateQueries({ queryKey: ['phone-server-bootstrap', id] })
    },
  })
}

export function useCheckServerHealth() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/health`, { method: 'POST' })
      if (!res.ok) throw new Error('Health check failed')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

// ---------------------------------------------------------------------------
// Call-only settings (phone_* keys)
// ---------------------------------------------------------------------------

export function usePhoneSettings() {
  return useQuery({
    queryKey: ['phone-settings'],
    queryFn: async (): Promise<PhoneSettings> => {
      const res = await apiFetch('/v1/admin/phone/settings')
      if (!res.ok) throw new Error('Failed to fetch phone settings')
      return res.json()
    },
  })
}

export function useSavePhoneSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: Record<string, string>) => {
      const res = await apiFetch('/v1/admin/phone/settings', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error('Failed to save phone settings')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-settings'] }) },
  })
}
