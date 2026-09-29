// Phone API hooks — telephony servers, routes, and call-only settings.
// STT/TTS providers + chat audio policy live in api/audio.ts.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import type { AgentRole } from '../lib/permissions'

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
  // Who the caller IS on this route and what the session may touch.
  // 'caller' = every caller gets a private space (external principal; on a
  // shared-only agent that is the shared space with no per-caller memory),
  // 'user' = the call runs as identity_user_sub's own session. 'shared' is
  // a legacy value (option removed 2026-09-07) the API no longer accepts.
  // `remember_callers` off makes every caller ephemeral.
  identity_mode: 'caller' | 'shared' | 'user'
  identity_user_sub: string | null
  // Server echo of a column kept for compatibility: an external caller
  // always runs as viewer (the per-route role selector was removed
  // 2026-09-08); the API ignores whatever a client sends.
  role: AgentRole
  remember_callers: boolean
  // Server-computed mask flag — the PIN value itself never leaves the proxy
  // (set with the route save as `pin`, removed through useDeleteRoutePin).
  pin_configured: boolean
  // Server-computed advisories for this route (a user-tied line without a
  // PIN, a tied user that lost access).
  warnings: string[]
  created_at: string
  updated_at: string
}

// Request-only fields on a route save: a PIN set with the route (never
// echoed back) and the acknowledgement a user-mode inbound route needs to
// save without one (the server refuses the save otherwise).
export type PhoneRouteSaveExtras = { pin?: string; acknowledge_no_pin?: boolean }
export type PhoneRouteCreate = Omit<PhoneRoute, 'id' | 'created_at' | 'updated_at' | 'pin_configured' | 'warnings' | 'role'> & PhoneRouteSaveExtras
export type PhoneRouteUpdate = Partial<PhoneRouteCreate>

export interface PhoneCallLogEntry {
  id: number
  route_id: string | null
  route_name: string
  phone_server_id: number | null
  agent: string
  direction: 'inbound' | 'outbound'
  from_number: string
  to_number: string
  transport: string
  call_uuid: string
  outcome: string
  pin_attempts: number
  started_at: string
  ended_at: string | null
  duration_s: number | null
  // Audit trail of the warmed session: the daemon reports the session id,
  // the proxy fills the caller identity label and the tools the call ran.
  session_id: string
  identity: string
  tools_run: string[]
  created_at: string
}

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
  twilio_auth_token_configured: boolean
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
  twilio_auth_token?: string
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

// Route PIN removal (the write-only sub-resource; a PIN is SET with the route
// save). On a user-mode inbound route the server needs the acknowledgement.
export function useDeleteRoutePin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, acknowledgeNoPin }: { id: string; acknowledgeNoPin?: boolean }) => {
      const query = acknowledgeNoPin ? '?acknowledge_no_pin=true' : ''
      const res = await apiFetch(`/v1/admin/phone/routes/${id}/pin${query}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to remove the PIN')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-routes'] }) },
  })
}

// Per-route call log — fetched lazily while the viewer modal is open.
export function usePhoneRouteCallLog(routeId: string, open: boolean, offset = 0, limit = 25) {
  return useQuery({
    queryKey: ['phone-call-log', routeId, offset, limit],
    enabled: open && !!routeId,
    refetchInterval: open ? 15_000 : false,
    queryFn: async (): Promise<{ calls: PhoneCallLogEntry[]; total: number }> => {
      const params = new URLSearchParams({
        route_id: routeId, offset: String(offset), limit: String(limit),
      })
      const res = await apiFetch(`/v1/admin/phone/call-log?${params}`)
      if (!res.ok) throw new Error('Failed to fetch the call log')
      return res.json()
    },
  })
}

// What a session on a route with this agent + identity would attach — the
// agent's MCPs minus the phone exclusions and (for the external identities)
// minus what never reaches external callers. Manifest-level, no processes;
// the RouteModal shows it before saving.
export interface RouteMcpPreview {
  attached: { name: string; label: string }[]
  excluded: { name: string; label: string; reason: string }[]
}

export function useRouteMcpPreview(agent: string, identityMode: PhoneRoute['identity_mode']) {
  return useQuery({
    queryKey: ['phone-route-mcp-preview', agent, identityMode],
    enabled: !!agent,
    queryFn: async (): Promise<RouteMcpPreview> => {
      const params = new URLSearchParams({ agent, identity_mode: identityMode })
      const res = await apiFetch(`/v1/admin/phone/routes/mcp-preview?${params}`)
      if (!res.ok) throw new Error('Failed to preview the tools on this route')
      return res.json()
    },
  })
}

// Caller data (external routes): the retention window for what callers leave
// behind (their private trees, the phone conversations, the call-log rows),
// what is on disk right now, and "Forget all".
export interface ExternalDataStatus {
  enabled: boolean
  days: number
  callers: number
  bytes: number
  agents: Record<string, { callers: number; bytes: number }>
  phone_chats: number
  call_log_rows: number
}

export interface ForgetExternalDataResult {
  callers_forgotten: number
  callers_busy_skipped: number
  phone_chats_deleted: number
  phone_chats_busy_skipped: number
  call_log_rows_deleted: number
  caller_bytes_freed: number
}

export function useExternalData() {
  return useQuery({
    queryKey: ['phone-external-data'],
    queryFn: async (): Promise<ExternalDataStatus> => {
      const res = await apiFetch('/v1/admin/phone/external-data')
      if (!res.ok) throw new Error('Failed to fetch the caller-data status')
      return res.json()
    },
  })
}

export function useSaveExternalData() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: { enabled?: boolean; days?: number }) => {
      const res = await apiFetch('/v1/admin/phone/external-data', {
        method: 'PUT',
        body: JSON.stringify(data),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to save')
      return res.json() as Promise<ExternalDataStatus>
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-external-data'] }) },
  })
}

export function useForgetExternalData() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      const res = await apiFetch('/v1/admin/phone/external-data/forget', { method: 'POST' })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'Failed to forget caller data')
      return res.json() as Promise<ForgetExternalDataResult>
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['phone-external-data'] })
      qc.invalidateQueries({ queryKey: ['phone-call-log'] })
    },
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

export function useSetPhoneServerTwilioAuthToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, value }: { id: number; value: string }) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/twilio-auth-token`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      })
      if (!res.ok) throw new Error('Failed to save Twilio auth token')
      return res.json()
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useDeletePhoneServerTwilioAuthToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      const res = await apiFetch(`/v1/admin/phone-servers/${id}/twilio-auth-token`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete Twilio auth token')
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
