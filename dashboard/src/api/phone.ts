// Phone API hooks — telephony servers, routes, and call-only settings.
// STT/TTS providers + chat audio policy live in api/audio.ts.

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { raiseApiError, apiJson, withMessage } from '../lib/http/apiJson'
import type { AgentRole } from '../lib/permissions'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// Per-route filler toggle (backchannel / thinking filler)
export type RouteMode = 'on' | 'off'

// A call's direction (a route's, a call log row's): the API's `direction`
// words, named once here as the proxy names them in adapters/phone.py.
export const PHONE_INBOUND = 'inbound'
export const PHONE_OUTBOUND = 'outbound'
export type PhoneDirection = typeof PHONE_INBOUND | typeof PHONE_OUTBOUND

// Where the agent may hand the caller (transfer_call): the only numbers
// it ever dials, named by label in its instructions.
export interface PhoneTransferTarget {
  label: string
  number: string
}

/** What a route write answers: the server's notes for the person (a route
 * with no PIN, a provisioning step, a delete's warning). */
export interface PhoneRouteWriteResult {
  warnings?: string[]
  warning?: string
  provisioning_instructions?: string
}

export interface PhoneRoute {
  id: string
  direction: PhoneDirection
  name: string
  agent: string
  language: string
  // Read the caller's language from the audio and switch the call to it as
  // the first words are heard (among the Languages card's list); `language`
  // stays the greeting's and the start.
  language_detect: boolean
  llm_mode: string
  transfer_targets: PhoneTransferTarget[]
  phone_server_id: number | null
  stt_provider_id: number | null
  tts_provider_id: number | null
  // A voice for this route only (the TTS provider's own id); '' keeps the
  // provider's voice for the call's language.
  voice_id: string
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
  direction: PhoneDirection
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

/** A route PIN as the server takes it when one is set: 6 digits. A shorter
 * PIN stored earlier keeps working until it is changed. */
export const PIN_DIGITS = 6
export const isRoutePin = (value: string) => /^\d{6}$/.test(value)

export function usePhoneRoutes() {
  return useQuery({
    queryKey: ['phone-routes'],
    queryFn: async (): Promise<PhoneRoute[]> => {
      const data = await apiJson<{ routes: PhoneRoute[] }>('/v1/admin/phone/routes')
        .catch(withMessage('Failed to fetch phone routes'))
      return data.routes
    },
  })
}

export function useCreatePhoneRoute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: PhoneRouteCreate) => {
      return apiJson<PhoneRouteWriteResult>('/v1/admin/phone/routes', {
        method: 'POST',
        body: JSON.stringify(data),
      }, 'Failed to create route')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-routes'] }) },
  })
}

export function useUpdatePhoneRoute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: PhoneRouteUpdate }) => {
      return apiJson<PhoneRouteWriteResult>(`/v1/admin/phone/routes/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }, 'Failed to update route')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phone-routes'] }),
  })
}

export function useDeletePhoneRoute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: string) => {
      return apiJson<PhoneRouteWriteResult>(`/v1/admin/phone/routes/${id}`, { method: 'DELETE' }, 'Failed to delete route')
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
      return apiJson<PhoneRouteWriteResult>(`/v1/admin/phone/routes/${id}/pin${query}`, { method: 'DELETE' }, 'Failed to remove the PIN')
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
      return apiJson<{ calls: PhoneCallLogEntry[]; total: number }>(`/v1/admin/phone/call-log?${params}`)
        .catch(withMessage('Failed to fetch the call log'))
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
      return apiJson<RouteMcpPreview>(`/v1/admin/phone/routes/mcp-preview?${params}`)
        .catch(withMessage('Failed to preview the tools on this route'))
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
      return apiJson<ExternalDataStatus>('/v1/admin/phone/external-data')
        .catch(withMessage('Failed to fetch the caller-data status'))
    },
  })
}

export function useSaveExternalData() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: { enabled?: boolean; days?: number }) => {
      return apiJson<ExternalDataStatus>('/v1/admin/phone/external-data', {
        method: 'PUT',
        body: JSON.stringify(data),
      }, 'Failed to save')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phone-external-data'] }),
  })
}

export function useForgetExternalData() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      return apiJson<ForgetExternalDataResult>('/v1/admin/phone/external-data/forget', { method: 'POST' }, 'Failed to forget caller data')
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
      const data = await apiJson<{ servers: PhoneServer[] }>('/v1/admin/phone-servers')
        .catch(withMessage('Failed to fetch phone servers'))
      return data.servers
    },
    refetchInterval: 15000,  // keep health + bootstrap badges fresh
  })
}

export function useCreatePhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (data: PhoneServerCreate) => {
      return apiJson('/v1/admin/phone-servers', {
        method: 'POST',
        body: JSON.stringify(data),
      }, 'Failed to create server')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useUpdatePhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: number; data: PhoneServerUpdate }) => {
      return apiJson(`/v1/admin/phone-servers/${id}`, {
        method: 'PUT',
        body: JSON.stringify(data),
      }).catch(withMessage('Failed to update server'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phone-servers'] }),
  })
}

export function useDeletePhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/phone-servers/${id}`, { method: 'DELETE' }, 'Failed to delete server')
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useSetDefaultPhoneServer() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/phone-servers/${id}/default`, { method: 'PUT' })
        .catch(withMessage('Failed to set default server'))
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useSetPhoneServerAmiSecret() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, value }: { id: number; value: string }) => {
      return apiJson(`/v1/admin/phone-servers/${id}/ami-secret`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      }).catch(withMessage('Failed to save AMI secret'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phone-servers'] }),
  })
}

export function useDeletePhoneServerAmiSecret() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/phone-servers/${id}/ami-secret`, { method: 'DELETE' })
        .catch(withMessage('Failed to delete AMI secret'))
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['phone-servers'] }) },
  })
}

export function useSetPhoneServerTwilioAuthToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, value }: { id: number; value: string }) => {
      return apiJson(`/v1/admin/phone-servers/${id}/twilio-auth-token`, {
        method: 'PUT',
        body: JSON.stringify({ value }),
      }).catch(withMessage('Failed to save Twilio auth token'))
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phone-servers'] }),
  })
}

export function useDeletePhoneServerTwilioAuthToken() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/phone-servers/${id}/twilio-auth-token`, { method: 'DELETE' })
        .catch(withMessage('Failed to delete Twilio auth token'))
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
      return apiJson<ServerBootstrap>(`/v1/admin/phone-servers/${id}/bootstrap`)
        .catch(withMessage('Failed to fetch bootstrap'))
    },
  })
}

export function useVerifyServerBootstrap() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (id: number) => {
      return apiJson(`/v1/admin/phone-servers/${id}/bootstrap/verify`, { method: 'POST' }, 'Verify failed')
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
      return apiJson(`/v1/admin/phone-servers/${id}/bootstrap/apply`, {
        method: 'POST', body: JSON.stringify(creds),
      }, 'Apply failed')
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
      return apiJson(`/v1/admin/phone-servers/${id}/health`, { method: 'POST' })
        .catch(withMessage('Health check failed'))
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
      return apiJson<PhoneSettings>('/v1/admin/phone/settings').catch(withMessage('Failed to fetch phone settings'))
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
      // A refusal names the key and its rule: the message the toast shows.
      if (!res.ok) await raiseApiError(res, 'Failed to save phone settings')
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['phone-settings'] }),
  })
}

/** The shipped call prompts (the seed's texts): what Restore Default writes. */
export type PhonePromptDefaults = { context_inbound: string; context_outbound: string }

export function usePhonePromptDefaults() {
  return useQuery({
    queryKey: ['phone-prompt-defaults'],
    queryFn: async (): Promise<PhonePromptDefaults> => {
      return apiJson<PhonePromptDefaults>('/v1/admin/phone/settings/defaults')
        .catch(withMessage('Failed to fetch the call prompt defaults'))
    },
    staleTime: Infinity,
  })
}
