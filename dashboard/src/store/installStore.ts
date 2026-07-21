// Per-install lifecycle state, keyed by `${machineId}::${agent}`. Driven
// by the dashboard WS events install_started / install_mcp_plan /
// install_progress / mcp_install_failed / install_heartbeat /
// install_done / install_failed.
//
// Why a separate store from chatStore? Install is a satellite-level
// operation shared across chats — a new chat or task run for the same
// (machine, agent) reuses the same install slot. The user can open the
// new-chat page for agent X, trigger an install, navigate to a different
// chat, come back, and still see the install progress.
//
// Mirrors machineUpdateStore.ts shape (the closest existing precedent —
// by-machine_id keyed, mutators called from WS dispatcher, no persist).

import { create } from 'zustand'

import { useChatStore } from './chatStore'

export type InstallStatus = 'installing' | 'verifying' | 'done' | 'failed'

export interface InstallProgressEntry {
  phase: string
  pct: number
  message: string
}

export interface InstallFailure {
  mcp: string
  error: string
}

export interface InstallSlice {
  machineId: string
  agent: string
  status: InstallStatus
  mcps: string[]                                       // ordered union of plan + discovered
  progress: Record<string, InstallProgressEntry>       // by mcp name
  failures: InstallFailure[]
  startedAt: number
  lastEventAt: number
  doneAt: number | null                                // set on install_done, drives done-grace UI window
  error: string | null
  warmupFailures: string[]                             // MCPs that installed but failed the boot check (advisory)
}

interface InstallStoreState {
  byKey: Record<string, InstallSlice>

  begin: (data: { machine_id: string; agent: string }) => void
  setPlan: (
    data: {
      machine_id: string
      agent: string
      mcps_to_install?: string[]
      mcps_to_update?: string[]
    },
  ) => void
  recordProgress: (
    data: {
      machine_id: string
      agent: string
      mcp: string
      phase: string
      pct: number
      message: string
    },
  ) => void
  recordFailure: (
    data: { machine_id: string; agent: string; mcp: string; error: string },
  ) => void
  touchHeartbeat: (data: { machine_id: string; agent: string }) => void
  verifying: (data: { machine_id: string; agent: string }) => void
  finish: (
    data: { machine_id: string; agent: string; warmup_failures?: string[] },
  ) => void
  fail: (data: { machine_id: string; agent: string; error: string }) => void
  clear: (machineId: string, agent: string) => void
  /** Drop every NON-terminal slice (installing/verifying). Called on WS
   * (re)connect: any genuinely in-flight install is immediately re-fed by
   * the server's connect replay (install_registry.snapshot_inflight), so
   * what this really removes is ghosts — a lifecycle whose done/failed
   * frame was lost to a dropped socket (e.g. a proxy restart mid-install)
   * would otherwise show "Preparing remote environment…" forever. Terminal
   * slices keep their done-grace / failed-with-retry UI. */
  clearInFlight: () => void
}

export const installKey = (machineId: string, agent: string): string =>
  `${machineId}::${agent}`

const _empty = (machineId: string, agent: string): InstallSlice => ({
  machineId,
  agent,
  status: 'installing',
  mcps: [],
  progress: {},
  failures: [],
  startedAt: Date.now(),
  lastEventAt: Date.now(),
  doneAt: null,
  error: null,
  warmupFailures: [],
})

export const useInstallStore = create<InstallStoreState>((set) => ({
  byKey: {},

  begin: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k]
      // Idempotent: a second start_session for the same (machine, agent)
      // shares the install slot — don't wipe an in-progress slice or
      // drop the plan / partial progress we've already accumulated. Only
      // reset when the previous install is in a terminal state (done /
      // failed) — that's a fresh install cycle. 'verifying' is mid-flight
      // (the post-install pre-warm tail) so it's preserved like 'installing'.
      if (prev && (prev.status === 'installing' || prev.status === 'verifying')) {
        return { byKey: { ...s.byKey, [k]: { ...prev, lastEventAt: Date.now() } } }
      }
      return {
        byKey: {
          ...s.byKey,
          [k]: _empty(data.machine_id, data.agent),
        },
      }
    }),

  setPlan: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      const seen = new Set(prev.mcps)
      const merged = [...prev.mcps]
      for (const name of [...(data.mcps_to_install ?? []), ...(data.mcps_to_update ?? [])]) {
        if (!seen.has(name)) {
          merged.push(name)
          seen.add(name)
        }
      }
      return {
        byKey: {
          ...s.byKey,
          [k]: { ...prev, mcps: merged, lastEventAt: Date.now() },
        },
      }
    }),

  recordProgress: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      const mcps = prev.mcps.includes(data.mcp) ? prev.mcps : [...prev.mcps, data.mcp]
      return {
        byKey: {
          ...s.byKey,
          [k]: {
            ...prev,
            mcps,
            progress: {
              ...prev.progress,
              [data.mcp]: { phase: data.phase, pct: data.pct, message: data.message },
            },
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  recordFailure: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      // De-dupe by MCP so retries don't pile up rows.
      const others = prev.failures.filter((f) => f.mcp !== data.mcp)
      return {
        byKey: {
          ...s.byKey,
          [k]: {
            ...prev,
            failures: [...others, { mcp: data.mcp, error: data.error }],
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  touchHeartbeat: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      // Seed an empty slice if this is the first event a late-attaching WS
      // received (it missed install_started). Without this the 15s heartbeat
      // keepalive was a no-op and the bar never materialized until a page
      // refresh replayed history. Mirrors recordProgress / verifying / finish.
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      return {
        byKey: { ...s.byKey, [k]: { ...prev, lastEventAt: Date.now() } },
      }
    }),

  // Post-install pre-warm phase: MCPs are installed, the satellite
  // is booting each to warm the cache + confirm it answers `initialize`.
  // Mid-flight — preserve plan/progress; just flip status so the bar shows
  // "Checking MCPs…". Create the slice if a reconnect missed install_started.
  verifying: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      if (prev.status === 'done' || prev.status === 'failed') return s
      return {
        byKey: {
          ...s.byKey,
          [k]: { ...prev, status: 'verifying', lastEventAt: Date.now() },
        },
      }
    }),

  finish: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      return {
        byKey: {
          ...s.byKey,
          [k]: {
            ...prev,
            status: 'done',
            doneAt: Date.now(),
            lastEventAt: Date.now(),
            warmupFailures: data.warmup_failures ?? prev.warmupFailures,
          },
        },
      }
    }),

  fail: (data) =>
    set((s) => {
      const k = installKey(data.machine_id, data.agent)
      const prev = s.byKey[k] ?? _empty(data.machine_id, data.agent)
      return {
        byKey: {
          ...s.byKey,
          [k]: {
            ...prev,
            status: 'failed',
            error: data.error,
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  clear: (machineId, agent) =>
    set((s) => {
      const k = installKey(machineId, agent)
      if (!(k in s.byKey)) return s
      const { [k]: _, ...rest } = s.byKey
      return { byKey: rest }
    }),

  clearInFlight: () =>
    set((s) => {
      const rest: Record<string, InstallSlice> = {}
      let dropped = false
      for (const [k, slice] of Object.entries(s.byKey)) {
        if (slice.status === 'installing' || slice.status === 'verifying') {
          dropped = true
          continue
        }
        rest[k] = slice
      }
      return dropped ? { byKey: rest } : s
    }),
}))

// ─── selector hooks ──────────────────────────────────────────────────────

// Look up an install slice by chatId — convenience for the chat surface,
// which knows chatId but not (machineId, agent) directly. Falls back to
// undefined if chat isn't in chatStore yet (e.g. brand-new chat-id minted
// after install already started) — call sites should also accept explicit
// (machineId, agent) for the new-chat page case (chat_id still null).
export const useInstallSliceForChat = (
  chatId: string | null | undefined,
): InstallSlice | undefined => {
  const chatSlice = useChatStore((s) => (chatId ? s.byChat[chatId] : undefined))
  return useInstallStore((s) => {
    if (!chatSlice?.executionTarget || !chatSlice.agent) return undefined
    return s.byKey[installKey(chatSlice.executionTarget, chatSlice.agent)]
  })
}

// Direct lookup by (machineId, agent) — used by AgentChat new-chat page
// when chatId is null but agent + execution target are known from props.
export const useInstallSliceByKey = (
  machineId: string | null | undefined,
  agent: string | null | undefined,
): InstallSlice | undefined =>
  useInstallStore((s) =>
    machineId && agent ? s.byKey[installKey(machineId, agent)] : undefined,
  )

// Scan by agent across all machines. Used by the new-chat page where the
// agent is known but the chat_id hasn't been minted yet and execution_target
// only becomes known once warmup_ready arrives. The install_started event
// itself carries machine_id + agent so this selector resolves the slice as
// soon as the first event lands. Returns the first match — multi-machine
// for the same agent is a rare edge case (admin-shared + user-paired); v1
// shows the most-recently-started install.
export const useInstallSliceByAgent = (
  agent: string | null | undefined,
): InstallSlice | undefined =>
  useInstallStore((s) => {
    if (!agent) return undefined
    let best: InstallSlice | undefined
    for (const slice of Object.values(s.byKey)) {
      if (slice.agent !== agent) continue
      if (!best || slice.startedAt > best.startedAt) best = slice
    }
    return best
  })
