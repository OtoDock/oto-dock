import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { useAuth } from '../contexts/AuthContext'

export interface RemoteMachine {
  id: string
  name: string
  // Live status merged from in-memory WS connection manager. The proxy
  // overlays in-memory last_heartbeat over the DB status row before
  // returning to the UI.
  status: 'online' | 'stale' | 'offline' | 'disconnected' | 'never_connected' | 'paused'
  last_seen: string | null
  last_heartbeat_age_s?: number | null
  reachable?: boolean
  registered_by: string  // user_sub of who paired it
  // 'admin' = paired via the admin Remote Machines page (platform
  // infrastructure, can be agent-scope default). 'user' = paired via
  // UserSettings (personal scope, only the owner's user-scope chats run there).
  pairing_scope: 'admin' | 'user'
  capabilities: {
    os?: string
    arch?: string
    installed_clis?: string[]
    installed_mcps?: string[]
    // The OS account the satellite runs as + its home dir. Meaningful now
    // that the satellite is a per-user service (identity = the real user,
    // never root/SYSTEM). Surfaced as a "Running as" subtitle on the cards.
    os_user?: string
    home_dir?: string
    local_tunnel_port?: number
    // Device-local MCP support: whether this machine has an interactive GUI
    // session. Lets the UI warn that device
    // control (computer/browser) won't work on a headless satellite.
    display?: {
      has_display?: boolean
      server?: 'x11' | 'wayland' | 'quartz' | 'windows' | 'none'
      session_active_unlocked?: boolean
    }
  }
  assigned_agents: string[]
  created_at: string
  // Auto-update fields.
  auto_update_enabled?: boolean
  satellite_version?: string | null
  last_update_at?: string | null
  last_update_error?: string | null
  pending_update?: boolean
  // Per-machine filesystem-access policy. When true, the
  // path framework admits any path the satellite-user's OS account
  // can reach. When false, only the agent tree + the OS user's home
  // dir are admitted. Defaults at pairing time: admin pairing → true,
  // user pairing → false.
  allow_full_fs?: boolean
  // Per-machine device-control consent: the
  // capabilities ('computer' | 'browser' | 'app') device-local MCPs may use
  // on this machine. Empty/absent = all blocked. Defaults to [] at pairing
  // for both admin- and user-paired machines (granted only by explicit toggle).
  device_grants?: string[]
  // Proxy-side concurrent-session override. null = use the
  // satellite's own reported recommendation. The satellite still hard-caps
  // at its physical max regardless of this value.
  max_sessions?: number | null
  // Joined from `users` so the admin Remote Machines page can show the
  // owner of user-paired machines in its read-only section. Empty
  // string when the registering user has been deleted.
  owner_display_name?: string
  owner_email?: string
  owner_role?: string
}

export type SatelliteOs = 'linux' | 'macos' | 'windows'

export interface PairResult {
  machine_id: string
  name: string
  pairing_token: string
  expires_in_hours: number
  install_commands: Record<SatelliteOs, string>
}

/** Best-effort detection of the user's OS via navigator.userAgent. */
export function detectSatelliteOs(): SatelliteOs {
  if (typeof navigator === 'undefined') return 'linux'
  const ua = navigator.userAgent.toLowerCase()
  if (ua.includes('win')) return 'windows'
  if (ua.includes('mac')) return 'macos'
  return 'linux'
}

export const useRemoteMachines = () => {
  // Admin-only endpoint, but consumers (AgentCard, AgentConfig) render for
  // every role — gate the query HERE so no consumer can poll it as non-admin.
  // An unconditional 15s poll answered 403 is not just wasted traffic: a
  // network IDS/IPS can match the repeated 403s as scanning (UniFi/Snort sig
  // 2101201 "403 Forbidden") and block the client's whole flow to the proxy.
  const { user } = useAuth()
  return useQuery({
    queryKey: ['remote-machines'],
    queryFn: async (): Promise<RemoteMachine[]> => {
      const res = await apiFetch('/v1/admin/remote-machines')
      const data = await res.json()
      return data.machines ?? []
    },
    refetchInterval: 15000,
    enabled: user?.role === 'admin',
  })
}

export const usePairMachine = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      body: { name: string; allow_full_fs?: boolean },
    ): Promise<PairResult> => {
      const res = await apiFetch('/v1/admin/remote-machines/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed to pair machine' }))
        throw new Error(err.detail || 'Failed to pair machine')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['remote-machines'] }),
  })
}

export const useSetAllowFullFs = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, enabled }: { machineId: string; enabled: boolean },
    ) => {
      const res = await apiFetch(
        `/v1/admin/remote-machines/${machineId}/allow-full-fs`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update FS policy')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['remote-machines'] }),
  })
}

// Per-machine concurrent-session override (admin scope). `value` is
// the proxy-side soft cap; pass `null` to clear the override (empty input →
// the satellite's own recommendation is used).
export const useSetMaxSessions = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, value }: { machineId: string; value: number | null },
    ) => {
      const res = await apiFetch(
        `/v1/admin/remote-machines/${machineId}/max-sessions`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ max_sessions: value }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update session limit')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['remote-machines'] }),
  })
}

// The device-control capabilities + their UI labels, shared by the admin
// Remote Machines page and User Settings → My
// Machines so the two grant UIs can't drift. Keys must match the server's
// DEVICE_CAPABILITIES set.
export const DEVICE_CAPABILITY_INFO: { key: string; label: string; desc: string }[] = [
  { key: 'computer', label: 'Computer control', desc: 'mouse, keyboard & screen' },
  { key: 'browser', label: 'Browser control', desc: 'control your local persistent browser' },
  { key: 'app', label: 'App connectors', desc: 'control a running desktop app' },
]

// Set the device-control consent set (admin scope).
// `grants` is the full desired list — the UI toggles a capability by sending
// the new list with it added/removed.
export const useSetDeviceGrants = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, grants }: { machineId: string; grants: string[] },
    ) => {
      const res = await apiFetch(
        `/v1/admin/remote-machines/${machineId}/device-grants`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ grants }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update device grants')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['remote-machines'] }),
  })
}

export const useDeleteMachine = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (machineId: string) => {
      const res = await apiFetch(`/v1/admin/remote-machines/${machineId}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error('Failed to delete machine')
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['remote-machines'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
  })
}

export const useAssignAgent = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ machineId, agentSlug }: { machineId: string; agentSlug: string }) => {
      const res = await apiFetch(`/v1/admin/remote-machines/${machineId}/agents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_slug: agentSlug }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed to assign agent' }))
        throw new Error(err.detail || 'Failed to assign agent')
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['remote-machines'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
  })
}

export const useUnassignAgent = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ machineId, agentSlug }: { machineId: string; agentSlug: string }) => {
      const res = await apiFetch(`/v1/admin/remote-machines/${machineId}/agents/${agentSlug}`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error('Failed to unassign agent')
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['remote-machines'] })
      qc.invalidateQueries({ queryKey: ['agents'] })
    },
  })
}

// --- User-level hooks ---

export interface MyRemoteMachinesData {
  machines: RemoteMachine[]
  targets: {
    user_sub: string
    machine_id: string
    agent_slug: string
    name: string
    status: string
  }[]
}

export const useMyRemoteMachines = () =>
  useQuery({
    queryKey: ['my-remote-machines'],
    queryFn: async (): Promise<MyRemoteMachinesData> => {
      const res = await apiFetch('/v1/users/me/remote-machines')
      return res.json()
    },
    refetchInterval: 15000,
  })

export const usePairMyMachine = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      body: { name: string; allow_full_fs?: boolean },
    ): Promise<PairResult> => {
      const res = await apiFetch('/v1/users/me/remote-machines/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to pair machine')
      }
      return res.json()
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

export const useSetMyAllowFullFs = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, enabled }: { machineId: string; enabled: boolean },
    ) => {
      const res = await apiFetch(
        `/v1/users/me/remote-machines/${machineId}/allow-full-fs`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update FS policy')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

// Owner-scoped device-control consent. Only the
// user who paired the machine can grant capabilities (admin-paired machines
// reject with 403 server-side).
export const useSetMyDeviceGrants = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, grants }: { machineId: string; grants: string[] },
    ) => {
      const res = await apiFetch(
        `/v1/users/me/remote-machines/${machineId}/device-grants`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ grants }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update device grants')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

export const useDeleteMyMachine = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (machineId: string) => {
      const res = await apiFetch(`/v1/users/me/remote-machines/${machineId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error('Failed to delete machine')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

// Per-agent target endpoints. Set one agent to run on one machine
// by checking the box in Remote Machines. Legacy global-target endpoints are gone.

export const useSetMyRemoteTarget = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { agent_slug: string; machine_id: string }) => {
      const res = await apiFetch(
        `/v1/users/me/remote-targets/${encodeURIComponent(args.agent_slug)}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ machine_id: args.machine_id }),
        },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to set target')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

export const useRemoveMyRemoteTarget = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (agent_slug: string) => {
      const res = await apiFetch(
        `/v1/users/me/remote-targets/${encodeURIComponent(agent_slug)}`,
        { method: 'DELETE' },
      )
      if (!res.ok) throw new Error('Failed to remove target')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

// ─── Auto-update toggle + manual update trigger ──────────────────

export const useSetAutoUpdate = (mine: boolean = false) => {
  const qc = useQueryClient()
  const base = mine ? '/v1/users/me/remote-machines' : '/v1/admin/remote-machines'
  return useMutation({
    mutationFn: async (args: { machine_id: string; enabled: boolean }) => {
      const res = await apiFetch(`${base}/${args.machine_id}/auto-update`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: args.enabled }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to toggle auto-update')
      }
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['remote-machines'] })
      qc.invalidateQueries({ queryKey: ['my-remote-machines'] })
    },
  })
}

export const useTriggerUpdateNow = (mine: boolean = false) => {
  const qc = useQueryClient()
  const base = mine ? '/v1/users/me/remote-machines' : '/v1/admin/remote-machines'
  return useMutation({
    mutationFn: async (machine_id: string) => {
      const res = await apiFetch(`${base}/${machine_id}/update-now`, {
        method: 'POST',
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to trigger update')
      }
      return res.json()
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['remote-machines'] })
      qc.invalidateQueries({ queryKey: ['my-remote-machines'] })
    },
  })
}
