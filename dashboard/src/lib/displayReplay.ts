import type { ChatMessage } from '@/api/chats'
import type { MessageBlock } from '@/components/chat/types'
import { eventToBlock } from './messageBlocks'

// Replay-on-open rule for interactive PiP artifact windows.
//
// The interactive drainer persists every FINAL display/file-tools artifact as
// a chat_messages event row (proxy interactive_session.persist_drained_artifact);
// this module decides which of those rows re-open as popups when the terminal
// view (re)attaches.
//
// RECENCY RULE — the FINAL TURN: rows strictly AFTER the last `user` row of
// the newest history page. Older turns' artifacts stay in the rich DB history
// only (they were answered/moved past); the current/last turn's displays are
// the ones still relevant over the terminal. A page with no user row means
// every loaded row is newer than the last prompt, so all of it qualifies.
//
// On top of that:
//   - document_preview dedupes by file_id and ui by path (latest wins) — the
//     same in-place-replace identity the live pty_artifact handler uses;
//   - rows the server marked dismissed (chat-level preview dismissals) and
//     ids in the caller's per-browser X-dismiss set are dropped;
//   - capped to the newest MAX_REPLAY_WINDOWS so a display-heavy turn can't
//     storm the terminal with popups.
export const MAX_REPLAY_WINDOWS = 6

const REPLAYABLE_TYPES = new Set([
  'images', 'url', 'file', 'video', 'audio', 'document_preview', 'ui',
])

export interface ReplayArtifact {
  /** chat_messages row id — the stable dedupe/dismissal key. */
  dbId: number
  block: MessageBlock
}

export function replayableDisplayEvents(
  messages: ChatMessage[],
  dismissedIds: Set<number>,
): ReplayArtifact[] {
  let lastUserIdx = -1
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') { lastUserIdx = i; break }
  }
  const out: ReplayArtifact[] = []
  for (let i = lastUserIdx + 1; i < messages.length; i++) {
    const m = messages[i]
    if (m.role !== 'event' || !REPLAYABLE_TYPES.has(m.event_type) || !m.event_data) continue
    let evt: any
    try { evt = JSON.parse(m.event_data) } catch { continue }
    if (!evt || evt.dismissed) continue
    const block = eventToBlock(evt, m.id)
    if (!block) continue
    // Latest-wins identity dedupe (chronological walk → later replaces earlier).
    if (block.type === 'document_preview') {
      const idx = out.findIndex((r) => r.block.type === 'document_preview' && r.block.fileId === block.fileId)
      if (idx >= 0) { out[idx] = { dbId: m.id, block }; continue }
    }
    if (block.type === 'ui' && block.path) {
      const idx = out.findIndex((r) => r.block.type === 'ui' && r.block.path === block.path)
      if (idx >= 0) { out[idx] = { dbId: m.id, block }; continue }
    }
    out.push({ dbId: m.id, block })
  }
  return out.filter((r) => !dismissedIds.has(r.dbId)).slice(-MAX_REPLAY_WINDOWS)
}
