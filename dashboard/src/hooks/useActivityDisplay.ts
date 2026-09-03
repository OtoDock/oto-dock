/**
 * Compact-activity display preference — localStorage-backed, per user/device
 * (like theme; deliberately NOT server-persisted). Provider-less on purpose:
 * the pref is read in both the ChatMessages and UserSettings trees, so a tiny
 * useSyncExternalStore hook beats threading a context through both.
 *
 * Cross-tab updates arrive via the `storage` event; same-tab updates (the
 * Appearance toggle) go through the in-module emitter — `storage` never fires
 * in the tab that wrote the value.
 */
import { useSyncExternalStore } from 'react'

export type ActivityDisplayMode = 'compact' | 'detailed'

const STORAGE_KEY = 'activity-display'

const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  const onStorage = (e: StorageEvent) => {
    // key === null means "storage cleared" — re-read then too.
    if (e.key === STORAGE_KEY || e.key === null) listener()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

export function getActivityDisplay(): ActivityDisplayMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'detailed' ? 'detailed' : 'compact'
  } catch {
    return 'compact'  // storage unavailable (privacy mode) — default wins
  }
}

export function setActivityDisplay(mode: ActivityDisplayMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    // Storage unavailable — still notify so the session flips in memory…
    // except useSyncExternalStore re-reads getActivityDisplay, which will
    // keep returning the default. Acceptable: no storage, no preference.
  }
  for (const l of listeners) l()
}

export function useActivityDisplay(): ActivityDisplayMode {
  return useSyncExternalStore(subscribe, getActivityDisplay, getActivityDisplay)
}
