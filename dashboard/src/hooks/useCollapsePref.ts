/**
 * Persisted open/closed preference for collapsible panels — localStorage-
 * backed, per user/device (like useActivityDisplay; deliberately NOT
 * server-persisted). Provider-less on purpose: the Active-now panels render
 * in trees that mount TWICE (ResponsiveDrawer keeps both the mobile and
 * desktop sidebar mounted), so per-instance useState would desync — a tiny
 * useSyncExternalStore keyed store keeps every copy in step.
 *
 * Cross-tab updates arrive via the `storage` event; same-tab updates go
 * through the in-module emitter — `storage` never fires in the writing tab.
 */
import { useSyncExternalStore } from 'react'

const listeners = new Map<string, Set<() => void>>()
// Stable per-key subscribe fns — a fresh closure per render would make
// useSyncExternalStore tear down and resubscribe on every render.
const subscribers = new Map<string, (l: () => void) => () => void>()

function subscribeFor(key: string) {
  let sub = subscribers.get(key)
  if (!sub) {
    sub = (listener: () => void): (() => void) => {
      let set = listeners.get(key)
      if (!set) {
        set = new Set()
        listeners.set(key, set)
      }
      set.add(listener)
      const onStorage = (e: StorageEvent) => {
        // key === null means "storage cleared" — re-read then too.
        if (e.key === key || e.key === null) listener()
      }
      window.addEventListener('storage', onStorage)
      return () => {
        set.delete(listener)
        window.removeEventListener('storage', onStorage)
      }
    }
    subscribers.set(key, sub)
  }
  return sub
}

function getOpen(key: string, defaultOpen: boolean): boolean {
  try {
    const v = localStorage.getItem(key)
    if (v === 'open') return true
    if (v === 'closed') return false
  } catch {
    // storage unavailable (privacy mode) — default wins
  }
  return defaultOpen
}

function setOpen(key: string, open: boolean): void {
  try {
    localStorage.setItem(key, open ? 'open' : 'closed')
  } catch {
    // No storage, no persistence — the emitter still flips mounted copies
    // only if the read path could see the change; acceptable to no-op.
  }
  for (const l of listeners.get(key) ?? []) l()
}

/** Persisted collapse state: `[open, toggle]`. `key` must be stable. */
export function useCollapsePref(key: string, defaultOpen: boolean): [boolean, () => void] {
  const open = useSyncExternalStore(
    subscribeFor(key),
    () => getOpen(key, defaultOpen),
    () => defaultOpen,
  )
  return [open, () => setOpen(key, !getOpen(key, defaultOpen))]
}
