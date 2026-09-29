import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch } from './auth'
import type { ActionFloor, EffectiveRole } from '../lib/permissions'
import type { SiteKind } from '../lib/placement'
import { appKind, type AppKindName } from '../lib/kinds/app'
import type { DeployState } from '../lib/status/appDeploy'
import type { AppServerState } from '../lib/status/appServer'

export interface AppArgsSchema {
  properties?: Record<string, { type?: string; description?: string; enum?: unknown[] }>
  required?: string[]
}

export interface AppAction {
  id: string
  label: string
  type: 'fire_task' | 'send_prompt' | 'mcp_tool' | 'data_feed' | 'platform'
  /** platform only: the catalog method the page may call (otodock.platform). */
  method?: string
  task_id?: string
  task_name?: string
  prompt?: string
  mcp?: string
  tool?: string
  fixed_args?: Record<string, unknown>
  args_schema?: AppArgsSchema
  /** mcp_tool only: whether the target MCP is currently assigned+enabled. */
  mcp_available?: boolean
  /** data_feed only: the read-only platform feed the page may subscribe to
      (otodock.feed) — answered by the host page, viewer-scoped. */
  feed?: string
  /** The role a viewer needs for this action; absent = every viewer. */
  min_role?: ActionFloor
  /** fire_task only: the checks (by name) the press attaches to the run (CHECKS.md). */
  checks?: string[]
}

/** The manifest's other blocks (APPS.md "Handlers", "Bindings") as the
 * approval card renders them; each is absent or empty until declared. */
export interface AppHandlers {
  on_schedule?: Record<string, { cron: string }>
  on_trigger?: string[]
  on_event?: Record<string, string[]>
}
export interface AppExportEntry { description: string; min_role?: ActionFloor }
export interface AppExports {
  methods?: Record<string, AppExportEntry>
  snapshots?: Record<string, AppExportEntry>
  events?: Record<string, AppExportEntry>
}
export interface AppBinding { name: string; agent: string; app: string }
export interface AppRequires { mcps?: string[]; providers?: string[] }
/** APPS.md "Steps": the handlers that run a script where the agent's
 * sessions run; `sha256` is the content the approval signs. */
export interface AppStep { run: string; timeout?: number; sha256?: string }
export type AppSteps = Record<string, AppStep>
/** Where the steps would run, in the card's words. */
export interface AppStepTarget { kind: SiteKind; name?: string }
export interface AppRequiresStatus {
  mcps: { name: string; assigned: boolean }[]
  /** `account` and `identity` (whose account the buttons run with: the
   * agent's for a shared app, the owner's for a personal one) since 1.7;
   * absent on an older proxy. */
  providers: { provider: string; mcp: string; connected: boolean; account?: string; identity?: 'you' | 'the owner' | 'the agent' | '' }[]
}

/** APPS.md "Secrets" (since 1.7): a declared secret is a NAME a person sets
 * a value for after the deploy — never the agent, never a value in the
 * manifest. `sends_to` says the platform adds it to the app's calls to
 * that egress host (the server never sees it); `env` says the server reads
 * it itself; neither means the platform alone uses it. `set` says whether
 * a value is stored; `declared: false` marks a stored name the manifest no
 * longer declares. Never carries a value. */
export interface AppSecret {
  name: string
  required: boolean
  description?: string
  sends_to?: { host: string; header: string; prefix?: string }
  env?: boolean
  set?: boolean
  declared?: boolean
  set_by?: string
  updated_at?: string
}

/** APPS.md "Inbound hooks" (since 1.7): a public route a vendor's signed
 * events wake a handler through — the scheme, the declared secret it is
 * verified with and the handler; `header` / `prefix` for `hmac_sha256`,
 * `id_header` for `hmac_sha256` and `bearer`. */
export interface AppInboundHook {
  verify: 'stripe' | 'github' | 'hmac_sha256' | 'bearer'
  secret: string
  handler: string
  header?: string
  prefix?: string
  id_header?: string
}

export interface PinnedApp {
  id: string
  slug: string
  title: string
  scope: 'shared' | 'personal'
  /** Where the pin lives: the standing apps strip, or a chat/project Dock. */
  pin_scope?: 'standing' | 'chat' | 'project'
  /** Set on Dock pin rows (a project pin may come from another agent). */
  agent?: string
  position: number
  rel_path: string
  updated_at: string
  actions: AppAction[]
  actions_sig: string
  actions_approved: boolean
  approval_stale: boolean
  can_approve: boolean
  can_manage: boolean
  /** This viewer parked the shared app off their own strip (per-user hide —
   * the row still returns so the hidden affordance can restore it). */
  hidden_for_me: boolean
  /** Another user's personal app this viewer holds a share on ("Shared
   * with me"): never managed, hidden through the share. */
  granted?: boolean
  /** The role this viewer's action floors are judged against. */
  viewer_role?: EffectiveRole
  /** The release viewers are served (0 = the working file, not deployed yet). */
  release?: number
  /** A previous release exists to roll back to. */
  has_previous_release?: boolean
  /** A single html file, or a folder with a client and maybe a server
   * (APPS.md; the facts per kind in lib/kinds/app.ts). */
  kind?: AppKindName
  /** The tree hash the client document is addressed by (folder apps). */
  release_sha?: string
  /** The preview copy's hash, for the owner or an editor who may open it. */
  preview_sha?: string
  has_server?: boolean
  /** idle | pending (a release waiting for approval) — lib/status/appDeploy.ts. */
  deploy_state?: DeployState
  pending_release?: number
  /** A deploy is running right now (the row lock is held). */
  deploying?: boolean
  /** The server's state (lib/status/appServer.ts; the supervisor's words). */
  server?: AppServerState
  server_error?: string
  /** The manifest's other blocks, for the approval card's words. */
  files?: { read: string[]; write: string[] }
  egress?: string[]
  handlers?: AppHandlers
  exports?: AppExports
  bindings?: AppBinding[]
  requires?: AppRequires
  requires_status?: AppRequiresStatus
  /** The server's "nothing to approve": no actions and no block. Absent on
   * an older proxy, where the actions alone decide. */
  manifest_empty?: boolean
  /** APPS.md "Steps" (since 1.7): the scripts, where they run, whether a
   * live external link reaches the app, and whether only a manager may
   * approve (a shared app whose scripts receive the agent's accounts). */
  steps?: AppSteps
  step_target?: AppStepTarget
  has_live_link?: boolean
  steps_need_manager?: boolean
  /** APPS.md "Secrets" (since 1.7): the declared names with whether each
   * is set — the card's Secrets group. Absent on an older proxy. */
  secrets?: AppSecret[]
  /** APPS.md "Inbound hooks" (since 1.7): the public routes a vendor may
   * call, by hook name. Absent on an older proxy. */
  inbound?: Record<string, AppInboundHook>
  /** APPS.md "External links" (since 1.7): what a link may do — the hosts
   * the host page may open in a new tab, the app's API paths whose calls
   * from a link need a bot check, and the session cookie's lifetime. Every
   * key present with its default; absent on an older proxy. */
  external?: AppExternal
}

export interface AppExternal {
  links: string[]
  challenge: string[]
  session_days: number
}

/** The ten-minute viewer claim a folder app's page sends with its own API
 * calls; minted for a person at the keyboard, posted into the frame. */
/** The viewer claim a folder app's page sends with its own calls. `preview`
 * (an approver looking at the working copy) names the preview copy's own
 * server, so the copy's page talks to the copy's data (APPS.md). */
export async function mintViewerToken(appId: string, opts: { preview?: boolean } = {}): Promise<{ token: string; exp: number; ttl: number }> {
  const res = await apiFetch(`/v1/apps/${appId}/viewer-token${opts.preview ? '?preview=1' : ''}`, { method: 'POST' })
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'viewer token refused')
  return res.json()
}

/** The last lines of an app server's log (owner, editors, managers). */
export async function fetchAppLogs(appId: string, tail = 200): Promise<{ log: string; server: AppServerState; error: string; retry_after: number }> {
  const res = await apiFetch(`/v1/apps/${appId}/logs?tail=${tail}`)
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'logs unavailable')
  return res.json()
}

/** A folder app's deploy state: the live and pending releases and what the
 * pending one changes (APPS.md "Deploy pipeline"). */
export interface DeployStatus {
  deploy_state: DeployState
  release: number
  pending_release: number
  manifest_approved: boolean
  deploy_requires_approval: boolean
  deploying: boolean
  server: AppServerState
  error: string
  retry_after: number
  changes?: { added: string[]; removed: string[]; changed: string[] }
  /** APPS.md "Secrets": each declared name with whether it is set, and why
   * the release waits when a required one is not ("X is not set"); empty
   * when nothing waits. Absent on an older proxy. */
  secrets?: AppSecret[]
  waiting?: string
  /** The pending release's own app.json turns the per-app approval switch
   * off: a deploy never lowers it, the person's approval of that release
   * does. Absent on an older proxy. */
  lowers_approval?: boolean
}

export const useDeployStatus = (appId: string | undefined, enabled = true) =>
  useQuery({
    queryKey: ['app-deploy', appId],
    queryFn: async (): Promise<DeployStatus> => {
      const res = await apiFetch(`/v1/apps/${appId}/deploy/status`)
      if (!res.ok) throw new Error('deploy status unavailable')
      return res.json()
    },
    enabled: !!appId && enabled,
  })

/** What the card showed when the person decided: the pending release and
 * the manifest sig. A deploy while a release waits replaces both, so the
 * proxy refuses an approval naming an older pair (409). */
export interface DeployDecision {
  appId: string
  release?: number
  sig?: string
}

const useDeployDecision = (agent: string, verb: 'approve' | 'reject') => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ appId, release, sig }: DeployDecision) => {
      const res = await apiFetch(`/v1/apps/${appId}/deploy/${verb}`, {
        method: 'POST',
        ...(verb === 'approve' ? { body: JSON.stringify({ release, sig }) } : {}),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `${verb} failed`)
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
      qc.invalidateQueries({ queryKey: ['app'] })
      qc.invalidateQueries({ queryKey: ['app-deploy'] })
    },
  })
}
/** Take the pending release live (a person at the keyboard; the manifest it
 * carries is approved in the same click). */
export const useApproveDeploy = (agent: string) => useDeployDecision(agent, 'approve')
export const useRejectDeploy = (agent: string) => useDeployDecision(agent, 'reject')

/** The app's declared secrets with whether each is set, who set it and when
 * (APPS.md "Secrets") — the settings panel's list. Names only: no route ever
 * returns a value. */
export const useAppSecrets = (appId: string | undefined, enabled = true) =>
  useQuery({
    queryKey: ['app-secrets', appId],
    queryFn: async (): Promise<{ secrets: AppSecret[]; waiting: string }> => {
      const res = await apiFetch(`/v1/apps/${appId}/secrets`)
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'secrets unavailable')
      const body = await res.json()
      return { secrets: body?.secrets ?? [], waiting: body?.waiting ?? '' }
    },
    enabled: !!appId && enabled,
  })

const invalidateSecrets = (qc: ReturnType<typeof useQueryClient>, agent: string) => {
  qc.invalidateQueries({ queryKey: ['app-secrets'] })
  qc.invalidateQueries({ queryKey: ['app-deploy'] })
  qc.invalidateQueries({ queryKey: ['apps', agent] })
  qc.invalidateQueries({ queryKey: ['app'] })
}

/** Set or replace one declared secret's value (a person at the keyboard;
 * the running server restarts with it on the next request). The value goes
 * out once and never comes back. */
export const useSetAppSecret = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ appId, name, value }: { appId: string; name: string; value: string }) => {
      const res = await apiFetch(`/v1/apps/${appId}/secrets/${encodeURIComponent(name)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'the value was not saved')
    },
    onSettled: () => invalidateSecrets(qc, agent),
  })
}

export const useDeleteAppSecret = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ appId, name }: { appId: string; name: string }) => {
      const res = await apiFetch(`/v1/apps/${appId}/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'the value was not removed')
    },
    onSettled: () => invalidateSecrets(qc, agent),
  })
}

/** Delete an app with its data: the row, the releases, the database, the
 * folder. The dialog asks for the slug first. */
export const usePurgeApp = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ appId, confirm }: { appId: string; confirm: string }) => {
      const res = await apiFetch(`/v1/apps/${appId}/purge`, {
        method: 'POST', body: JSON.stringify({ confirm }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'delete failed')
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
      qc.invalidateQueries({ queryKey: ['app'] })
      qc.invalidateQueries({ queryKey: ['chat-pins'] })
    },
  })
}

/** A Dock FILE pin — a reference only: the Dock reads the content through
 * the files API, where the viewer's own role decides what renders. */
export interface PinnedFileRef {
  id: string
  agent: string
  rel_path: string
  title: string
  pin_scope: 'chat' | 'project'
  updated_at: string
}

/** The chat's Dock pins: its own chat-scoped app, (project chats) the
 * project-scoped one, and the scope's FILE pins. App rows are shaped exactly
 * like /v1/apps rows, so AppFrame + the approval card work unchanged. */
export interface ChatPins {
  chat: PinnedApp | null
  project: PinnedApp | null
  files?: PinnedFileRef[]
}

export const useChatPins = (chatId: string | undefined) =>
  useQuery({
    queryKey: ['chat-pins', chatId],
    queryFn: async (): Promise<ChatPins> => {
      const res = await apiFetch(`/v1/chats/${chatId}/pins`)
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    enabled: !!chatId,
    staleTime: 15_000,
  })

/** One app row for the full-screen page — the list shape plus its agent and
 * (chat-scoped pins) its chat. Denied is the same 404 as missing. */
export const useApp = (appId: string | undefined) =>
  useQuery({
    queryKey: ['app', appId],
    queryFn: async (): Promise<(PinnedApp & { agent: string; chat_id: string }) | null> => {
      const res = await apiFetch(`/v1/apps/${appId}`)
      if (res.status === 404) return null
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    enabled: !!appId,
    staleTime: 30_000,
  })

/** The app's state document (APPS.md "Live apps"), written by the
 * agent and read by the page. */
export interface AppState {
  doc: Record<string, unknown>
  rev: number
}

const keepHigherRev = (old: unknown, next: unknown): unknown => {
  const o = old as AppState | undefined
  const n = next as AppState | undefined
  return o && n && o.rev > n.rev ? o : next
}

/** Fetched once per app and kept current by `app_state` frames. Every cache
 * write keeps the higher rev, so a fetch that resolves after a newer frame
 * never turns the page back; a socket reconnect invalidates it. */
export const useAppState = (appId: string | undefined) =>
  useQuery({
    queryKey: ['app-state', appId],
    queryFn: async (): Promise<AppState> => {
      const res = await apiFetch(`/v1/apps/${appId}/state`)
      if (!res.ok) throw new Error(await res.text())
      return res.json()
    },
    enabled: !!appId,
    staleTime: Infinity,
    structuralSharing: keepHigherRev,
  })

export const useApps = (agent: string) =>
  useQuery({
    queryKey: ['apps', agent],
    queryFn: async (): Promise<PinnedApp[]> => {
      const res = await apiFetch(`/v1/apps?agent=${encodeURIComponent(agent)}`)
      if (!res.ok) throw new Error(await res.text())
      const data = await res.json()
      return data.apps ?? []
    },
    enabled: !!agent,
    staleTime: 30_000,
  })

export const useApproveApp = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ appId, sig }: { appId: string; sig: string }) => {
      const res = await apiFetch(`/v1/apps/${appId}/approve`, {
        method: 'POST',
        body: JSON.stringify({ sig }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Approve failed')
    },
    // 409 (manifest changed) also lands here — the refetch shows the new card.
    // Dock pins render off ['chat-pins'] — same approval endpoint, so both
    // caches refresh (the extra invalidate is a no-op without mounted pins).
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
      qc.invalidateQueries({ queryKey: ['chat-pins'] })
      qc.invalidateQueries({ queryKey: ['app'] })
    },
  })
}

export const useUnpinApp = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (appId: string) => {
      const res = await apiFetch(`/v1/apps/${appId}`, { method: 'DELETE' })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Unpin failed')
    },
    // A standing app can also surface as a Dock pin row — keep both fresh.
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
      qc.invalidateQueries({ queryKey: ['chat-pins'] })
    },
  })
}

/** Per-user hide of a SHARED app (any role): parks it off MY strip only —
 * the team's view and the row itself are untouched. */
export const useHideAppForMe = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (appId: string) => {
      const res = await apiFetch(`/v1/apps/${appId}/hide`, { method: 'POST' })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Hide failed')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['apps', agent] }),
  })
}

export const useUnhideAppForMe = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (appId: string) => {
      const res = await apiFetch(`/v1/apps/${appId}/unhide`, { method: 'POST' })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Restore failed')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['apps', agent] }),
  })
}

/** A platform-catalog feed snapshot for one app's viewer (APPS.md
 * "Platform catalog"): the rows and the sequence number deltas continue from. */
export interface CatalogSnapshot {
  rows: Record<string, unknown>[]
  seq: number
}

export async function fetchCatalogFeed(appId: string, feed: string): Promise<CatalogSnapshot> {
  const res = await apiFetch(`/v1/apps/${appId}/catalog/${encodeURIComponent(feed)}`)
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `feed ${feed} unavailable`)
  return res.json()
}

/** One platform method call as the viewer; the server judges the declared
 * entry, its floor and the args. */
export async function callPlatformMethod(
  appId: string, method: string, args: unknown,
): Promise<{ ok: boolean; result?: unknown; reason?: string }> {
  const res = await apiFetch(`/v1/apps/${appId}/catalog/${encodeURIComponent(method)}`, {
    method: 'POST', body: JSON.stringify({ args: args ?? null }),
  })
  if (!res.ok) {
    return { ok: false, reason: (await res.json().catch(() => null))?.detail || 'unavailable' }
  }
  return res.json()
}

/** What the rollback route answers: the release that serves now and, for
 * a folder app, whether its database went back with it. */
export interface RollbackResult {
  release: number
  screens: number
  /** Folder apps only: absent on a single-file app. */
  db_restored?: boolean
  snapshot?: string
}

/** The sentence the host shows after a rollback. Three outcomes: a file
 * app, a folder app whose database was restored, and a folder app that had
 * no copy from before the release it left (it keeps its current data). */
export function rollbackNoticeText(app: PinnedApp, left: number, r: RollbackResult): string {
  const head = `Release ${r.release} serves again.`
  if (!appKind(app).keepsData || r.db_restored === undefined) return head
  if (r.db_restored) {
    return `${head} The database was restored from before release ${left} went live; the newer writes are kept in a snapshot beside the releases.`
  }
  return `${head} No copy from before release ${left} exists, so the app keeps its current data.`
}

/** Point the app back at its previous release (editor+ on shared rows, the
 * owner on personal ones); every open frame reloads on the deploy frame. */
export const useRollbackApp = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (appId: string): Promise<RollbackResult> => {
      const res = await apiFetch(`/v1/apps/${appId}/rollback`, { method: 'POST' })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Roll back failed')
      return res.json()
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['apps', agent] })
      qc.invalidateQueries({ queryKey: ['chat-pins'] })
      qc.invalidateQueries({ queryKey: ['app'] })
    },
  })
}

export const useReorderApps = (agent: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (ids: string[]) => {
      const res = await apiFetch('/v1/apps/order', {
        method: 'PUT',
        body: JSON.stringify({ agent, ids }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || 'Reorder failed')
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['apps', agent] }),
  })
}

export interface AppActionResult {
  status: string
  reason?: string
  result?: string
  run_id?: string
}

export interface AppActionCall {
  call_id: string
  action_id: string
  args?: unknown
}

/** Execute declared fire_task / mcp_tool actions over REST in one batch
 * (send_prompt rides the chat WS). The server streams one JSON line per
 * call as it completes; `onResult` fires per line with the single-call
 * shape — fire_task `sent`, mcp_tool `done` with the tool's text (or
 * `error`), refusals `denied` with a reason. Every call gets exactly one
 * result: a whole-batch refusal or a transport failure resolves the
 * remaining calls `denied` here. Args pass the server's user-approved
 * schema gate — never trusted client-side. */
export async function fireAppActions(
  appId: string,
  calls: AppActionCall[],
  onResult: (callId: string, r: AppActionResult) => void,
): Promise<void> {
  const pending = new Set(calls.map((c) => c.call_id))
  const settle = (callId: string, r: AppActionResult) => {
    if (!pending.delete(callId)) return
    onResult(callId, r.status === 'ok' ? { ...r, status: 'sent' } : r)
  }
  const failRest = (reason: string) => {
    for (const id of [...pending]) settle(id, { status: 'denied', reason })
  }
  const takeLine = (line: string) => {
    if (!line.trim()) return
    let row: any
    try { row = JSON.parse(line) } catch { return }
    if (row && typeof row.call_id === 'string') settle(row.call_id, row)
  }
  try {
    const res = await apiFetch(`/v1/apps/${appId}/actions/batch`, {
      method: 'POST',
      body: JSON.stringify({ calls }),
    })
    if (!res.ok) {
      const detail = (await res.json().catch(() => null))?.detail || `HTTP ${res.status}`
      return failRest(String(detail))
    }
    if (res.body && typeof res.body.getReader === 'function') {
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buf = ''
      for (;;) {
        const { value, done } = await reader.read()
        buf += decoder.decode(value ?? new Uint8Array(), { stream: !done })
        let nl = buf.indexOf('\n')
        while (nl >= 0) {
          takeLine(buf.slice(0, nl))
          buf = buf.slice(nl + 1)
          nl = buf.indexOf('\n')
        }
        if (done) break
      }
      takeLine(buf)
    } else {
      for (const line of (await res.text()).split('\n')) takeLine(line)
    }
    failRest('no result')
  } catch {
    failRest('Network error — the call may not have reached the server')
  }
}

/** Ask the platform to keep this app's tool manager warm (fire and forget;
 * the server answers 204 when there is nothing to build). */
export function warmApp(appId: string): void {
  void apiFetch(`/v1/apps/${appId}/warm`, { method: 'POST' }).catch(() => { /* best effort */ })
}
