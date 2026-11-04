import type { DockState } from '../hooks/useArtifactWindows'

/**
 * A terminal chat's artifact windows as the person left them (which are
 * open, which minimized, the dock's chip order), per chat, while the page
 * lives: a switch to another chat and back finds them as they were. Never
 * stored: a reload starts clean and the replay seed re-opens the final
 * turn's windows (`useArtifactWindows`).
 */

/** Chats kept per page; the least recently kept goes first. */
export const MAX_KEPT_DOCKS = 50

const docks = new Map<string, DockState>()
let lastWindowId = 0

/** A window id unique on the page, across chats and mounts. */
export function nextWindowId(): number {
  return ++lastWindowId
}

export function keptDock(chatId: string): DockState | undefined {
  return chatId ? docks.get(chatId) : undefined
}

export function keepDock(chatId: string, dock: DockState): void {
  if (!chatId) return
  docks.delete(chatId)
  docks.set(chatId, dock)
  while (docks.size > MAX_KEPT_DOCKS) {
    const oldest = docks.keys().next().value
    if (oldest === undefined) break
    docks.delete(oldest)
  }
}

/** The chat is gone: its windows with it. */
export function forgetDock(chatId: string): void {
  docks.delete(chatId)
}

/** Every chat's windows, for a test's clean start. */
export function resetDocks(): void {
  docks.clear()
}
