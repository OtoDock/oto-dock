import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from './auth'
import { useAuth } from '../contexts/AuthContext'
import { isAdmin } from '../lib/permissions'
import { isLocalTarget, type PairingScope } from '../lib/placement'
import { isReachableMachine, type MachineState } from '../lib/status/machine'
import type { BootstrapOs, DisplayServer } from '../lib/hostOs/os'
import { apiJson, apiOk, withMessage } from '../lib/http/apiJson'

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
      return apiJson<PairResult>('/v1/admin/remote-machines/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }, 'Failed to pair machine')
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
      await apiOk(`/v1/admin/remote-machines/${machineId}/allow-full-fs`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        }, 'Could not change full filesystem access')
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
      await apiOk(`/v1/admin/remote-machines/${machineId}/max-sessions`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ max_sessions: value }),
        }, 'Failed to update session limit')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['remote-machines'] }),
  })
}

// The device-control capabilities + their UI labels, shared by the admin
// Remote Machines page and User Settings → My
// Machines so the two grant UIs can't drift. Keys must match the server's
// DEVICE_CAPABILITIES set. `grant` is what the agents may do once it is
// granted, the confirm's sentences after their subject (deviceGrantBody).
export interface DeviceCapabilityInfo {
  key: string
  label: string
  desc: string
  grant: string
}

export const DEVICE_CAPABILITY_INFO: DeviceCapabilityInfo[] = [
  {
    key: 'computer', label: 'Computer control', desc: 'Mouse, keyboard and screen',
    grant: 'can see the screen of this computer and use its mouse and keyboard. ' +
      "They can also answer the computer's own permission and password prompts.",
  },
  // The granted row swaps this for the selected mode's description
  // (lib/remote/browserMode.ts browserModeDesc).
  {
    key: 'browser', label: 'Browser control', desc: 'Use a browser on this computer',
    grant: 'can browse the web on this computer and use the sites the browser is signed in to.',
  },
  {
    key: 'app', label: 'App connectors', desc: 'Work in the applications on this computer',
    grant: 'can work in the applications on this computer that have a connector, like Blender. ' +
      'They can run code inside those applications.',
  },
]

/** The grant confirm's body: the person's own machine names "your agents",
 *  the admin page the machine's assigned ones. */
export function deviceGrantBody(cap: DeviceCapabilityInfo, assigned: boolean): string {
  return `${assigned ? 'Assigned agents' : 'Your agents'} ${cap.grant} ` +
    'Turn it on only on a computer you trust them with.'
}

// Set the device-control consent set (admin scope).
// `grants` is the full desired list — the UI toggles a capability by sending
// the new list with it added/removed.
export const useSetDeviceGrants = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (
      { machineId, grants }: { machineId: string; grants: string[] },
    ) => {
      await apiOk(`/v1/admin/remote-machines/${machineId}/device-grants`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ grants }),
        }, 'Failed to update device grants')
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
      await apiOk(`${machinePath(scope, machineId)}/browser-mode`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      }, 'Failed to update the browser mode')
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
      await apiOk(`${machinePath(scope, machineId)}/browser-token`, token === null
          ? { method: 'DELETE' }
          : {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token }),
          }, 'Failed to update the extension token')
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
      await apiOk(`${machinePath(scope, machineId)}/browser-unattended`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      }, 'Failed to update unattended use')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: machinesKey(scope) }),
  })
}

export const useDeleteMachine = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (machineId: string) => {
      await apiOk(`/v1/admin/remote-machines/${machineId}`, {
        method: 'DELETE',
      }).catch(withMessage('Failed to delete machine'))
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
      await apiOk(`/v1/admin/remote-machines/${machineId}/agents`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_slug: agentSlug }),
      }, 'Failed to assign agent')
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
      await apiOk(`/v1/admin/remote-machines/${machineId}/agents/${agentSlug}`, {
        method: 'DELETE',
      }).catch(withMessage('Failed to unassign agent'))
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
      return apiJson<PairResult>('/v1/users/me/remote-machines/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }, 'Failed to pair machine')
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
      await apiOk(`/v1/users/me/remote-machines/${machineId}/allow-full-fs`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
        }, 'Could not change full filesystem access')
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
      await apiOk(`/v1/users/me/remote-machines/${machineId}/device-grants`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ grants }),
        }, 'Failed to update device grants')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

export const useDeleteMyMachine = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (machineId: string) => {
      await apiOk(`/v1/users/me/remote-machines/${machineId}`, { method: 'DELETE' })
        .catch(withMessage('Failed to delete machine'))
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
      await apiOk(`/v1/users/me/remote-targets/${encodeURIComponent(args.agent_slug)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ machine_id: args.machine_id }),
        }, 'Failed to set target')
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
  })
}

export const useRemoveMyRemoteTarget = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (agent_slug: string) => {
      await apiOk(
        `/v1/users/me/remote-targets/${encodeURIComponent(agent_slug)}`,
        { method: 'DELETE' },
      ).catch(withMessage('Failed to remove target'))
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
      return apiJson(`${base}/${args.machine_id}/auto-update`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: args.enabled }),
      }, 'Failed to toggle auto-update')
    },
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ['remote-machines'] }),
      qc.invalidateQueries({ queryKey: ['my-remote-machines'] }),
    ]),
  })
}

export const useTriggerUpdateNow = (mine: boolean = false) => {
  const qc = useQueryClient()
  const base = mine ? '/v1/users/me/remote-machines' : '/v1/admin/remote-machines'
  return useMutation({
    mutationFn: async (machine_id: string) => {
      return apiJson(`${base}/${machine_id}/update-now`, {
        method: 'POST',
      }, 'Failed to trigger update')
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['remote-machines'] })
      qc.invalidateQueries({ queryKey: ['my-remote-machines'] })
    },
  })
}

// ─── Where a chat runs, chosen per chat ──────────────────────────

/** How the platform's own place reads in a list, a menu or a tooltip (the
 *  proxy's `chat_targets.LOCAL_LABEL`). */
export const LOCAL_LABEL = 'OtoDock platform'

/** A place as it reads inside a sentence (the proxy's `place_phrase`): "the
 *  OtoDock platform" for the platform ("The …" opening a sentence), a
 *  machine's name as it is. Decided by the target, never by the label. */
export function placePhrase(target: string | null | undefined, label: string, start = false): string {
  if (isLocalTarget(target)) return `${start ? 'The' : 'the'} ${LOCAL_LABEL}`
  return label
}

/** One place a chat (or a new chat, or a task) may run: the OtoDock platform
 *  (`target` TARGET_LOCAL, `pairing` '') or a paired machine. `pickable` is
 *  the server's verdict for this person now; `why_not` its sentence when not. */
export interface RunTarget {
  target: string
  label: string
  pairing: PairingScope | ''
  /** The machine's live state (lib/status/machine.ts); '' for the server. */
  state: MachineState | ''
  reachable: boolean
  is_default: boolean
  pickable: boolean
  why_not: string
  /** The guard lets this place run the chat (a pick may still wait for
   *  the machine to come online): false when `why_not` is the guard's. */
  allowed: boolean
  /** A move with this place carries the chat's session files: false for a
   *  machine whose satellite is too old to move them, so the move continues
   *  from a summary. An older proxy sends none: read as true. */
  moves_files?: boolean
}

/** A chat's place and the places it may move to (GET /v1/chats/{id}/targets). */
export interface ChatTargets {
  current: {
    target: string
    label: string
    pairing: PairingScope | ''
    state: MachineState | ''
    /** The machine owner's display name ('' for the server or an admin machine). */
    owner: string
  }
  default_target: string
  targets: (RunTarget & { is_current: boolean })[]
  /** False: no menu, `refusal` says why (a phone chat, a Direct LLM chat,
   *  a person who cannot drive the chat). */
  can_move: boolean
  refusal: string
  /** Set while the chat runs on someone else's computer: read-only here. */
  lock: { sentence: string } | null
  /** Set while a move of the chat runs; `progress` is its transfer so far
   *  (null before the first report), kept fresh by `chat_move_progress`. */
  moving: { to_label: string; progress?: MoveProgress | null } | null
  /** The chat's place came from a person's move: the mismatch banner and
   *  the row menu's move to the default stay hidden. */
  place_chosen: boolean
}

/** A running move's transfer: `done` of `total` bytes (`total` 0: unknown)
 *  in `phase` (`MOVE_PHASE`). */
export interface MoveProgress {
  done: number
  total: number
  phase: string
}

/** What a move carries: the session's own files, or a summary of the chat
 *  when the place it leaves is offline (`chat_moved.memory` too). */
export const MOVE_MEMORY = { FILES: 'files', SUMMARY: 'summary' } as const
export type MoveMemory = (typeof MOVE_MEMORY)[keyof typeof MOVE_MEMORY]

export interface MoveChatResult {
  move_id: string
  to_label: string
  memory: MoveMemory
}

/** Where new chats of an agent start for this person, and the choices of a
 *  task's "Runs on" (GET /v1/agents/{name}/run-targets, `?task_id=` for a
 *  task's run person). */
export interface AgentRunTargets {
  default_target: string
  targets: RunTarget[]
}

/** The targets query of one chat: the chip, the banner, the row menu and the
 *  page's lock read the same entry, and a `chat_moved` frame refreshes it. */
export const chatTargetsKey = (chatId: string | null) => ['chat-targets', chatId] as const

export const useChatTargets = (chatId: string | null) =>
  useQuery({
    queryKey: chatTargetsKey(chatId),
    queryFn: () => apiJson<ChatTargets>(
      `/v1/chats/${encodeURIComponent(chatId ?? '')}/targets`, {}, 'Could not read where this chat can run'),
    enabled: !!chatId,
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  })

/** The chat's current place is a machine that is offline. */
export function leavesOffline(data: ChatTargets): boolean {
  if (isLocalTarget(data.current.target)) return false
  const here = data.targets.find((t) => t.is_current)
  return !(here ? here.reachable : isReachableMachine(data.current.state))
}

/** The memory a move from the chat's current place to `target` carries: a
 *  summary when that place is a machine that is offline or when either place
 *  cannot move the session files (an older satellite), else the files. */
export function moveMemoryOf(data: ChatTargets, target: string): MoveMemory {
  if (leavesOffline(data)) return MOVE_MEMORY.SUMMARY
  const here = data.targets.find((t) => t.is_current)
  const there = data.targets.find((t) => t.target === target)
  return here?.moves_files === false || there?.moves_files === false
    ? MOVE_MEMORY.SUMMARY : MOVE_MEMORY.FILES
}

/** Move a chat (POST /v1/chats/{id}/move, `from` = the place the chip
 *  showed). The 202 starts the move in the background: the targets entry
 *  shows it moving until the next answer, and the `chat_moved` frame brings
 *  the outcome. A refusal (409) raises its sentence. */
export const useMoveChat = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ chatId, target, from }: { chatId: string; target: string; from: string }) =>
      apiJson<MoveChatResult>(`/v1/chats/${encodeURIComponent(chatId)}/move`, {
        method: 'POST',
        json: { target, from },
      }, 'The chat could not be moved'),
    onSuccess: async (res, { chatId }) => {
      const key = chatTargetsKey(chatId)
      // A poll already in flight answers from before the move.
      await qc.cancelQueries({ queryKey: key })
      qc.setQueryData<ChatTargets>(key, (old) => (old ? { ...old, moving: { to_label: res.to_label } } : old))
    },
    // A refusal names a change (the chat just moved, a machine went away):
    // read the place again.
    onError: (_e, { chatId }) => qc.invalidateQueries({ queryKey: chatTargetsKey(chatId) }),
  })
}

/** `taskId` asks for that task's run person (the server then ignores
 *  `scope`): the creator of a user-scope task, no person for an agent-scope
 *  or Shared-only one. */
export const useAgentRunTargets = (agent: string, scope?: 'user' | 'agent', taskId?: string,
                                   engine?: string) => {
  const params = new URLSearchParams()
  if (taskId) params.set('task_id', taskId)
  else if (scope) params.set('scope', scope)
  if (engine && !taskId) params.set('engine', engine)
  const qs = params.toString() ? `?${params.toString()}` : ''
  return useQuery({
    queryKey: ['agent-run-targets', agent, scope ?? '', taskId ?? '', engine ?? ''],
    queryFn: () => apiJson<AgentRunTargets>(
      `/v1/agents/${encodeURIComponent(agent)}/run-targets${qs}`,
      {}, 'Could not read where this agent can run'),
    enabled: !!agent,
    refetchInterval: 15_000,
  })
}
