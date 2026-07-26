// Workspace upload/sync transfer progress, keyed by transfer_id. Driven by
// (a) the local XHR upload loop (phase 1: dashboard → proxy) and (b) the
// dashboard WS events transfer_started / transfer_machine_state /
// transfer_progress / transfer_done / transfer_state (phase 2: proxy →
// remote machines, ONE row per target machine — core/remote/transfer_registry).
//
// A locally-initiated upload is born under a `local:<uuid>` key while the XHR
// runs, then re-keyed to the server transfer_id from the POST /v1/upload
// response (linkUpload). Server events for the same id may arrive BEFORE the
// response resolves — applyStarted/linkUpload merge by id either way.
//
// Mirrors installStore.ts (the closest precedent — WS-dispatcher-driven
// mutators, done-grace linger, clearInFlight on reconnect + server replay).

import { useMemo } from 'react'
import { create } from 'zustand'

import type { ScopeKey } from '../components/workspace/sections'

export type MachinePushState = 'queued' | 'active' | 'done' | 'failed'

export interface TransferMachineRow {
  machineId: string
  name: string
  state: MachinePushState
  bytesSent: number
  bytesTotal: number
  error?: string
}

export interface TransferItem {
  id: string                       // transfer_id, or `local:<uuid>` pre-link
  agent: string
  relPath: string                  // best-effort during phase 1; server value after link
  filename: string
  kind: 'upload' | 'sync'
  phase: 1 | 2                     // 1 while the XHR runs (upload kind only)
  uploadSent: number
  uploadTotal: number
  uploadFailed: boolean
  machines: Record<string, TransferMachineRow>
  startedAt: number
  lastEventAt: number
  doneAt: number | null            // drives the linger window before prune()
}

// Terminal items linger this long so done/failed states are actually seen
// before the icon auto-hides. Slightly shorter than the server registry's
// 6s DONE_LINGER_S so a replayed item never outlives its server record.
const DONE_LINGER_MS = 5000

/** Map an agent-relative path to the workspace section it belongs to.
 * Foreign users' personal paths return null (the server never emits those to
 * us anyway — this is defense-in-depth for rendering). */
export function sectionForRelPath(
  relPath: string, username?: string,
): ScopeKey | null {
  const p = relPath.replace(/^\/+/, '')
  if (p.startsWith('users/')) {
    const parts = p.split('/')
    if (parts.length < 3 || !username || parts[1] !== username) return null
    if (parts[2] === 'workspace') return 'my-workspace'
    if (parts[2] === 'context') return 'my-context'
    return null
  }
  if (p.startsWith('workspace/') || p === 'workspace') return 'agent-workspace'
  if (p.startsWith('knowledge/') || p === 'knowledge') return 'agent-knowledge'
  if (p.startsWith('config/') || p === 'config') return 'agent-config'
  return null
}

/** An item still deserves the toolbar icon: uploading, any machine row
 * pending, or inside the done-linger window. */
export function isTransferActive(t: TransferItem, now = Date.now()): boolean {
  if (t.doneAt !== null) return now - t.doneAt < DONE_LINGER_MS
  if (t.phase === 1 && !t.uploadFailed) return true
  const rows = Object.values(t.machines)
  if (rows.some((r) => r.state === 'queued' || r.state === 'active')) return true
  return true // phase 2 with no terminal stamp yet (e.g. awaiting first event)
}

interface WsMachineRow {
  machine_id: string
  name?: string
  state?: MachinePushState
  bytes_sent?: number
  bytes_total?: number
  error?: string
}

function rowFromWs(m: WsMachineRow): TransferMachineRow {
  return {
    machineId: m.machine_id,
    name: m.name || m.machine_id.slice(0, 8),
    state: m.state || 'queued',
    bytesSent: m.bytes_sent ?? 0,
    bytesTotal: m.bytes_total ?? 0,
    ...(m.error ? { error: m.error } : {}),
  }
}

interface TransferStoreState {
  byId: Record<string, TransferItem>

  beginLocalUpload: (d: {
    clientId: string; agent: string; targetDir: string
    filename: string; bytesTotal: number
  }) => void
  updateLocalUpload: (clientId: string, sent: number) => void
  failLocalUpload: (clientId: string, error?: string) => void
  linkUpload: (clientId: string, d: {
    transferId: string; relPath: string; remotePush: boolean
  }) => void

  applyStarted: (msg: any) => void
  applyMachineState: (msg: any) => void
  applyProgress: (msg: any) => void
  applyDone: (msg: any) => void
  applySnapshot: (msg: any) => void

  prune: () => void
  clearInFlight: () => void
}

export const useTransferStore = create<TransferStoreState>((set, get) => ({
  byId: {},

  beginLocalUpload: ({ clientId, agent, targetDir, filename, bytesTotal }) =>
    set((s) => ({
      byId: {
        ...s.byId,
        [clientId]: {
          id: clientId,
          agent,
          relPath: targetDir ? `${targetDir.replace(/\/+$/, '')}/${filename}` : filename,
          filename,
          kind: 'upload',
          phase: 1,
          uploadSent: 0,
          uploadTotal: bytesTotal,
          uploadFailed: false,
          machines: {},
          startedAt: Date.now(),
          lastEventAt: Date.now(),
          doneAt: null,
        },
      },
    })),

  updateLocalUpload: (clientId, sent) =>
    set((s) => {
      const t = s.byId[clientId]
      if (!t) return s
      return {
        byId: {
          ...s.byId,
          [clientId]: { ...t, uploadSent: sent, lastEventAt: Date.now() },
        },
      }
    }),

  failLocalUpload: (clientId) =>
    set((s) => {
      const t = s.byId[clientId]
      if (!t) return s
      return {
        byId: {
          ...s.byId,
          [clientId]: {
            ...t, uploadFailed: true, doneAt: Date.now(),
            lastEventAt: Date.now(),
          },
        },
      }
    }),

  linkUpload: (clientId, { transferId, relPath, remotePush }) =>
    set((s) => {
      const local = s.byId[clientId]
      if (!local) return s
      const byId = { ...s.byId }
      delete byId[clientId]
      // A transfer_started for this id may have landed first — merge its
      // machine rows into the re-keyed item.
      const early = byId[transferId]
      byId[transferId] = {
        ...local,
        id: transferId,
        relPath,
        phase: 2,
        uploadSent: local.uploadTotal,
        machines: early ? early.machines : {},
        // No connected machine will receive it → complete now (linger).
        doneAt: remotePush ? (early?.doneAt ?? null) : Date.now(),
        lastEventAt: Date.now(),
      }
      return { byId }
    }),

  applyStarted: (msg) =>
    set((s) => {
      const existing = s.byId[msg.transfer_id]
      const machines: Record<string, TransferMachineRow> = {}
      for (const m of msg.machines || []) machines[m.machine_id] = rowFromWs(m)
      return {
        byId: {
          ...s.byId,
          [msg.transfer_id]: {
            id: msg.transfer_id,
            agent: msg.agent_slug,
            relPath: msg.rel_path,
            filename: msg.filename || msg.rel_path.split('/').pop() || msg.rel_path,
            kind: msg.kind === 'upload' ? 'upload' : 'sync',
            // Keep local phase-1 state when this id was already linked;
            // otherwise the item starts at phase 2 (server-side push).
            phase: existing?.phase === 1 ? 1 : 2,
            uploadSent: existing?.uploadSent ?? 0,
            uploadTotal: existing?.uploadTotal ?? msg.bytes_total ?? 0,
            uploadFailed: existing?.uploadFailed ?? false,
            machines,
            startedAt: existing?.startedAt ?? Date.now(),
            lastEventAt: Date.now(),
            doneAt: null,
          },
        },
      }
    }),

  applyMachineState: (msg) =>
    set((s) => {
      const t = s.byId[msg.transfer_id]
      if (!t) return s
      const row = t.machines[msg.machine_id] || rowFromWs({ machine_id: msg.machine_id })
      const machines = {
        ...t.machines,
        [msg.machine_id]: {
          ...row,
          state: msg.state as MachinePushState,
          ...(msg.state === 'done' ? { bytesSent: row.bytesTotal } : {}),
          ...(msg.error ? { error: msg.error } : {}),
        },
      }
      return {
        byId: { ...s.byId, [msg.transfer_id]: { ...t, machines, lastEventAt: Date.now() } },
      }
    }),

  applyProgress: (msg) =>
    set((s) => {
      const t = s.byId[msg.transfer_id]
      if (!t) return s
      const row = t.machines[msg.machine_id] || rowFromWs({ machine_id: msg.machine_id })
      const machines = {
        ...t.machines,
        [msg.machine_id]: {
          ...row,
          state: row.state === 'queued' ? 'active' as const : row.state,
          bytesSent: msg.bytes_sent ?? row.bytesSent,
          bytesTotal: msg.bytes_total ?? row.bytesTotal,
        },
      }
      return {
        byId: { ...s.byId, [msg.transfer_id]: { ...t, machines, lastEventAt: Date.now() } },
      }
    }),

  applyDone: (msg) =>
    set((s) => {
      const t = s.byId[msg.transfer_id]
      if (!t) return s
      return {
        byId: {
          ...s.byId,
          [msg.transfer_id]: { ...t, doneAt: Date.now(), lastEventAt: Date.now() },
        },
      }
    }),

  applySnapshot: (msg) =>
    set((s) => {
      const existing = s.byId[msg.transfer_id]
      const machines: Record<string, TransferMachineRow> = {}
      for (const m of msg.machines || []) machines[m.machine_id] = rowFromWs(m)
      return {
        byId: {
          ...s.byId,
          [msg.transfer_id]: {
            id: msg.transfer_id,
            agent: msg.agent_slug,
            relPath: msg.rel_path,
            filename: msg.filename || msg.rel_path.split('/').pop() || msg.rel_path,
            kind: msg.kind === 'upload' ? 'upload' : 'sync',
            phase: existing?.phase === 1 ? 1 : 2,
            uploadSent: existing?.uploadSent ?? 0,
            uploadTotal: existing?.uploadTotal ?? msg.bytes_total ?? 0,
            uploadFailed: existing?.uploadFailed ?? false,
            machines,
            startedAt: existing?.startedAt ?? Date.now(),
            lastEventAt: Date.now(),
            doneAt: msg.done ? (existing?.doneAt ?? Date.now()) : null,
          },
        },
      }
    }),

  prune: () =>
    set((s) => {
      const now = Date.now()
      const byId: Record<string, TransferItem> = {}
      let changed = false
      for (const [id, t] of Object.entries(s.byId)) {
        if (t.doneAt !== null && now - t.doneAt > DONE_LINGER_MS) {
          changed = true
          continue
        }
        byId[id] = t
      }
      return changed ? { byId } : s
    }),

  // On WS (re)connect: drop non-terminal server-driven state — genuinely
  // in-flight transfers are immediately re-fed by the server's connect
  // replay (transfer_registry.snapshot_inflight), so anything not replayed
  // was a ghost from the dropped socket. KEEP phase-1 items: they are
  // locally owned (the XHR doesn't ride the WS).
  clearInFlight: () =>
    set((s) => {
      const byId: Record<string, TransferItem> = {}
      let changed = false
      for (const [id, t] of Object.entries(s.byId)) {
        if (t.phase === 1 || t.doneAt !== null) {
          byId[id] = t
        } else {
          changed = true
        }
      }
      return changed ? { byId } : s
    }),
}))

/** Items for one agent + section, sorted oldest-first (stable list). */
export function transfersForSection(
  byId: Record<string, TransferItem>,
  agent: string,
  scope: ScopeKey | '',
  username?: string,
): TransferItem[] {
  if (!scope) return []
  return Object.values(byId)
    .filter((t) => t.agent === agent && sectionForRelPath(t.relPath, username) === scope)
    .sort((a, b) => a.startedAt - b.startedAt)
}

export const useTransfersForSection = (
  agent: string, scope: ScopeKey | '', username?: string,
): TransferItem[] => {
  // Select the STABLE byId reference and derive with useMemo — a selector
  // that builds a fresh array per call makes useSyncExternalStore see an
  // ever-changing snapshot (React #185 infinite update loop).
  const byId = useTransferStore((s) => s.byId)
  return useMemo(
    () => transfersForSection(byId, agent, scope, username),
    [byId, agent, scope, username],
  )
}
