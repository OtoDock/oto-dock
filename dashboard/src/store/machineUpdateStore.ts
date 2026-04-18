// Per-machine update lifecycle state. Driven by the dashboard WS events
// `satellite_updating`, `satellite_updated`, `satellite_update_failed`
// (emitted by proxy/ws/satellite.py helpers when a paired satellite is
// being version-mismatched-and-pushed-the-new-tarball, has come back on
// the new version, or has rolled back).
//
// Drives the MachineUpdateBanner component + lets AgentChat queue user
// messages locally while the underlying machine is mid-restart.

import { create } from 'zustand'

export type MachineUpdateStatus = 'updating' | 'updated' | 'failed'

export interface MachineUpdateSlice {
  machineId: string
  machineName: string
  fromVersion: string
  toVersion: string
  startedAt: number
  status: MachineUpdateStatus
  error?: string
  rolledBackTo?: string
}

interface MachineUpdateStoreState {
  byMachine: Record<string, MachineUpdateSlice>

  beginUpdate: (data: {
    machine_id: string
    machine_name?: string
    from_version?: string
    to_version: string
    started_at?: string
  }) => void

  markUpdated: (data: {
    machine_id: string
    machine_name?: string
    version: string
  }) => void

  markFailed: (data: {
    machine_id: string
    machine_name?: string
    error: string
    rolled_back_to?: string
  }) => void

  dismiss: (machineId: string) => void

  // Connect-time reconciliation against the proxy's authoritative in-flight set
  // (`satellite_update_sync`). Clears stale 'updating' banners whose update
  // finished while the dashboard was briefly disconnected (the transient
  // `satellite_updated` was missed), and surfaces any in-flight update the
  // dashboard connected too late to see live.
  reconcile: (inflight: Array<{
    machine_id: string
    machine_name?: string
    from_version?: string
    to_version: string
  }>) => void
}

export const useMachineUpdateStore = create<MachineUpdateStoreState>((set) => ({
  byMachine: {},

  beginUpdate: (data) =>
    set((s) => ({
      byMachine: {
        ...s.byMachine,
        [data.machine_id]: {
          machineId: data.machine_id,
          machineName: data.machine_name ?? '',
          fromVersion: data.from_version ?? '',
          toVersion: data.to_version,
          startedAt: Date.now(),
          status: 'updating',
        },
      },
    })),

  markUpdated: (data) =>
    set((s) => {
      const prev = s.byMachine[data.machine_id]
      return {
        byMachine: {
          ...s.byMachine,
          [data.machine_id]: {
            machineId: data.machine_id,
            machineName: data.machine_name ?? prev?.machineName ?? '',
            fromVersion: prev?.fromVersion ?? '',
            toVersion: data.version,
            startedAt: prev?.startedAt ?? Date.now(),
            status: 'updated',
          },
        },
      }
    }),

  markFailed: (data) =>
    set((s) => {
      const prev = s.byMachine[data.machine_id]
      return {
        byMachine: {
          ...s.byMachine,
          [data.machine_id]: {
            machineId: data.machine_id,
            machineName: data.machine_name ?? prev?.machineName ?? '',
            fromVersion: prev?.fromVersion ?? '',
            toVersion: prev?.toVersion ?? '',
            startedAt: prev?.startedAt ?? Date.now(),
            status: 'failed',
            error: data.error,
            rolledBackTo: data.rolled_back_to,
          },
        },
      }
    }),

  dismiss: (machineId) =>
    set((s) => {
      if (!(machineId in s.byMachine)) return s
      const { [machineId]: _, ...rest } = s.byMachine
      return { byMachine: rest }
    }),

  reconcile: (inflight) =>
    set((s) => {
      const live = new Set(inflight.map((u) => u.machine_id))
      const next: Record<string, MachineUpdateSlice> = {}
      // Keep non-'updating' slices (a freshly-flashed 'updated' / a sticky
      // 'failed') and any 'updating' that the proxy confirms is still in flight.
      for (const [mid, slice] of Object.entries(s.byMachine)) {
        if (slice.status !== 'updating' || live.has(mid)) next[mid] = slice
        // else: drop — the update finished while we were disconnected.
      }
      // Surface an in-flight update we connected too late to see live.
      for (const u of inflight) {
        if (!next[u.machine_id]) {
          next[u.machine_id] = {
            machineId: u.machine_id,
            machineName: u.machine_name ?? '',
            fromVersion: u.from_version ?? '',
            toVersion: u.to_version,
            startedAt: Date.now(),
            status: 'updating',
          }
        }
      }
      return { byMachine: next }
    }),
}))

export const useMachineUpdateSlice = (machineId: string | null | undefined) =>
  useMachineUpdateStore((s) => (machineId ? s.byMachine[machineId] : undefined))
