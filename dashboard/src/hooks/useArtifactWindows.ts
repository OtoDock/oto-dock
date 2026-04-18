import { useCallback, useEffect, useRef, useState } from 'react'
import type { MessageBlock } from '@/components/chat/types'
import { eventToBlock } from '@/lib/messageBlocks'
import { replayableDisplayEvents } from '@/lib/displayReplay'
import { loadDismissedPips, recordDismissedPip } from '@/lib/pipDismissals'
import { fetchChatPage } from '@/api/chats'
import type { InteractiveWs } from './useInteractiveTerminal'

/**
 * Owns the floating display/file-tools artifact windows for an interactive CLI
 * session. Subscribes to `pty_artifact` frames (the
 * drainer forwards display-mcp/file-tools artifacts there — there is no inline
 * message list in interactive), converts each to a renderable MessageBlock via
 * the shared `eventToBlock` mapper, and maintains the open-window list. The
 * placeholder/dedup rules mirror the -p pump (stream_pump `_handle_perm_event`):
 *   - image_generating  → a placeholder window
 *   - images            → replaces the latest image_generating placeholder
 *   - image_gen_failed  → removes the latest image_generating placeholder
 *   - media_processing  → a placeholder window
 *   - video / audio     → replaces the latest media_processing placeholder
 *   - media_failed      → removes the latest media_processing placeholder
 *   - document_preview  → replaces the window with the same file_id (in place)
 *   - ui                → replaces the window with the same path (in place)
 *   - url / file / images → a new window each
 *
 * REPLAY-ON-OPEN: the drainer also persists every final artifact as a chat row
 * and stamps its row id on the frame (`db_message_id`). When the terminal
 * (re)attaches, the hook fetches the newest history page and re-opens the
 * FINAL TURN's popups (rule + cap in `lib/displayReplay`), so a user who was
 * away — or who reloaded — still gets the displays. The row id is the dedupe
 * key between the seed and any frames that raced in live, and the X-dismiss
 * memory key (`lib/pipDismissals`, per-browser localStorage).
 *
 * Visual placement + dragging lives in the rendering component; this hook holds
 * only data (windows + which are minimized) so it stays testable + view-free.
 */

export interface ArtifactWindow {
  id: number
  block: MessageBlock
  title: string
  /** chat_messages row id of the persisted artifact event (final types only —
   * placeholders never persist). Stable across reloads; keys replay dedupe +
   * dismissal memory. */
  dbId?: number
}

export function titleFor(block: MessageBlock): string {
  switch (block.type) {
    case 'images': {
      const cap = block.images.find((i) => i.caption)?.caption
      return cap || (block.images.length > 1 ? `${block.images.length} images` : 'Image')
    }
    case 'image_generating':
      return 'Generating image…'
    case 'media_processing':
      return block.mediaKind === 'audio' ? 'Preparing audio…' : 'Preparing video…'
    case 'video':
      return block.title || block.caption || 'Video'
    case 'audio':
      return block.title || block.caption || 'Audio'
    case 'url':
      return block.title || block.url || 'Link'
    case 'file':
      return block.filename || 'File'
    case 'document_preview':
      return block.filename || 'Document'
    case 'ui':
      return block.title || 'UI artifact'
    default:
      return 'Artifact'
  }
}

function removeLatestOfType(wins: ArtifactWindow[], type: MessageBlock['type']): ArtifactWindow[] {
  for (let i = wins.length - 1; i >= 0; i--) {
    if (wins[i].block.type === type) {
      const copy = wins.slice()
      copy.splice(i, 1)
      return copy
    }
  }
  return wins
}

/** Insert/replace one artifact block in the window list: skip when its row id
 * is already showing (seed ⇄ live race), replace in place on the
 * document_preview file_id / ui path identity, else append a new window. */
function upsertWindow(
  wins: ArtifactWindow[],
  block: MessageBlock,
  dbId: number | undefined,
  allocId: () => number,
): ArtifactWindow[] {
  if (dbId !== undefined && wins.some((w) => w.dbId === dbId)) return wins
  // In-place replace, but never DOWNGRADE: a seed row must not overwrite a
  // window a newer live frame already updated (row ids are monotonic).
  const replaceAt = (idx: number): ArtifactWindow[] => {
    const cur = wins[idx].dbId
    if (cur !== undefined && dbId !== undefined && cur > dbId) return wins
    const copy = wins.slice()
    copy[idx] = { ...copy[idx], block, title: titleFor(block), dbId }
    return copy
  }
  if (block.type === 'document_preview') {
    const idx = wins.findIndex(
      (w) => w.block.type === 'document_preview' && w.block.fileId === block.fileId,
    )
    if (idx >= 0) return replaceAt(idx)
  }
  if (block.type === 'ui' && block.path) {
    // A re-shown artifact (same workspace file) replaces its window in
    // place — the PiP twin of the inline chat's supersede-to-chip rule.
    const idx = wins.findIndex((w) => w.block.type === 'ui' && w.block.path === block.path)
    if (idx >= 0) return replaceAt(idx)
  }
  return [...wins, { id: allocId(), block, title: titleFor(block), dbId }]
}

export function useArtifactWindows(ws: InteractiveWs, chatId: string) {
  const { subscribe } = ws
  const [windows, setWindows] = useState<ArtifactWindow[]>([])
  const [minimized, setMinimized] = useState<Set<number>>(new Set())
  const idRef = useRef(0)
  const allocId = useCallback(() => ++idRef.current, [])
  const windowsRef = useRef(windows)
  windowsRef.current = windows

  const close = useCallback((id: number) => {
    // X = permanent for this browser: remember the persisted row id so the
    // replay-on-open seed never re-opens this popup (see lib/pipDismissals —
    // cross-device re-show is the documented v1 limit).
    const dbId = windowsRef.current.find((w) => w.id === id)?.dbId
    if (dbId !== undefined && chatId) recordDismissedPip(chatId, dbId)
    setWindows((prev) => prev.filter((w) => w.id !== id))
    setMinimized((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [chatId])

  const minimize = useCallback((id: number) => {
    setMinimized((prev) => {
      const next = new Set(prev)
      next.add(id)
      return next
    })
  }, [])

  const restore = useCallback((id: number) => {
    setMinimized((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  }, [])

  useEffect(() => {
    // New chat / session / left interactive → start with no windows; the
    // replay seed below re-opens the final turn's persisted artifacts. An
    // empty chatId (not interactive) clears + does not subscribe.
    setWindows([])
    setMinimized(new Set())
    if (!chatId) return

    const unsub = subscribe('pty_artifact', (msg: any) => {
      if (msg.chat_id && msg.chat_id !== chatId) return
      const event = msg.event
      const t = event?.type
      if (!t) return
      // Removal events carry no renderable block — drop the placeholder window.
      if (t === 'image_gen_failed') {
        setWindows((prev) => removeLatestOfType(prev, 'image_generating'))
        return
      }
      if (t === 'media_failed') {
        setWindows((prev) => removeLatestOfType(prev, 'media_processing'))
        return
      }
      const block = eventToBlock(event)
      if (!block) return
      const dbId = typeof event.db_message_id === 'number' ? event.db_message_id : undefined
      setWindows((prev) => {
        let next = prev
        if (t === 'images') next = removeLatestOfType(next, 'image_generating')
        if (t === 'video' || t === 'audio') next = removeLatestOfType(next, 'media_processing')
        return upsertWindow(next, block, dbId, allocId)
      })
    })

    // Replay-on-open: re-open the FINAL TURN's persisted popups (recency rule,
    // identity dedupe + cap in lib/displayReplay; per-browser X-dismissals
    // filtered there). Runs after subscribe, so frames racing in live are
    // deduped against the seed by row id in upsertWindow.
    let stale = false
    fetchChatPage(chatId, 50)
      .then(({ messages }) => {
        if (stale) return
        const replay = replayableDisplayEvents(messages, loadDismissedPips(chatId))
        if (!replay.length) return
        setWindows((prev) => {
          let next = prev
          for (const r of replay) next = upsertWindow(next, r.block, r.dbId, allocId)
          return next
        })
      })
      .catch(() => { /* seed is best-effort — live frames still render */ })

    return () => {
      stale = true
      unsub()
    }
  }, [chatId, subscribe, allocId])

  return { windows, minimized, close, minimize, restore }
}
