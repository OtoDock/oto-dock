// The side of the chat page the minimized windows' dock sits on, per device:
// a chip dragged past the middle of the chat moves the dock there, and the
// next visit keeps it. localStorage, per browser like lib/pipDismissals; a
// refused read or write degrades to the left, the default side.

import { useSyncExternalStore } from 'react'

export const DOCK_SIDE = { LEFT: 'left', RIGHT: 'right' } as const
export type DockSide = typeof DOCK_SIDE[keyof typeof DOCK_SIDE]

const KEY = 'oto.dock.side'
const listeners = new Set<() => void>()

export function loadDockSide(): DockSide {
  try {
    return localStorage.getItem(KEY) === DOCK_SIDE.RIGHT ? DOCK_SIDE.RIGHT : DOCK_SIDE.LEFT
  } catch {
    return DOCK_SIDE.LEFT
  }
}

export function setDockSide(side: DockSide): void {
  try {
    localStorage.setItem(KEY, side)
  } catch {
    /* storage refused: this page still moves the dock, the next one does not */
  }
  current = side
  listeners.forEach((l) => l())
}

export function otherSide(side: DockSide): DockSide {
  return side === DOCK_SIDE.RIGHT ? DOCK_SIDE.LEFT : DOCK_SIDE.RIGHT
}

let current: DockSide | null = null

function snapshot(): DockSide {
  if (current === null) current = loadDockSide()
  return current
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  // Another tab of this browser moved the dock.
  const onStorage = (e: StorageEvent) => {
    if (e.key !== KEY && e.key !== null) return
    current = loadDockSide()
    onChange()
  }
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(onChange)
    window.removeEventListener('storage', onStorage)
  }
}

export function useDockSide(): DockSide {
  return useSyncExternalStore(subscribe, snapshot, () => DOCK_SIDE.LEFT)
}

/** Tests: forget the cached side so the next read goes to storage. */
export function resetDockSideForTests(): void {
  current = null
}
