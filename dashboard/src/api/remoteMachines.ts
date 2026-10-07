import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin } from '../lib/permissions'
import type { PairingScope } from '../lib/placement'
import type { MachineState } from '../lib/status/machine'
import type { BootstrapOs, DisplayServer } from '../lib/hostOs/os'

export interface RemoteMachine {
  id: string
  name: string
  // The live state (lib/status/machine.ts): every route overlays the
  // in-memory connection's state on the DB row before returning it, so
  // the column's `offline` never arrives.
  status: MachineState
  last_seen: string | null
  last_heartbeat_age_s?: number | null
  reachable?: boolean
  registered_by: string  // user_sub of who paired it
  // 'admin' = paired via the admin Remote Machines page (platform
  // infrastructure, can be agent-scope default). 'user' = paired via
  // UserSettings (personal scope, only the owner's user-scope chats run there).
  pairing_scope: PairingScope
  capabilities: {
    os?: string
    arch?: string
    installed_clis?: string[]
    installed_mcps?: string[]
    // The engine ids (execution_path values) this satellite can run
    // (0.5.124+). Absent on an older satellite — the proxy then assumes the
    // two CLI engines; the card shows no engine chips rather than repeat
    // that assumption.
    engines?: string[]
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
      server?: DisplayServer
      session_active_unlocked?: boolean
    }
    // Per-CLI {version, path} the satellite's pin reconcile resolved
    // (0.5.107+; keys are bin names 'claude'/'codex'). Absent on older
    // satellites and in the short auth→reconcile window after a connect.
    cli_status?: Record<string, { version?: string | null; path?: string | null }>
  }
  // The platform's pinned CLI versions (same for every machine; carried
  // per-machine because the list hook returns machines only). Judged
  // against capabilities.cli_status for the drift-highlighted chips.
  cli_pins?: Record<string, string>
  assigned_agents: string[]
  created_at: string
  // Auto-update fields.
  auto_update_enabled?: boolean
  satellite_version?: string | null
  last_update_at?: string | null
  last_update_error?: string | null
  pending_update?: boolean
  // Rollback budget for the target named in update_rollback_target: at 2
  // consecutive rollbacks the proxy stops auto-pushing that target until
  // "Update now" (see MachineUpdateControls' paused notice).
  update_rollback_count?: number
  update_rollback_target?: string | null
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
  // Which browser the browser-control MCP drives on this machine:
  // 'dedicated' (the per-agent profile, default) or 'own' (the OS user's
  // signed-in Chrome/Edge/Brave through the Playwright Extension). Only
  // meaningful while 'browser' is granted; revoking that grant resets it.
  browser_mode?: 'dedicated' | 'own'
  // Whether an extension token is stored for own mode (the token itself is
  // never returned). Without one every session asks for a click in the
  // browser and unattended sessions cannot use it.
  browser_extension_token_set?: boolean
  // Whether unattended sessions (scheduled tasks, triggers, calls, meetings)
  // may use the saved token; off, only a session a person drives does.
  browser_unattended?: boolean
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

export interface PairResult {
  machine_id: string
  name: string
  pairing_token: string
  expires_in_hours: number
  install_commands: Record<BootstrapOs, string>
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
    enabled: isAdmin(user),
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
  // The granted row swaps this for the selected mode's description
  // (components/BrowserModeControls.tsx browserModeDesc).
  { key: 'browser', label: 'Browser control', desc: 'drive a real browser on this machine' },
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

// Own-browser mode + extension token. One hook each for both grant UIs:
// `scope` picks the admin route (admin-paired machines) or the owner route
// (the caller's user-paired machines) and the machine list to refresh.
export type MachineScope = 'admin' | 'me'
const machinePath = (scope: MachineScope, machineId: string) =>
  scope === 'admin'
    ? `/v1/admin/remote-machines/${machineId}`
    : `/v1/users/me/remote-machines/${machineId}`
const machinesKey = (scope: MachineScope) =>
  scope === 'admin' ? ['remote-machines'] : ['my-remote-machines']

export const useSetBrowserMode = (scope: MachineScope) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, mode }: { machineId: string; mode: 'dedicated' | 'own' },
    ) => {
      const res = await apiFetch(`${machinePath(scope, machineId)}/browser-mode`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update the browser mode')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: machinesKey(scope) }),
  })
}

// `token: null` forgets the stored token.
export const useSetBrowserToken = (scope: MachineScope) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, token }: { machineId: string; token: string | null },
    ) => {
      const res = await apiFetch(
        `${machinePath(scope, machineId)}/browser-token`,
        token === null
          ? { method: 'DELETE' }
          : {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token }),
          },
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update the extension token')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: machinesKey(scope) }),
  })
}

export const useSetBrowserUnattended = (scope: MachineScope) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, enabled }: { machineId: string; enabled: boolean },
    ) => {
      const res = await apiFetch(`${machinePath(scope, machineId)}/browser-unattended`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Failed' }))
        throw new Error(err.detail || 'Failed to update unattended use')
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: machinesKey(scope) }),
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
      // An admin may remove a USER-paired machine; their own User Settings
      // tab lists it too when they are its owner.
      qc.invalidateQueries({ queryKey: ['my-remote-machines'] })
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
