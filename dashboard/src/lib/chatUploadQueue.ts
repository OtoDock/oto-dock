// Sequential upload queue for CHAT attachments — a module-level singleton.
//
// Why not component state or a per-call loop: chat uploads are eager-on-pick
// and can outlive the page that started them (chat navigation unmounts
// AgentChat; a warmup re-keys the draft slice under transferNewChatToChat),
// and a per-call loop would run one chain per pick — adding 3×300MB files in
// one pick (or across picks/chats) must serialize on ONE wire chain, which is
// exactly the failure mode this replaces (three parallel fetches with no
// progress).
//
// State writes go through the chat slice that CURRENTLY holds the file id —
// resolved at write time, so the draft→chat re-key never strands a chip.

import { useChatStore } from '../store/chatStore'
import { useTransferStore } from '../store/transferStore'
import type { PendingFile } from '../store/types'

import { uploadWithProgress } from './uploadWithProgress'

export interface ChatUploadEntry {
  fileId: string
  file: File
  agent: string
  abort: AbortController
}

const queue: ChatUploadEntry[] = []
let running = false

// Store writes for a many-minutes 300MB upload would otherwise re-render the
// whole chat tree per XHR tick — cap chip updates to whole-percent steps at
// most every PROGRESS_MIN_INTERVAL_MS.
const PROGRESS_MIN_INTERVAL_MS = 250

/** The chat slice currently holding this pending file (null once removed). */
function locateChatId(fileId: string): string | null {
  const byChat = useChatStore.getState().byChat
  for (const [chatId, slice] of Object.entries(byChat)) {
    if (slice.pendingFiles.some((f) => f.id === fileId)) return chatId
  }
  return null
}

function patchFile(fileId: string, patch: Partial<PendingFile>): void {
  const chatId = locateChatId(fileId)
  if (chatId) useChatStore.getState().updatePendingFile(chatId, fileId, patch)
}

// transferStore.prune() normally runs from the workspace TransferPopup's
// open-while interval; chat-originated transfers must not depend on that
// popup ever opening, or a finished upload pins the toolbar's "active
// transfers" badge forever. Tick while anything is tracked, stop when idle.
let pruneTimer: ReturnType<typeof setInterval> | null = null
function ensurePruneTick(): void {
  if (pruneTimer != null) return
  pruneTimer = setInterval(() => {
    const ts = useTransferStore.getState()
    ts.prune()
    if (
      Object.keys(useTransferStore.getState().byId).length === 0 &&
      queue.length === 0 && !running && pruneTimer != null
    ) {
      clearInterval(pruneTimer)
      pruneTimer = null
    }
  }, 5000)
}

/** Add a file to the global chain (uploads start in pick order, one at a time). */
export function enqueueChatUpload(entry: ChatUploadEntry): void {
  queue.push(entry)
  ensurePruneTick()
  void pump()
}

/** Drop a still-queued entry (removal of an in-flight one aborts instead). */
export function dequeueChatUpload(fileId: string): boolean {
  const idx = queue.findIndex((e) => e.fileId === fileId)
  if (idx < 0) return false
  queue.splice(idx, 1)
  return true
}

/** Test hook: pending entry count (the running one is not pending). */
export function chatUploadQueueSize(): number {
  return queue.length
}

async function pump(): Promise<void> {
  if (running) return
  running = true
  try {
    while (queue.length > 0) {
      const entry = queue.shift()!
      if (entry.abort.signal.aborted) continue
      if (!locateChatId(entry.fileId)) continue // removed while queued
      await uploadOne(entry)
    }
  } finally {
    running = false
  }
}

async function uploadOne(entry: ChatUploadEntry): Promise<void> {
  const ts = useTransferStore.getState()
  const clientId = `local:chat-${entry.fileId}`
  patchFile(entry.fileId, {
    queued: false, uploading: true,
    uploadSent: 0, uploadTotal: entry.file.size,
  })
  // Chat uploads land in the default target (uploads/files) — targetDir ''.
  // Pre-link the relPath is the bare filename → no workspace section, which
  // is fine: the chip is the phase-1 surface; linkUpload swaps in the server
  // path and the workspace popup picks it up for phase 2.
  ts.beginLocalUpload({
    clientId, agent: entry.agent, targetDir: '',
    filename: entry.file.name, bytesTotal: entry.file.size,
  })

  let lastPct = -1
  let lastWrite = 0
  try {
    const resp = await uploadWithProgress(
      entry.file, entry.agent, '',
      (sent, total) => {
        useTransferStore.getState().updateLocalUpload(clientId, sent)
        const pctNow = total > 0 ? Math.floor((sent / total) * 100) : 0
        const now = Date.now()
        if (pctNow !== lastPct && now - lastWrite >= PROGRESS_MIN_INTERVAL_MS) {
          lastPct = pctNow
          lastWrite = now
          patchFile(entry.fileId, { uploadSent: sent, uploadTotal: total })
        }
      },
      entry.abort.signal,
    )
    patchFile(entry.fileId, {
      uploading: false, queued: false,
      uploadedPath: resp.path,
      name: resp.filename || entry.file.name,
      uploadSent: resp.size, uploadTotal: resp.size,
      transferId: resp.transfer_id, remotePush: resp.remote_push,
    })
    useTransferStore.getState().linkUpload(clientId, {
      transferId: resp.transfer_id,
      relPath: resp.path,
      remotePush: resp.remote_push,
    })
  } catch (e: any) {
    useTransferStore.getState().failLocalUpload(clientId)
    if (e?.name === 'AbortError') return // user removed the file — chip is gone
    patchFile(entry.fileId, {
      uploading: false, queued: false,
      error: e?.message || 'Upload failed',
    })
  }
}
