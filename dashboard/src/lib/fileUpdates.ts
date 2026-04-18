// Lightweight in-process pub/sub for `file_updated` WS events.
//
// useDashboardWs emits here when a shared workspace file changes on the server
// (a Collabora save or an agent/disk write). Deeply-nested Collabora preview
// iframes subscribe via useCollaboraLiveReload so they can refresh without
// prop-drilling a callback through the whole component tree. Decoupled from
// React state on purpose — emit is a plain fan-out; subscribers manage their
// own lifecycle.

export interface FileUpdate {
  agent_slug: string
  rel_path: string
  file_id?: string
  source?: string // "collabora" | "disk"
  /** Dock pin membership changed for this path (file pinned/unpinned) —
   * refresh the pins LIST, not just open content. */
  pin?: boolean
}

type Listener = (u: FileUpdate) => void

const listeners = new Set<Listener>()

export function emitFileUpdate(u: FileUpdate): void {
  listeners.forEach((l) => {
    try {
      l(u)
    } catch {
      /* one subscriber throwing must not break the fan-out */
    }
  })
}

export function onFileUpdate(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
